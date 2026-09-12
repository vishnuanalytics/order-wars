"""Phase 5 FastAPI backend (`uvicorn backend.main:app --reload`).

Thin HTTP/WebSocket layer over `game/run_game.py` and `db/models.py` — no
game logic lives here. `POST /games` returns as soon as the DB rows exist
(`game.run_game.create_game`, a handful of fast writes), then runs the
actual turn loop (`play_game` — real LLM calls, can take minutes) on a
background thread via `backend.game_hub.hub` so the request doesn't block;
`GET /games/{id}/live` is how a client watches it happen.
"""

import asyncio
import uuid

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, sessionmaker

from backend.game_hub import hub
from backend.schemas import (
    AdHocFactionIn,
    FactionStateOut,
    GameCreate,
    GameCreatedOut,
    GameEventOut,
    GameOut,
    GameSummaryOut,
    ScenarioCreate,
    ScenarioOut,
)
from db.models import FactionStateSnapshot, Game, GameEvent, RolePreset, Scenario, ScenarioFaction
from db.session import get_sessionmaker
from game.run_game import ScenarioNotFoundError, create_game, play_game
from map_data.loader import PROVINCES_PATH

app = FastAPI(title="Order Wars API")


def _default_session_factory() -> sessionmaker[Session]:
    return get_sessionmaker()


def get_session(
    session_factory: sessionmaker[Session] = Depends(_default_session_factory),
):
    """Per-request read/write session — commits on success, always closes.
    `create_game`/`play_game` below manage their own short-lived sessions
    instead of reusing this one, since they outlive a single request.
    """
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/map/provinces")
def get_provinces() -> FileResponse:
    return FileResponse(PROVINCES_PATH, media_type="application/geo+json")


@app.post("/scenarios", response_model=ScenarioOut, status_code=201)
def create_scenario(payload: ScenarioCreate, session: Session = Depends(get_session)) -> Scenario:
    try:
        role_presets = [RolePreset(f.role_preset) for f in payload.factions]
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    scenario = Scenario(name=payload.name, max_turns=payload.max_turns, map_ref=payload.map_ref)
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
    return scenario


@app.get("/scenarios", response_model=list[ScenarioOut])
def list_scenarios(session: Session = Depends(get_session)) -> list[Scenario]:
    return session.query(Scenario).order_by(Scenario.created_at.desc()).all()


@app.get("/scenarios/{scenario_id}", response_model=ScenarioOut)
def get_scenario(scenario_id: uuid.UUID, session: Session = Depends(get_session)) -> Scenario:
    scenario = session.get(Scenario, scenario_id)
    if scenario is None:
        raise HTTPException(404, "Scenario not found")
    return scenario


def _as_faction_configs(factions: list[AdHocFactionIn]) -> list[dict]:
    return [
        {
            "faction_id": f.faction_id,
            "name": f.name,
            "role_preset": f.role_preset,
            "home_province": f.home_province,
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

    def _play() -> None:
        play_game(
            game_id,
            faction_configs,
            db_faction_id,
            payload.max_turns,
            session_factory=session_factory,
            on_event=_on_event,
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
) -> list[GameEvent]:
    return (
        session.query(GameEvent)
        .filter_by(game_id=game_id)
        .order_by(GameEvent.turn, GameEvent.created_at)
        .offset(offset)
        .limit(limit)
        .all()
    )


@app.websocket("/games/{game_id}/live")
async def game_live(websocket: WebSocket, game_id: str) -> None:
    """Streams `{"type": "state", ...}` messages as the game plays, then a
    final `{"type": "stream_end"}` (or `{"type": "error", ...}` first, if
    `play_game` raised). Subscribing to a game id nothing is currently
    playing just waits — it's not an error, the game may start later.
    """
    await websocket.accept()
    subscription = hub.subscribe(game_id)
    try:
        while True:
            message = await asyncio.to_thread(subscription.get)
            await websocket.send_json(message)
            if message.get("type") in ("stream_end", "error"):
                break
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(game_id, subscription)
