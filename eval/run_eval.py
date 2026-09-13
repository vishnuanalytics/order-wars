"""Batch eval runner (`python -m eval.run_eval --game-id <id>`).

Scores every faction-scoped GameEvent in a game against the metrics in
eval/metrics.py and persists the results to eval_scores. Read-only towards
the game itself — running eval twice on the same game just adds a fresh set
of eval_scores rows each time (no dedup/versioning), since each metric run
can use different provider behavior and is a legitimate independent sample,
not necessarily a correction of the last one.
"""

import argparse
import uuid

from deepeval.test_case import LLMTestCase
from sqlalchemy.orm import Session, sessionmaker

from agents.roles import describe
from db.models import EvalScore, Game, GameEvent, GameFaction
from db.session import get_sessionmaker, scoped_session
from eval.llm_wrapper import DeepEvalLLM
from eval.metrics import LegalActionMetric, build_role_alignment_metric


def _build_test_case(event: GameEvent, faction: GameFaction) -> LLMTestCase:
    payload = event.payload
    target = payload.get("target_province") or payload.get("target_faction") or ""
    actual_output = f"{event.event_type}" + (f" -> {target}" if target else "")
    if payload.get("rationale"):
        actual_output += f": {payload['rationale']}"
    if payload.get("resolution"):
        actual_output += f" ({payload['resolution']})"

    input_text = (
        f"Faction '{faction.faction_name}' has role preset "
        f"'{faction.role_preset.value}' ({describe(faction.role_preset.value)})"
    )
    return LLMTestCase(input=input_text, actual_output=actual_output)


def run_eval(
    game_id: uuid.UUID,
    session_factory: sessionmaker[Session] | None = None,
) -> list[dict]:
    """Returns a list of `{turn, faction_name, metric_name, score, success}`
    dicts for every (event, metric) pair scored, in addition to persisting
    them as EvalScore rows.
    """
    session_factory = session_factory or get_sessionmaker()
    model = DeepEvalLLM()
    role_alignment_metric = build_role_alignment_metric(model)
    legal_action_metric = LegalActionMetric()

    results = []
    with scoped_session(session_factory) as session:
        game = session.get(Game, game_id)
        if game is None:
            raise ValueError(f"No game with id {game_id}")

        factions_by_id = {faction.id: faction for faction in game.factions}
        events = (
            session.query(GameEvent)
            .filter_by(game_id=game_id)
            .order_by(GameEvent.turn)
            .all()
        )

        for event in events:
            if event.faction_id is None:
                continue  # not every event type need be faction-scoped
            faction = factions_by_id[event.faction_id]
            test_case = _build_test_case(event, faction)

            for metric in (legal_action_metric, role_alignment_metric):
                metric.measure(test_case)
                session.add(
                    EvalScore(
                        game_event_id=event.id,
                        metric_name=metric.__name__,
                        score=metric.score,
                        success=bool(metric.is_successful()),
                        reason=metric.reason,
                    )
                )
                results.append(
                    {
                        "turn": event.turn,
                        "faction_name": faction.faction_name,
                        "metric_name": metric.__name__,
                        "score": metric.score,
                        "success": metric.is_successful(),
                    }
                )

    return results


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-id", required=True, help="A completed game's id.")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    scored = run_eval(uuid.UUID(args.game_id))

    print(f"Scored {len(scored)} (event, metric) pairs:\n")
    for row in scored:
        status = "OK" if row["success"] else "LOW"
        print(f"[{status}] turn {row['turn']} — {row['faction_name']} — {row['metric_name']}: {row['score']:.2f}")

    if scored:
        legal_scores = [r["score"] for r in scored if r["metric_name"] == "Legal Action"]
        role_scores = [r["score"] for r in scored if r["metric_name"] == "Role Alignment"]
        if legal_scores:
            print(f"\nLegal Action rate: {sum(legal_scores) / len(legal_scores):.0%}")
        if role_scores:
            print(f"Role Alignment average: {sum(role_scores) / len(role_scores):.2f}")
