"""Custom DeepEval metrics scored against a game's persisted decisions.

Two kinds, deliberately: `LegalActionMetric` is rule-based (no LLM call,
free, deterministic) — it doesn't need an LLM judge to know whether an
action was sanitized, since `agents/graph.py`'s `_sanitize_action` already
leaves an unambiguous trace in the rationale text. `RoleAlignmentMetric` is
a judgment call (does this decision fit the faction's stated doctrine?)
that genuinely needs an LLM, via `GEval`.
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
