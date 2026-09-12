"""In-process registry of running games and their live-update subscribers.

`game.run_game.play_game` is a synchronous, potentially long-running
(minutes — it's making real LLM calls every turn) blocking call. FastAPI's
request handlers need to return immediately with a game id, so `play_game`
runs on a background thread here; WebSocket clients subscribe to a plain
`queue.Queue` (thread-safe) that the background thread pushes into, and the
async WebSocket handler forwards from that queue via `asyncio.to_thread` —
this avoids the pitfall of touching asyncio objects (like `asyncio.Queue`)
from a non-event-loop thread, which isn't safe.

Single-process only, by design and clearly scoped — a known limit, not an
oversight. A multi-worker/multi-process deployment (several `uvicorn`
workers, or a real production rollout) would need a shared pub/sub broker
(e.g. Redis) instead, since this registry only lives in one process's
memory. Fine for this project's scale; call out explicitly if that changes.
"""

import queue
import threading
from collections.abc import Callable


class GameHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[queue.Queue]] = {}

    def start(self, game_id: str, target: Callable[[], None]) -> None:
        """Run `target` (a zero-arg callable — the caller closes over
        whatever `play_game` needs) on a background thread, broadcasting a
        terminal message when it finishes or raises.
        """

        def _run() -> None:
            try:
                target()
            except Exception as exc:  # noqa: BLE001 — reported to subscribers, not swallowed
                self.broadcast(game_id, {"type": "error", "message": str(exc)})
            finally:
                self.broadcast(game_id, {"type": "stream_end"})

        threading.Thread(target=_run, daemon=True).start()

    def subscribe(self, game_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.setdefault(game_id, []).append(q)
        return q

    def unsubscribe(self, game_id: str, q: queue.Queue) -> None:
        with self._lock:
            subs = self._subscribers.get(game_id)
            if subs and q in subs:
                subs.remove(q)

    def broadcast(self, game_id: str, message: dict) -> None:
        with self._lock:
            subs = list(self._subscribers.get(game_id, ()))
        for q in subs:
            q.put(message)


hub = GameHub()
