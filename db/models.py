"""SQLAlchemy ORM models for the Phase 5/6 persistence schema.

Design decisions (see CLAUDE.md "Persistence (Neon Postgres)" for the why):
  - Postgres is the sole source of truth for game history — no flat-file logs.
  - Scenario edits overwrite in place; `Game.config_snapshot` freezes the
    scenario config at run time so past runs stay reproducible.
  - `starting_resources` / `starting_units` / `starting_territory` are JSON,
    not typed columns — Phase 4 hasn't fixed what a "unit" or "resource" is
    yet, so this avoids a premature migration.
  - `event_type` on `GameEvent` is a plain indexed string rather than a
    Postgres ENUM — this set will grow through Phases 2-5 and ENUM values are
    awkward to add later; role/status fields below are genuinely fixed sets,
    so those do use native enums.

Tables are grouped: config-time (Scenario, ScenarioFaction) vs. run-time
(everything else, keyed off Game).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import Enum as SAEnum
from sqlalchemy.types import Uuid

# Generic JSON that becomes JSONB on Postgres but still works on SQLite,
# which the test suite uses so tests don't need a live database.
JSONVariant = JSON().with_variant(JSONB, "postgresql")


class RolePreset(str, enum.Enum):
    """Agent behavior profiles a faction can be assigned in the scenario editor."""

    EXPANSIONIST = "expansionist"
    WARMONGER = "warmonger"
    DIPLOMAT_TRADER = "diplomat_trader"
    ISOLATIONIST = "isolationist"
    CUSTOM = "custom"


class GameStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class DiplomaticStatus(str, enum.Enum):
    NEUTRAL = "neutral"
    WAR = "war"
    TRUCE = "truce"
    ALLIANCE = "alliance"


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


# --------------------------------------------------------------------------
# Config-time: what the scenario-editor UI writes, before a run exists.
# --------------------------------------------------------------------------


class Scenario(Base):
    """A saved, reusable setup: map, turn limit, and its factions."""

    __tablename__ = "scenarios"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    map_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    max_turns: Mapped[int] = mapped_column(default=50)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    factions: Mapped[list[ScenarioFaction]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    games: Mapped[list[Game]] = relationship(back_populates="scenario")

    def __repr__(self) -> str:
        return f"Scenario(id={self.id!r}, name={self.name!r})"


class ScenarioFaction(Base):
    """One faction's starting configuration within a scenario."""

    __tablename__ = "scenario_factions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scenario_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scenarios.id", ondelete="CASCADE"), index=True
    )
    faction_name: Mapped[str] = mapped_column(String(100))
    role_preset: Mapped[RolePreset] = mapped_column(
        SAEnum(RolePreset, name="role_preset"), default=RolePreset.CUSTOM
    )
    # Flexible on purpose — see module docstring.
    starting_resources: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    starting_units: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    starting_territory: Mapped[list] = mapped_column(JSONVariant, default=list)
    custom_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)

    scenario: Mapped[Scenario] = relationship(back_populates="factions")

    def __repr__(self) -> str:
        return f"ScenarioFaction(id={self.id!r}, faction_name={self.faction_name!r})"


# --------------------------------------------------------------------------
# Run-time: created when the user hits "Run"; keyed off Game.
# --------------------------------------------------------------------------


class Game(Base):
    """One playthrough of a scenario (or an ad-hoc run with no saved scenario)."""

    __tablename__ = "games"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scenarios.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[GameStatus] = mapped_column(
        SAEnum(GameStatus, name="game_status"), default=GameStatus.PENDING
    )
    current_turn: Mapped[int] = mapped_column(default=0)
    winner_faction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("game_factions.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    # Frozen copy of the scenario config at run start, so editing the saved
    # scenario afterward never changes what a past run actually used.
    config_snapshot: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True)

    scenario: Mapped[Scenario | None] = relationship(back_populates="games")
    factions: Mapped[list[GameFaction]] = relationship(
        back_populates="game",
        cascade="all, delete-orphan",
        foreign_keys="GameFaction.game_id",
    )
    events: Mapped[list[GameEvent]] = relationship(
        back_populates="game", cascade="all, delete-orphan"
    )
    snapshots: Mapped[list[FactionStateSnapshot]] = relationship(
        back_populates="game", cascade="all, delete-orphan"
    )
    diplomatic_relations: Mapped[list[DiplomaticRelation]] = relationship(
        back_populates="game", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"Game(id={self.id!r}, status={self.status!r}, turn={self.current_turn})"


class GameFaction(Base):
    """A faction instance within one specific run (the live, mutable copy)."""

    __tablename__ = "game_factions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    game_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE"), index=True
    )
    faction_name: Mapped[str] = mapped_column(String(100))
    role_preset: Mapped[RolePreset] = mapped_column(
        SAEnum(RolePreset, name="role_preset")
    )
    is_alive: Mapped[bool] = mapped_column(default=True)
    eliminated_at_turn: Mapped[int | None] = mapped_column(nullable=True)

    game: Mapped[Game] = relationship(back_populates="factions", foreign_keys=[game_id])

    def __repr__(self) -> str:
        return f"GameFaction(id={self.id!r}, faction_name={self.faction_name!r})"


class GameEvent(Base):
    """Tick-by-tick action log — the Postgres replacement for flat-file logs."""

    __tablename__ = "game_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    game_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE"), index=True
    )
    turn: Mapped[int] = mapped_column(index=True)
    # Nullable: some events (e.g. a global tick marker) aren't faction-scoped.
    faction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("game_factions.id", ondelete="SET NULL"), nullable=True
    )
    # Plain indexed string, not an enum — see module docstring.
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    payload: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    game: Mapped[Game] = relationship(back_populates="events")
    eval_scores: Mapped[list[EvalScore]] = relationship(
        back_populates="game_event", cascade="all, delete-orphan"
    )
    annotations: Mapped[list[Annotation]] = relationship(
        back_populates="game_event", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"GameEvent(game_id={self.game_id!r}, turn={self.turn}, event_type={self.event_type!r})"


class FactionStateSnapshot(Base):
    """Per-turn resource/territory/unit totals per faction, for cheap UI charting."""

    __tablename__ = "faction_state_snapshots"
    __table_args__ = (
        UniqueConstraint("game_id", "faction_id", "turn", name="uq_snapshot_game_faction_turn"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    game_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE"), index=True
    )
    faction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("game_factions.id", ondelete="CASCADE"), index=True
    )
    turn: Mapped[int] = mapped_column(index=True)
    resources: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    territory_count: Mapped[int] = mapped_column(default=0)
    # Added in Phase 5, alongside territory_count: the count alone can't
    # render a map — the frontend needs to know *which* provinces, not just
    # how many. The latest snapshot per faction is enough to reconstruct
    # full board ownership without replaying the entire game_events history.
    territory: Mapped[list] = mapped_column(JSONVariant, default=list)
    unit_count: Mapped[int] = mapped_column(default=0)

    game: Mapped[Game] = relationship(back_populates="snapshots")

    def __repr__(self) -> str:
        return f"FactionStateSnapshot(game_id={self.game_id!r}, faction_id={self.faction_id!r}, turn={self.turn})"


class DiplomaticRelation(Base):
    """Pairwise status between two factions in one game, with change history.

    One row per (game, faction_a, faction_b, turn_changed) — the current
    status for a pair is the row with the highest `turn_changed`. Callers
    should insert `faction_a_id`/`faction_b_id` in a consistent order (e.g.
    sorted by UUID) so a pair isn't tracked as two independent relations.
    """

    __tablename__ = "diplomatic_relations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    game_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE"), index=True
    )
    faction_a_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("game_factions.id", ondelete="CASCADE")
    )
    faction_b_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("game_factions.id", ondelete="CASCADE")
    )
    status: Mapped[DiplomaticStatus] = mapped_column(
        SAEnum(DiplomaticStatus, name="diplomatic_status"), default=DiplomaticStatus.NEUTRAL
    )
    turn_changed: Mapped[int] = mapped_column(default=0)

    game: Mapped[Game] = relationship(back_populates="diplomatic_relations")

    def __repr__(self) -> str:
        return (
            f"DiplomaticRelation(game_id={self.game_id!r}, "
            f"{self.faction_a_id!r}<->{self.faction_b_id!r}, status={self.status!r})"
        )


# --------------------------------------------------------------------------
# Phase 6: eval + annotation. Scored/annotated per GameEvent (per decision),
# not per game — that's the natural grain for "was this specific choice
# good," and still supports per-game aggregates (average score, legality
# rate) by joining back through GameEvent.game_id.
# --------------------------------------------------------------------------


class EvalScore(Base):
    """One DeepEval metric's result for one GameEvent (one faction's one
    decision). `metric_name` is a plain string, not an enum — like
    `GameEvent.event_type`, the set of metrics is expected to grow.
    """

    __tablename__ = "eval_scores"

    id: Mapped[uuid.UUID] = _uuid_pk()
    game_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("game_events.id", ondelete="CASCADE"), index=True
    )
    metric_name: Mapped[str] = mapped_column(String(100), index=True)
    score: Mapped[float] = mapped_column()
    success: Mapped[bool] = mapped_column()
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    game_event: Mapped[GameEvent] = relationship(back_populates="eval_scores")

    def __repr__(self) -> str:
        return f"EvalScore(game_event_id={self.game_event_id!r}, metric_name={self.metric_name!r}, score={self.score!r})"


class Annotation(Base):
    """A human reviewer's note on one GameEvent — the "simple annotation UI
    for human review" CLAUDE.md's Phase 6 calls for. `created_by` is a plain
    free-text field, not a user FK: this project has no auth system (see
    Non-goals), so it's whatever the reviewer typed, not an enforced identity.
    """

    __tablename__ = "annotations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    game_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("game_events.id", ondelete="CASCADE"), index=True
    )
    rating: Mapped[int | None] = mapped_column(nullable=True)  # e.g. 1-5, reviewer's own scale
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    game_event: Mapped[GameEvent] = relationship(back_populates="annotations")

    def __repr__(self) -> str:
        return f"Annotation(game_event_id={self.game_event_id!r}, rating={self.rating!r})"
