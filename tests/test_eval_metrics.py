"""eval/metrics.py — LegalActionMetric needs no LLM at all; RoleAlignmentMetric
(a GEval instance) is tested against a fake DeepEvalBaseLLM, never a live one.
"""

from deepeval.metrics.g_eval.schema import ReasonScore
from deepeval.models.base_model import DeepEvalBaseLLM
from deepeval.test_case import LLMTestCase

from eval.metrics import LegalActionMetric, ResourceEfficiencyMetric, build_role_alignment_metric


def test_legal_action_metric_scores_a_legal_action_as_1():
    tc = LLMTestCase(input="n/a", actual_output="move_army -> Italy 21: expand (moved into Italy 21 (now held))")
    metric = LegalActionMetric()
    assert metric.measure(tc) == 1.0
    assert metric.is_successful() is True


def test_legal_action_metric_scores_a_sanitized_action_as_0():
    tc = LLMTestCase(input="n/a", actual_output="hold: invalid move target 'xyz' sanitized to hold")
    metric = LegalActionMetric()
    assert metric.measure(tc) == 0.0
    assert metric.is_successful() is False
    assert "sanitized" in metric.reason.lower()


def test_resource_efficiency_metric_scores_a_successful_action_as_1():
    tc = LLMTestCase(input="n/a", actual_output="build_unit: build a legion (built 1 legion)")
    metric = ResourceEfficiencyMetric()
    assert metric.measure(tc) == 1.0
    assert metric.is_successful() is True


def test_resource_efficiency_metric_scores_a_lacked_resources_attempt_as_0():
    tc = LLMTestCase(
        input="n/a", actual_output="build_unit: build a legion (tried to build a legion but lacked 10 gold)"
    )
    metric = ResourceEfficiencyMetric()
    assert metric.measure(tc) == 0.0
    assert metric.is_successful() is False
    assert "insufficient resources" in metric.reason.lower()


def test_resource_efficiency_metric_scores_a_wrong_owner_develop_as_0():
    tc = LLMTestCase(
        input="n/a",
        actual_output="develop_province -> Italy 5: fortify (cannot develop 'Italy 5': not your territory)",
    )
    metric = ResourceEfficiencyMetric()
    assert metric.measure(tc) == 0.0


def test_resource_efficiency_metric_scores_an_already_maxed_develop_as_0():
    tc = LLMTestCase(
        input="n/a",
        actual_output="develop_province -> Italy 5: fortify (Italy 5 is already at maximum development)",
    )
    metric = ResourceEfficiencyMetric()
    assert metric.measure(tc) == 0.0


def test_resource_efficiency_metric_ignores_non_economic_actions():
    tc = LLMTestCase(input="n/a", actual_output="hold: nothing to do this turn (held position)")
    metric = ResourceEfficiencyMetric()
    assert metric.measure(tc) == 1.0


class _FakeEvalLLM(DeepEvalBaseLLM):
    """Returns a fixed ReasonScore instead of calling any real API — used
    to test build_role_alignment_metric's GEval wiring without live calls.
    """

    def __init__(self, score: float = 8.0, reason: str = "fits the doctrine"):
        self._score = score
        self._reason = reason
        super().__init__(model="fake")

    def load_model(self):
        return self

    def generate(self, prompt, schema=None):
        if schema is not None:
            return schema(score=self._score, reason=self._reason)
        return "fake response"

    async def a_generate(self, prompt, schema=None):
        return self.generate(prompt, schema=schema)

    def get_model_name(self):
        return "fake"


def test_role_alignment_metric_uses_the_provided_model():
    fake_model = _FakeEvalLLM(score=9.0, reason="clearly expansionist")
    metric = build_role_alignment_metric(fake_model)

    tc = LLMTestCase(
        input="Faction 'Rome' has role preset 'expansionist' (...)",
        actual_output="move_army -> Italy 21: expand into unclaimed territory",
    )
    score = metric.measure(tc)

    assert score == 0.9  # GEval normalizes a 0-10 raw score to 0-1
    assert metric.reason == "clearly expansionist"
    assert metric.is_successful() is True


def test_role_alignment_metric_respects_threshold():
    fake_model = _FakeEvalLLM(score=2.0, reason="doesn't fit the doctrine at all")
    metric = build_role_alignment_metric(fake_model, threshold=0.5)

    tc = LLMTestCase(input="n/a", actual_output="n/a")
    metric.measure(tc)

    assert metric.score == 0.2
    assert metric.is_successful() is False
