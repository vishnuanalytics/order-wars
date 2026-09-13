"""GameHub in isolation, with real threads but deterministic ordering via
threading.Event — no FastAPI/websocket layer, no timing-based flakiness.
"""

import threading
import time

from agents.actions import FactionAction
from backend.game_hub import GameHub


def test_subscribe_then_broadcast_delivers_in_order():
    hub = GameHub()
    q = hub.subscribe("game-1")

    hub.broadcast("game-1", {"n": 1})
    hub.broadcast("game-1", {"n": 2})

    assert q.get(timeout=1) == {"n": 1}
    assert q.get(timeout=1) == {"n": 2}


def test_broadcast_only_reaches_subscribers_of_that_game():
    hub = GameHub()
    q_a = hub.subscribe("game-a")
    q_b = hub.subscribe("game-b")

    hub.broadcast("game-a", {"only": "a"})

    assert q_a.get(timeout=1) == {"only": "a"}
    assert q_b.empty()


def test_unsubscribe_stops_further_delivery():
    hub = GameHub()
    q = hub.subscribe("game-1")
    hub.unsubscribe("game-1", q)

    hub.broadcast("game-1", {"n": 1})

    assert q.empty()


def test_start_runs_target_and_broadcasts_stream_end_on_success():
    hub = GameHub()
    q = hub.subscribe("game-1")
    started = threading.Event()

    def _target():
        started.set()
        hub.broadcast("game-1", {"type": "state"})

    hub.start("game-1", _target)

    assert started.wait(timeout=1)
    assert q.get(timeout=1) == {"type": "state"}
    assert q.get(timeout=1) == {"type": "stream_end"}


def test_start_broadcasts_error_then_stream_end_when_target_raises():
    hub = GameHub()
    q = hub.subscribe("game-1")

    def _target():
        raise ValueError("boom")

    hub.start("game-1", _target)

    first = q.get(timeout=1)
    assert first == {"type": "error", "message": "boom"}
    assert q.get(timeout=1) == {"type": "stream_end"}


def test_start_does_not_block_the_caller():
    hub = GameHub()
    release = threading.Event()

    def _target():
        release.wait(timeout=2)

    hub.start("game-1", _target)  # must return immediately, not wait for _target
    release.set()


def test_start_forgets_the_game_after_finishing_successfully():
    hub = GameHub()
    finished = threading.Event()

    hub.start("game-1", finished.set)

    assert finished.wait(timeout=1)
    # Give the thread's finally-block a moment to run past broadcast().
    for _ in range(50):
        if "game-1" not in hub._subscribers:
            break
        time.sleep(0.01)
    assert "game-1" not in hub._subscribers


def test_start_forgets_the_game_after_raising():
    hub = GameHub()

    def _target():
        raise ValueError("boom")

    hub.start("game-1", _target)

    for _ in range(50):
        if "game-1" not in hub._subscribers:
            break
        time.sleep(0.01)
    assert "game-1" not in hub._subscribers


def test_a_current_subscriber_still_gets_stream_end_despite_cleanup():
    """Cleanup removes the dict entry, but a subscriber holding a direct
    queue reference from before cleanup must still receive everything
    broadcast before it ran.
    """
    hub = GameHub()
    q = hub.subscribe("game-1")

    hub.start("game-1", lambda: None)

    assert q.get(timeout=1) == {"type": "stream_end"}


# --- Live-play control plane -------------------------------------------


def test_take_control_marks_a_faction_human_controlled():
    hub = GameHub()
    assert hub.is_human_controlled("game-1", "rome") is False

    hub.take_control("game-1", "rome")

    assert hub.is_human_controlled("game-1", "rome") is True
    assert hub.controlled_factions("game-1") == {"rome"}


def test_release_control_reverts_to_ai():
    hub = GameHub()
    hub.take_control("game-1", "rome")

    hub.release_control("game-1", "rome")

    assert hub.is_human_controlled("game-1", "rome") is False


def test_control_is_scoped_per_game_and_per_faction():
    hub = GameHub()
    hub.take_control("game-1", "rome")

    assert hub.is_human_controlled("game-1", "carthage") is False
    assert hub.is_human_controlled("game-2", "rome") is False


def test_submit_action_then_await_returns_it_immediately():
    hub = GameHub()
    action = FactionAction(action_type="hold", rationale="testing")

    hub.submit_action("game-1", "rome", action)

    assert hub.await_human_action("game-1", "rome", timeout=1) == action


def test_await_human_action_returns_none_on_timeout():
    hub = GameHub()
    assert hub.await_human_action("game-1", "rome", timeout=0.05) is None


def test_forget_clears_control_state_and_pending_action_queues():
    hub = GameHub()
    hub.take_control("game-1", "rome")
    hub.submit_action("game-1", "rome", FactionAction(action_type="hold", rationale="x"))

    hub._forget("game-1")

    assert hub.is_human_controlled("game-1", "rome") is False
    assert ("game-1", "rome") not in hub._action_queues


def test_register_factions_then_slug_for_translates_db_id_to_slug():
    hub = GameHub()
    hub.register_factions("game-1", {"uuid-rome": "rome", "uuid-carthage": "carthage"})

    assert hub.slug_for("game-1", "uuid-rome") == "rome"
    assert hub.slug_for("game-1", "uuid-carthage") == "carthage"


def test_slug_for_returns_none_for_an_unregistered_id():
    hub = GameHub()
    assert hub.slug_for("game-1", "no-such-id") is None


def test_forget_clears_registered_faction_slugs():
    hub = GameHub()
    hub.register_factions("game-1", {"uuid-rome": "rome"})

    hub._forget("game-1")

    assert hub.slug_for("game-1", "uuid-rome") is None
