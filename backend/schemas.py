"""API request/response models — deliberately separate from db/models.py.

The ORM models are the persistence schema; these are the wire contract.
Keeping them apart means a column rename or a new internal field in
db/models.py doesn't silently change the API, and vice versa.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from game.run_game import MAX_TURNS_LIMIT


class ScenarioFactionIn(BaseModel):
    faction_name: str
    role_preset: str = "custom"
    starting_resources: dict[str, int] = Field(default_factory=lambda: {"gold": 20})
    starting_units: dict[str, int] = Field(default_factory=lambda: {"legion": 2})
    starting_territory: list[str] = Field(
        min_length=1, description="Real province ids from map_data/provinces.geojson"
    )
    custom_prompt: str | None = None


class ScenarioCreate(BaseModel):
    name: str
    max_turns: int = Field(default=10, ge=1, le=MAX_TURNS_LIMIT)
    map_ref: str | None = None
    factions: list[ScenarioFactionIn] = Field(min_length=1)


class ScenarioFactionOut(BaseModel):
    id: uuid.UUID
    faction_name: str
    role_preset: str
    starting_resources: dict
    starting_units: dict
    starting_territory: list[str]

    model_config = {"from_attributes": True}


class ScenarioOut(BaseModel):
    id: uuid.UUID
    name: str
    max_turns: int
    map_ref: str | None
    factions: list[ScenarioFactionOut]

    model_config = {"from_attributes": True}


class AdHocFactionIn(BaseModel):
    faction_id: str
    name: str
    role_preset: str = "custom"
    home_province: str
    resources: dict[str, int] | None = None
    units: dict[str, int] | None = None


class GameCreate(BaseModel):
    """Exactly one of `scenario_id` / `factions` should be set; if neither
    is, the game falls back to the built-in Rome/Carthage/Gaul demo.
    """

    scenario_id: uuid.UUID | None = None
    factions: list[AdHocFactionIn] | None = None
    max_turns: int = Field(default=10, ge=1, le=MAX_TURNS_LIMIT)


class GameCreatedOut(BaseModel):
    game_id: uuid.UUID


class GameFactionOut(BaseModel):
    id: uuid.UUID
    faction_name: str
    role_preset: str
    is_alive: bool
    eliminated_at_turn: int | None

    model_config = {"from_attributes": True}


class FactionStateOut(BaseModel):
    """A faction's latest known state within a game — derived from its most
    recent FactionStateSnapshot, not stored directly.
    """

    faction_id: uuid.UUID
    faction_name: str
    turn: int
    resources: dict
    territory: list[str]
    unit_count: int


class GameOut(BaseModel):
    id: uuid.UUID
    status: str
    current_turn: int
    winner_faction_id: uuid.UUID | None
    scenario_id: uuid.UUID | None
    factions: list[GameFactionOut]
    faction_states: list[FactionStateOut]

    model_config = {"from_attributes": True}


class GameSummaryOut(BaseModel):
    id: uuid.UUID
    status: str
    current_turn: int
    winner_faction_id: uuid.UUID | None

    model_config = {"from_attributes": True}


class EvalScoreOut(BaseModel):
    metric_name: str
    score: float
    success: bool
    reason: str | None

    model_config = {"from_attributes": True}


class AnnotationOut(BaseModel):
    id: uuid.UUID
    rating: int | None
    note: str | None
    created_by: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class AnnotationCreate(BaseModel):
    """A human reviewer's note on one GameEvent. All fields optional (but
    not all empty — see the route) since a reviewer might rate without
    writing anything, or vice versa.
    """

    rating: int | None = Field(default=None, ge=1, le=5)
    note: str | None = None
    created_by: str | None = None


class GameEventOut(BaseModel):
    id: uuid.UUID
    turn: int
    faction_id: uuid.UUID | None
    event_type: str
    payload: dict
    eval_scores: list[EvalScoreOut] = []
    annotations: list[AnnotationOut] = []

    model_config = {"from_attributes": True}


class EvalRunResultOut(BaseModel):
    turn: int
    faction_name: str
    metric_name: str
    score: float
    success: bool | None
