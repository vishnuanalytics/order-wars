"""Custom DeepEval metrics scored against a game's persisted decisions.

Three metrics, two families: `LegalActionMetric` and `ResourceEfficiencyMetric`
are both rule-based (no LLM call, free, deterministic) — they read an
unambiguous marker already left in the persisted text (`_sanitize_action`'s
"sanitized to hold" for legality; `game/rules.py`'s own failure wording for
efficiency) rather than needing a judgment call. `RoleAlignmentMetric` is a
genuine judgment call (does this decision fit the faction's stated
doctrine?) that needs an LLM, via `GEval` — kept as the only LLM-judged
metric so evaluating a game doesn't scale its LLM cost with how many
dimensions are scored (see the project's known Groq/OpenRouter free-tier
and $0 Anthropic-credit constraints).
"""

from deepeval.metrics import BaseMetric, GEval
from deepeval.models.base_model import DeepEvalBaseLLM
from deepeval.test_case import LLMTestCase, SingleTurnParams

# The exact phrase agents/graph.py's _sanitize_action appends to the
# rationale of any action it downgrades to hold. Matching on this instead
# of re-deriving legality independently keeps this metric honest about what
# it's actually checking — whatever made it into the persisted rationale —
# rather than a parallel reimplementation of the sanitizer's rules that
# could silently drift from it.
SANITIZED_MARKER = "sanitized to hold"


class LegalActionMetric(BaseMetric):
    """1.0 if the LLM's proposed action was legal as-is; 0.0 if
    `_sanitize_action` had to downgrade it to `hold`. No LLM call, no
    threshold ambiguity — this is a fact, not a judgment.
    """

    def __init__(self, threshold: float = 1.0):
        self.threshold = threshold

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        self.score = 0.0 if SANITIZED_MARKER in test_case.actual_output else 1.0
        self.reason = (
            "Action was sanitized to hold — the model proposed an illegal target."
            if self.score == 0.0
            else "Action was legal as proposed."
        )
        self.success = self.is_successful()
        return self.score

    async def a_measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        return self.measure(test_case, *args, **kwargs)

    def is_successful(self) -> bool:
        return self.score is not None and self.score >= self.threshold

    @property
    def __name__(self) -> str:
        return "Legal Action"


# Substrings that appear in game/rules.py's resolution text specifically
# when a legal action still failed to execute — wrong target ownership,
# an already-maxed province, or insufficient resources. These are actions
# _sanitize_action never touches (it only guards illegal targets, not
# affordability or ownership), so this metric is a genuine complement to
# LegalActionMetric, not a duplicate of it: an action can be perfectly
# legal and still waste the turn.
WASTE_MARKERS = ("lacked", "cannot develop", "already at maximum development")


class ResourceEfficiencyMetric(BaseMetric):
    """1.0 if a legal action actually executed as intended; 0.0 if it was a
    legal but wasted attempt — proposing a build/development the faction
    couldn't afford, or targeting a province it doesn't own. Rule-based,
    same precedent as LegalActionMetric: this is a fact drawn from
    game/rules.py's own resolution text, not a judgment call.
    """

    def __init__(self, threshold: float = 1.0):
        self.threshold = threshold

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        lowered = test_case.actual_output.lower()
        wasted = any(marker in lowered for marker in WASTE_MARKERS)
        self.score = 0.0 if wasted else 1.0
        self.reason = (
            "Action was legal but failed to execute — insufficient resources "
            "or an invalid target wasted the turn."
            if wasted
            else "Action executed as intended (or wasn't an economic action)."
        )
        self.success = self.is_successful()
        return self.score

    async def a_measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        return self.measure(test_case, *args, **kwargs)

    def is_successful(self) -> bool:
        return self.score is not None and self.score >= self.threshold

    @property
    def __name__(self) -> str:
        return "Resource Efficiency"


def build_role_alignment_metric(model: DeepEvalBaseLLM, threshold: float = 0.5) -> GEval:
    """A faction's decision should reflect its assigned role preset's
    doctrine (see agents/roles.py) — an expansionist grabbing empty land is
    aligned; an isolationist declaring an unprovoked war isn't. This is a
    judgment call, not a fact, hence GEval (an LLM-judge metric) rather than
    a rule-based one like LegalActionMetric above.
    """
    return GEval(
        name="Role Alignment",
        model=model,
        threshold=threshold,
        # GEval defaults to appending " [GEval]" to __name__ (the metric_name
        # stored on EvalScore rows and what eval/run_eval.py's CLI summary
        # groups by) — caught by a test asserting the exact stored name, not
        # by inspection. A plain "Role Alignment" is what actually gets
        # displayed/grouped, so keep it clean rather than leaking a
        # DeepEval-internal convention into persisted data.
        _include_g_eval_suffix=False,
        evaluation_params=[
            SingleTurnParams.INPUT,
            SingleTurnParams.ACTUAL_OUTPUT,
        ],
        evaluation_steps=[
            "The 'input' describes a faction's name, its assigned role preset "
            "and that role's doctrine, and the game context available when it "
            "acted (territory, resources, other factions, diplomatic status).",
            "The 'actual_output' is the action the faction actually chose, "
            "plus its stated rationale.",
            "Score how well the chosen action and rationale reflect the "
            "stated doctrine — not whether the action was a *good* move in "
            "some general strategic sense, only whether it fits the role.",
            "A generic action with a rationale that doesn't reference or "
            "reflect the doctrine at all should score low even if the action "
            "itself was legal and harmless.",
        ],
    )
