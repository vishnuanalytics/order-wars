"""GameHub in isolation, with real threads but deterministic ordering via
threading.Event — no FastAPI/websocket layer, no timing-based flakiness.
"""

import queue
import threading

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
