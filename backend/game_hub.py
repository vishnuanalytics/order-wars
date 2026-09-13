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

from agents.actions import FactionAction


class GameHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[queue.Queue]] = {}
        # Live-play control plane: which factions in a game are currently
        # human-controlled, and a place for a submitted action to land for
        # agents.graph's human_action_provider hook (via play_game, wired
        # up in backend/main.py's start_game) to pick up. Both no-auth —
        # "human-controlled" means whoever last sent take_control, the same
        # way everything else in this project has no user identity (see
        # CLAUDE.md Non-goals).
        self._human_controlled: dict[str, set[str]] = {}
        self._action_queues: dict[tuple[str, str], queue.Queue] = {}
        # game_id -> {faction db-uuid (str): agents.graph's internal slug}.
        # A submitted FactionAction's own target_faction field (naming a
        # *different* faction, e.g. to declare_war on) has to be in slug
        # space by the time it reaches agents/graph.py — see
        # _handle_live_control_message's use of slug_for below — but the
        # frontend only ever knows the DB id, the same as everywhere else
        # it references a faction. Registered once per game at start time
        # (backend/main.py's start_game already builds this mapping to
        # construct human_action_provider; this is just also stashing it
        # here for the WS handler, a separate code path, to reach).
        self._faction_slugs: dict[str, dict[str, str]] = {}

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
                # Nothing more will ever be broadcast for this game_id — drop
                # its entry so a long-running server's _subscribers dict
                # doesn't grow by one key (game finished, subscriber list
                # already empty) for every game ever played, forever. A
                # client that subscribes to this exact game_id afterward
                # just gets a fresh empty entry and waits forever for a
                # message that will never come — the same outcome a late
                # subscriber already got before this cleanup existed, so
                # this doesn't change that behavior, only stops it from
                # leaking memory for the (expected) common case of nobody
                # subscribing this late.
                self._forget(game_id)

        threading.Thread(target=_run, daemon=True).start()

    def _forget(self, game_id: str) -> None:
        with self._lock:
            self._subscribers.pop(game_id, None)
            self._human_controlled.pop(game_id, None)
            self._faction_slugs.pop(game_id, None)
            for key in [k for k in self._action_queues if k[0] == game_id]:
                self._action_queues.pop(key, None)

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

    # --- Live-play control plane ---------------------------------------

    def register_factions(self, game_id: str, uuid_to_slug: dict[str, str]) -> None:
        with self._lock:
            self._faction_slugs[game_id] = dict(uuid_to_slug)

    def slug_for(self, game_id: str, faction_db_id: str) -> str | None:
        with self._lock:
            return self._faction_slugs.get(game_id, {}).get(faction_db_id)

    def take_control(self, game_id: str, faction_id: str) -> None:
        with self._lock:
            self._human_controlled.setdefault(game_id, set()).add(faction_id)

    def release_control(self, game_id: str, faction_id: str) -> None:
        with self._lock:
            self._human_controlled.get(game_id, set()).discard(faction_id)

    def is_human_controlled(self, game_id: str, faction_id: str) -> bool:
        with self._lock:
            return faction_id in self._human_controlled.get(game_id, ())

    def controlled_factions(self, game_id: str) -> set[str]:
        with self._lock:
            return set(self._human_controlled.get(game_id, ()))

    def _action_queue(self, game_id: str, faction_id: str) -> queue.Queue:
        key = (game_id, faction_id)
        with self._lock:
            if key not in self._action_queues:
                self._action_queues[key] = queue.Queue()
            return self._action_queues[key]

    def submit_action(self, game_id: str, faction_id: str, action: FactionAction) -> None:
        self._action_queue(game_id, faction_id).put(action)

    def await_human_action(
        self, game_id: str, faction_id: str, timeout: float
    ) -> FactionAction | None:
        """Blocks the calling thread (agents.graph's turn loop, via
        play_game's injected human_action_provider) up to `timeout` seconds
        for a submitted action. Returns None on timeout — the caller falls
        back to letting the AI decide that turn; control itself isn't
        released, so the human keeps it for next time.
        """
        try:
            return self._action_queue(game_id, faction_id).get(timeout=timeout)
        except queue.Empty:
            return None


hub = GameHub()
