"""Phase 5 FastAPI backend (`uvicorn backend.main:app --reload`).

Thin HTTP/WebSocket layer over `game/run_game.py` and `db/models.py` — no
game logic lives here. `POST /games` returns as soon as the DB rows exist
(`game.run_game.create_game`, a handful of fast writes), then runs the
actual turn loop (`play_game` — real LLM calls, can take minutes) on a
background thread via `backend.game_hub.hub` so the request doesn't block;
`GET /games/{id}/live` is how a client watches it happen.
"""

import asyncio
import os
import queue
import uuid
from collections import defaultdict

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from agents.actions import FactionAction
from backend.auth import (
    get_current_user_optional,
    issue_session_token,
    upsert_user_from_google_claims,
    verify_google_id_token,
)
from backend.deps import _default_session_factory, get_session
from backend.game_hub import hub
from backend.schemas import (
    AdHocFactionIn,
    AnnotationCreate,
    AnnotationOut,
    AuthResultOut,
    DiplomaticRelationOut,
    EvalRunResultOut,
    FactionStateOut,
    GameCreate,
    GameCreatedOut,
    GameEventOut,
    GameOut,
    GameSummaryOut,
    GoogleSignInIn,
    InsightsOut,
    RolePresetMetricOut,
    ScenarioCreate,
    ScenarioOut,
    UserOut,
)
from db.models import (
    Annotation,
    DiplomaticRelation,
    DiplomaticStatus,
    EvalScore,
    FactionStateSnapshot,
    Game,
    GameEvent,
    GameFaction,
    RolePreset,
    Scenario,
    ScenarioFaction,
    User,
)
from eval.run_eval import run_eval
from game.narrative import classify_event
from game.run_game import ScenarioNotFoundError, create_game, play_game
from map_data.loader import CITIES_PATH, PROVINCES_PATH, RIVERS_PATH

app = FastAPI(title="Order Wars API")

load_dotenv()
# The frontend calls this API from a browser on a different origin (its Vite
# dev server), which needs CORS headers to
# work at all — without this middleware every request from a page would be
# silently blocked by the browser. Defaults to "*" (allow any origin) and
# allow_credentials stays False — still safe now that Google Sign-In exists
# (see backend/auth.py's module docstring): auth here is Bearer-token, not
# cookie-based, so a session token only ever travels because this app's own
# frontend JS explicitly attaches it. A forged cross-origin request can't
# read another origin's localStorage to steal it, and allow_credentials=False
# means the browser won't send/expose cookies here even if a future feature
# added one. Set CORS_ORIGINS (comma-separated) in .env to restrict this once
# a specific frontend origin is known.
_cors_origins = os.environ.get("CORS_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _cors_origins == "*" else [o.strip() for o in _cors_origins.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)

# How long a human-controlled faction's turn waits for a submitted action
# before falling back to the AI for that one turn — see
# backend/game_hub.py's await_human_action. Chosen to be long enough for a
# real person to read the board and decide, short enough that an abandoned
# tab doesn't stall a spectated game for long.
HUMAN_ACTION_TIMEOUT_SECONDS = 45


# _default_session_factory/get_session live in backend/deps.py, not here —
# backend/auth.py needs get_session too, and importing it from main.py
# would be circular (main.py imports auth.py's routes/dependencies).
# Re-imported under these same names so every existing route handler's
# `Depends(get_session)`/`Depends(_default_session_factory)` and every
# test's `app.dependency_overrides[_default_session_factory]` keep working
# unchanged — Python imports bind to the same function object, not a copy.


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/auth/google", response_model=AuthResultOut)
def sign_in_with_google(payload: GoogleSignInIn, session: Session = Depends(get_session)) -> AuthResultOut:
    """The frontend's Google Identity Services callback posts the ID token
    it got directly from Google here — this is the only place that token
    is ever seen server-side. Returns this app's own longer-lived session
    token instead of passing the Google token back, since that one expires
    in ~1 hour.
    """
    try:
        claims = verify_google_id_token(payload.id_token)
    except ValueError as exc:
        raise HTTPException(401, f"Invalid Google ID token: {exc}") from exc

    user = upsert_user_from_google_claims(session, claims)
    session_token = issue_session_token(user.id)
    return AuthResultOut(session_token=session_token, user=UserOut.model_validate(user))


@app.get("/auth/me", response_model=UserOut)
def get_current_user_info(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> User:
    user = get_current_user_optional(session, authorization)
    if user is None:
        raise HTTPException(401, "Not signed in")
    return user


@app.get("/map/provinces")
def get_provinces() -> FileResponse:
    return FileResponse(PROVINCES_PATH, media_type="application/geo+json")


@app.get("/map/rivers")
def get_rivers() -> FileResponse:
    return FileResponse(RIVERS_PATH, media_type="application/geo+json")


@app.get("/map/cities")
def get_cities() -> FileResponse:
    return FileResponse(CITIES_PATH, media_type="application/geo+json")


@app.post("/scenarios", response_model=ScenarioOut, status_code=201)
def create_scenario(
    payload: ScenarioCreate,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> ScenarioOut:
    try:
        role_presets = [RolePreset(f.role_preset) for f in payload.factions]
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    current_user = get_current_user_optional(session, authorization)
    scenario = Scenario(
        name=payload.name, max_turns=payload.max_turns, map_ref=payload.map_ref,
        owner_user_id=current_user.id if current_user else None,
    )
    for faction_in, role_preset in zip(payload.factions, role_presets, strict=True):
        scenario.factions.append(
            ScenarioFaction(
                faction_name=faction_in.faction_name,
                role_preset=role_preset,
                starting_resources=faction_in.starting_resources,
                starting_units=faction_in.starting_units,
                starting_territory=faction_in.starting_territory,
                custom_prompt=faction_in.custom_prompt,
            )
        )
    session.add(scenario)
    session.flush()
    session.refresh(scenario)
    return _scenario_out(scenario, current_user.name if current_user else None)


def _scenario_out(scenario: Scenario, owner_name: str | None) -> ScenarioOut:
    """`owner_name` isn't an ORM column on Scenario (only owner_user_id is)
    — it's resolved by the caller (a single lookup for one scenario, a
    batched one for a list) and stitched in here, the same pattern
    get_game_events already uses for notable/headline.
    """
    out = ScenarioOut.model_validate(scenario)
    out.owner_name = owner_name
    return out


@app.get("/scenarios", response_model=list[ScenarioOut])
def list_scenarios(session: Session = Depends(get_session)) -> list[ScenarioOut]:
    scenarios = session.query(Scenario).order_by(Scenario.created_at.desc()).all()
    owner_ids = {s.owner_user_id for s in scenarios if s.owner_user_id is not None}
    owner_name_by_id = {
        u.id: u.name for u in session.query(User).filter(User.id.in_(owner_ids)).all()
    } if owner_ids else {}
    return [_scenario_out(s, owner_name_by_id.get(s.owner_user_id)) for s in scenarios]


@app.get("/scenarios/{scenario_id}", response_model=ScenarioOut)
def get_scenario(scenario_id: uuid.UUID, session: Session = Depends(get_session)) -> ScenarioOut:
    scenario = session.get(Scenario, scenario_id)
    if scenario is None:
        raise HTTPException(404, "Scenario not found")
    owner = session.get(User, scenario.owner_user_id) if scenario.owner_user_id else None
    return _scenario_out(scenario, owner.name if owner else None)


def _as_faction_configs(factions: list[AdHocFactionIn]) -> list[dict]:
    return [
        {
            "faction_id": f.faction_id,
            "name": f.name,
            "role_preset": f.role_preset,
            "home_province": f.home_province,
            "starting_territory": f.starting_territory,
            "resources": f.resources,
            "units": f.units,
        }
        for f in factions
    ]


@app.post("/games", response_model=GameCreatedOut, status_code=202)
def start_game(
    payload: GameCreate,
    session_factory: sessionmaker[Session] = Depends(_default_session_factory),
) -> GameCreatedOut:
    if payload.scenario_id is not None and payload.factions is not None:
        raise HTTPException(422, "Provide scenario_id or factions, not both")

    try:
        game_id, faction_configs, db_faction_id = create_game(
            faction_configs=_as_faction_configs(payload.factions) if payload.factions else None,
            scenario_id=payload.scenario_id,
            max_turns=payload.max_turns,
            session_factory=session_factory,
        )
    except ScenarioNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        # Bad input (invalid role preset, duplicate/unreal home provinces)
        # — distinct from ScenarioNotFoundError above, which is caught
        # first since it's a ValueError subclass. Previously both fell
        # into one `except ValueError` and always returned 404, even for
        # input that had nothing to do with a missing scenario.
        raise HTTPException(422, str(exc)) from exc

    # Lets the live WebSocket handler translate a submitted action's own
    # target_faction (e.g. who to declare_war on) from the DB id the
    # frontend sends into agents/graph's internal slug — see
    # GameHub.slug_for and _handle_live_control_message.
    hub.register_factions(str(game_id), {str(v): k for k, v in db_faction_id.items()})

    def _on_event(state: dict) -> None:
        hub.broadcast(
            str(game_id),
            {
                "type": "state",
                "turn": state["turn"],
                "active_faction_idx": state["active_faction_idx"],
                "last_event": state.get("last_event"),
            },
        )

    # Live-play: lets a WebSocket client take over a faction's turns (see
    # backend/game_hub.py's control-plane methods and
    # agents.graph.set_human_action_provider). `faction_slug` is whatever
    # agents/graph.py's turn loop uses internally (e.g. "rome") — GameHub
    # itself tracks control keyed by the DB faction id instead, matching
    # every other faction reference the frontend already uses, so this is
    # the one place that needs to translate between the two id spaces.
    def _human_action_provider(faction_slug: str) -> FactionAction | None:
        faction_db_id = str(db_faction_id[faction_slug])
        if not hub.is_human_controlled(str(game_id), faction_db_id):
            return None
        return hub.await_human_action(
            str(game_id), faction_db_id, timeout=HUMAN_ACTION_TIMEOUT_SECONDS
        )

    def _play() -> None:
        play_game(
            game_id,
            faction_configs,
            db_faction_id,
            payload.max_turns,
            session_factory=session_factory,
            on_event=_on_event,
            human_action_provider=_human_action_provider,
        )

    hub.start(str(game_id), _play)
    return GameCreatedOut(game_id=game_id)


@app.get("/games", response_model=list[GameSummaryOut])
def list_games(session: Session = Depends(get_session)) -> list[Game]:
    return session.query(Game).order_by(Game.started_at.desc()).all()


@app.get("/games/{game_id}", response_model=GameOut)
def get_game(game_id: uuid.UUID, session: Session = Depends(get_session)) -> GameOut:
    game = session.get(Game, game_id)
    if game is None:
        raise HTTPException(404, "Game not found")

    faction_states = []
    for game_faction in game.factions:
        latest = (
            session.query(FactionStateSnapshot)
            .filter_by(game_id=game_id, faction_id=game_faction.id)
            .order_by(FactionStateSnapshot.turn.desc())
            .first()
        )
        if latest is not None:
            faction_states.append(
                FactionStateOut(
                    faction_id=game_faction.id,
                    faction_name=game_faction.faction_name,
                    turn=latest.turn,
                    resources=latest.resources,
                    territory=latest.territory,
                    unit_count=latest.unit_count,
                )
            )

    return GameOut(
        id=game.id,
        status=game.status.value,
        current_turn=game.current_turn,
        winner_faction_id=game.winner_faction_id,
        scenario_id=game.scenario_id,
        factions=list(game.factions),
        faction_states=faction_states,
    )


@app.get("/games/{game_id}/events", response_model=list[GameEventOut])
def get_game_events(
    game_id: uuid.UUID,
    limit: int = 100,
    offset: int = 0,
    session: Session = Depends(get_session),
) -> list[GameEventOut]:
    events = (
        session.query(GameEvent)
        .filter_by(game_id=game_id)
        .order_by(GameEvent.turn, GameEvent.created_at)
        .offset(offset)
        .limit(limit)
        .all()
    )
    # notable/headline aren't stored columns — see game.narrative's
    # docstring for why this is computed at serve time instead.
    result = []
    for event in events:
        tag = classify_event(event.event_type, event.payload)
        out = GameEventOut.model_validate(event)
        out.notable = tag.notable
        out.headline = tag.headline
        result.append(out)
    return result


@app.get("/games/{game_id}/snapshots", response_model=list[FactionStateOut])
def get_game_snapshots(game_id: uuid.UUID, session: Session = Depends(get_session)) -> list[FactionStateOut]:
    """Every turn's FactionStateSnapshot for every faction, not just the
    latest (see get_game's use of the same model) — the frontend's replay
    scrubber needs ownership/resources *at each turn* to animate the map
    while stepping through a finished game, not only its final state.
    """
    game = session.get(Game, game_id)
    if game is None:
        raise HTTPException(404, "Game not found")

    faction_name_by_id = {faction.id: faction.faction_name for faction in game.factions}
    snapshots = (
        session.query(FactionStateSnapshot)
        .filter_by(game_id=game_id)
        .order_by(FactionStateSnapshot.turn)
        .all()
    )
    return [
        FactionStateOut(
            faction_id=snapshot.faction_id,
            faction_name=faction_name_by_id.get(snapshot.faction_id, ""),
            turn=snapshot.turn,
            resources=snapshot.resources,
            territory=snapshot.territory,
            unit_count=snapshot.unit_count,
        )
        for snapshot in snapshots
    ]


@app.get("/games/{game_id}/diplomacy", response_model=list[DiplomaticRelationOut])
def get_game_diplomacy(game_id: uuid.UUID, session: Session = Depends(get_session)) -> list[DiplomaticRelationOut]:
    game = session.get(Game, game_id)
    if game is None:
        raise HTTPException(404, "Game not found")

    # One row per (pair, turn_changed) — the current status for a pair is
    # whichever row has the highest turn_changed (see DiplomaticRelation's
    # docstring). Iterating turn_changed ascending and overwriting a dict
    # keyed by the pair lands on the latest row per pair without a
    # separate max() query.
    rows = (
        session.query(DiplomaticRelation)
        .filter_by(game_id=game_id)
        .order_by(DiplomaticRelation.turn_changed)
        .all()
    )
    latest_by_pair: dict[tuple[uuid.UUID, uuid.UUID], DiplomaticRelation] = {}
    for row in rows:
        latest_by_pair[(row.faction_a_id, row.faction_b_id)] = row

    faction_names = {faction.id: faction.faction_name for faction in game.factions}
    return [
        DiplomaticRelationOut(
            faction_a_id=row.faction_a_id,
            faction_a_name=faction_names.get(row.faction_a_id, str(row.faction_a_id)),
            faction_b_id=row.faction_b_id,
            faction_b_name=faction_names.get(row.faction_b_id, str(row.faction_b_id)),
            status=row.status.value,
            turn_changed=row.turn_changed,
        )
        for row in latest_by_pair.values()
        if row.status != DiplomaticStatus.NEUTRAL
    ]


@app.get("/insights/role-presets", response_model=InsightsOut)
def get_role_preset_insights(session: Session = Depends(get_session)) -> InsightsOut:
    """Cross-game view: how does each role preset tend to score, averaged
    over every decision any faction with that preset has made in any
    evaluated game? Distinct from a single game's Review tab — this is
    what actually answers "does the Warmonger preset really play less
    legally than the Diplomat-Trader, on average?" rather than just in one
    playthrough. Python-side aggregation, not SQL GROUP BY, matching this
    project's existing precedent (e.g. _mark_eliminated_factions) for
    dataset sizes this small — not expected to need a real OLAP query.
    """
    rows = (
        session.query(GameFaction.role_preset, EvalScore.metric_name, EvalScore.score, GameEvent.game_id)
        .join(GameEvent, EvalScore.game_event_id == GameEvent.id)
        .join(GameFaction, GameEvent.faction_id == GameFaction.id)
        .all()
    )

    scores_by_key: dict[tuple[str, str], list[float]] = defaultdict(list)
    games_seen: set[uuid.UUID] = set()
    for role_preset, metric_name, score, game_id in rows:
        scores_by_key[(role_preset.value, metric_name)].append(score)
        games_seen.add(game_id)

    role_presets = [
        RolePresetMetricOut(
            role_preset=role_preset,
            metric_name=metric_name,
            avg_score=sum(scores) / len(scores),
            sample_count=len(scores),
        )
        for (role_preset, metric_name), scores in sorted(scores_by_key.items())
    ]
    return InsightsOut(games_analyzed=len(games_seen), role_presets=role_presets)


@app.post("/games/{game_id}/evaluate", response_model=list[EvalRunResultOut])
def evaluate_game(
    game_id: uuid.UUID,
    session_factory: sessionmaker[Session] = Depends(_default_session_factory),
) -> list[dict]:
    """Runs synchronously, unlike POST /games — DeepEval calls against a
    short game's handful of events take a few seconds, not the minutes a
    full game loop can take, so there's no need for GameHub's background-
    thread treatment here. Would need it if games/eval runs grow much
    longer than this project's current scale.
    """
    try:
        return run_eval(game_id, session_factory=session_factory)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/events/{event_id}/annotations", response_model=AnnotationOut, status_code=201)
def create_annotation(
    event_id: uuid.UUID,
    payload: AnnotationCreate,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> Annotation:
    if payload.rating is None and not payload.note:
        raise HTTPException(422, "Provide a rating, a note, or both")
    if session.get(GameEvent, event_id) is None:
        raise HTTPException(404, "Event not found")

    # A signed-in user's real name always wins over whatever the client
    # sent for created_by — that field exists for genuine attribution, and
    # letting a signed-in request's body claim a different name would be a
    # spoofing hole for no real benefit. An anonymous request (no valid
    # session) keeps working exactly as before this feature existed.
    current_user = get_current_user_optional(session, authorization)
    created_by = current_user.name if current_user else payload.created_by

    annotation = Annotation(
        game_event_id=event_id,
        rating=payload.rating,
        note=payload.note,
        created_by=created_by,
    )
    session.add(annotation)
    session.flush()
    session.refresh(annotation)
    return annotation


def _queue_get_or_none(q: queue.Queue, timeout: float):
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return None


def _handle_live_control_message(game_id: str, data: dict) -> None:
    """A client taking control of a faction, releasing it, or submitting an
    action for their turn — the only messages this endpoint ever receives;
    everything else is server -> client only. `faction_id` throughout is
    the DB faction id (a string uuid), matching every other faction
    reference the frontend already uses — see start_game's
    _human_action_provider for where that gets translated to agents/'s
    internal slug id space.
    """
    message_type = data.get("type")
    faction_id = data.get("faction_id")
    if not faction_id:
        return

    if message_type == "take_control":
        hub.take_control(game_id, faction_id)
        hub.broadcast(game_id, {"type": "control_changed", "faction_id": faction_id, "human_controlled": True})
    elif message_type == "release_control":
        hub.release_control(game_id, faction_id)
        hub.broadcast(game_id, {"type": "control_changed", "faction_id": faction_id, "human_controlled": False})
    elif message_type == "submit_action":
        try:
            action = FactionAction(**(data.get("action") or {}))
        except ValidationError:
            return  # malformed submission from a client — dropped, not a server error
        if action.target_faction:
            # The client only ever knows other factions by DB id (same as
            # everywhere else in the UI) — translate to the slug
            # agents/graph.py's resolve_action/_sanitize_action expect. An
            # unknown id (e.g. register_factions hasn't run yet, or a bogus
            # value) is left as-is and simply fails sanitization the same
            # way an AI hallucinating a bad target would — no special case
            # needed here.
            slug = hub.slug_for(game_id, action.target_faction)
            if slug is not None:
                action = action.model_copy(update={"target_faction": slug})
        hub.submit_action(game_id, faction_id, action)


@app.websocket("/games/{game_id}/live")
async def game_live(websocket: WebSocket, game_id: str) -> None:
    """Streams `{"type": "state", ...}` messages as the game plays, then a
    final `{"type": "stream_end"}` (or `{"type": "error", ...}` first, if
    `play_game` raised). Subscribing to a game id nothing is currently
    playing just waits — it's not an error, the game may start later.

    Also accepts messages from the client (see _handle_live_control_message)
    — this is the live-play control channel, so unlike every other endpoint
    here this one is genuinely bidirectional. Sending and receiving run as
    two concurrent tasks since Starlette's WebSocket has no single call that
    does both; whichever finishes first (normally the receiver, on
    disconnect) ends the connection.
    """
    await websocket.accept()
    subscription = hub.subscribe(game_id)
    # A client joining after control was already claimed has no other way
    # to learn that — GameHub keeps no broadcast backlog (see subscribe's
    # docstring precedent for the same limitation on game state itself) —
    # so send a snapshot of current control state right away.
    await websocket.send_json(
        {"type": "control_state", "controlled_factions": sorted(hub.controlled_factions(game_id))}
    )

    async def _forward_broadcasts() -> None:
        # A bounded queue.get, not a bare blocking one: asyncio.to_thread
        # hands the call to a thread-pool worker, and cancelling the
        # awaiting Task does NOT interrupt an already-running executor
        # call — a subscriber that never receives anything (e.g. it
        # connected after the game already finished and was forgotten;
        # GameHub keeps no backlog) would otherwise block this forever,
        # and the `finally` below's cleanup would hang waiting for it to
        # resolve. Polling with a short timeout instead means a cancelled
        # task actually unblocks within one poll interval. Caught by a new
        # test hanging, not by inspection.
        while True:
            message = await asyncio.to_thread(_queue_get_or_none, subscription, 0.5)
            if message is None:
                continue
            await websocket.send_json(message)
            if message.get("type") in ("stream_end", "error"):
                break

    async def _receive_control_messages() -> None:
        try:
            while True:
                data = await websocket.receive_json()
                _handle_live_control_message(game_id, data)
        except WebSocketDisconnect:
            pass

    forward_task = asyncio.create_task(_forward_broadcasts())
    receive_task = asyncio.create_task(_receive_control_messages())
    try:
        await asyncio.wait({forward_task, receive_task}, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for task in (forward_task, receive_task):
            task.cancel()
        await asyncio.gather(forward_task, receive_task, return_exceptions=True)
        hub.unsubscribe(game_id, subscription)
