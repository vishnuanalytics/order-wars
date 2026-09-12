"""initial schema

Revision ID: 157d15fc6345
Revises:
Create Date: 2026-09-12 14:31:20.825632

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '157d15fc6345'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Postgres ENUM types are created/dropped explicitly (checkfirst=True) rather
# than relying on op.create_table/drop_table to manage them implicitly:
# dropping a table does NOT drop a named ENUM type it referenced (it may be
# shared, as role_preset is here), so autogenerate's default downgrade left
# these types behind and a downgrade -> upgrade cycle failed with
# "type already exists". create_type=False on each column below stops
# SQLAlchemy from redundantly trying to create/check the type again per-table.
game_status_enum = postgresql.ENUM(
    'PENDING', 'RUNNING', 'COMPLETED', 'FAILED', name='game_status'
)
role_preset_enum = postgresql.ENUM(
    'EXPANSIONIST', 'WARMONGER', 'DIPLOMAT_TRADER', 'ISOLATIONIST', 'CUSTOM',
    name='role_preset',
)
diplomatic_status_enum = postgresql.ENUM(
    'NEUTRAL', 'WAR', 'TRUCE', 'ALLIANCE', name='diplomatic_status'
)


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    game_status_enum.create(bind, checkfirst=True)
    role_preset_enum.create(bind, checkfirst=True)
    diplomatic_status_enum.create(bind, checkfirst=True)

    op.create_table('scenarios',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('map_ref', sa.String(length=200), nullable=True),
    sa.Column('max_turns', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('games',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('scenario_id', sa.Uuid(), nullable=True),
    sa.Column('status', postgresql.ENUM('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', name='game_status', create_type=False), nullable=False),
    sa.Column('current_turn', sa.Integer(), nullable=False),
    sa.Column('winner_faction_id', sa.Uuid(), nullable=True),
    sa.Column('config_snapshot', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('started_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.Column('ended_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['scenario_id'], ['scenarios.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_games_scenario_id'), 'games', ['scenario_id'], unique=False)
    op.create_table('scenario_factions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('scenario_id', sa.Uuid(), nullable=False),
    sa.Column('faction_name', sa.String(length=100), nullable=False),
    sa.Column('role_preset', postgresql.ENUM('EXPANSIONIST', 'WARMONGER', 'DIPLOMAT_TRADER', 'ISOLATIONIST', 'CUSTOM', name='role_preset', create_type=False), nullable=False),
    sa.Column('starting_resources', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('starting_units', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('starting_territory', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('custom_prompt', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['scenario_id'], ['scenarios.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_scenario_factions_scenario_id'), 'scenario_factions', ['scenario_id'], unique=False)
    op.create_table('game_factions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('game_id', sa.Uuid(), nullable=False),
    sa.Column('faction_name', sa.String(length=100), nullable=False),
    sa.Column('role_preset', postgresql.ENUM('EXPANSIONIST', 'WARMONGER', 'DIPLOMAT_TRADER', 'ISOLATIONIST', 'CUSTOM', name='role_preset', create_type=False), nullable=False),
    sa.Column('is_alive', sa.Boolean(), nullable=False),
    sa.Column('eliminated_at_turn', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['game_id'], ['games.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_game_factions_game_id'), 'game_factions', ['game_id'], unique=False)
    # games.winner_faction_id -> game_factions.id and game_factions.game_id ->
    # games.id are mutually referential, so this FK is added after both
    # tables exist rather than inline on games (use_alter=True on the model
    # side documents the intent, but op.create_table doesn't act on it —
    # only MetaData.create_all()'s dependency sort does).
    op.create_foreign_key(
        'games_winner_faction_id_fkey', 'games', 'game_factions',
        ['winner_faction_id'], ['id'], ondelete='SET NULL',
    )
    op.create_table('diplomatic_relations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('game_id', sa.Uuid(), nullable=False),
    sa.Column('faction_a_id', sa.Uuid(), nullable=False),
    sa.Column('faction_b_id', sa.Uuid(), nullable=False),
    sa.Column('status', postgresql.ENUM('NEUTRAL', 'WAR', 'TRUCE', 'ALLIANCE', name='diplomatic_status', create_type=False), nullable=False),
    sa.Column('turn_changed', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['faction_a_id'], ['game_factions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['faction_b_id'], ['game_factions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['game_id'], ['games.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_diplomatic_relations_game_id'), 'diplomatic_relations', ['game_id'], unique=False)
    op.create_table('faction_state_snapshots',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('game_id', sa.Uuid(), nullable=False),
    sa.Column('faction_id', sa.Uuid(), nullable=False),
    sa.Column('turn', sa.Integer(), nullable=False),
    sa.Column('resources', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('territory_count', sa.Integer(), nullable=False),
    sa.Column('unit_count', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['faction_id'], ['game_factions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['game_id'], ['games.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('game_id', 'faction_id', 'turn', name='uq_snapshot_game_faction_turn')
    )
    op.create_index(op.f('ix_faction_state_snapshots_faction_id'), 'faction_state_snapshots', ['faction_id'], unique=False)
    op.create_index(op.f('ix_faction_state_snapshots_game_id'), 'faction_state_snapshots', ['game_id'], unique=False)
    op.create_index(op.f('ix_faction_state_snapshots_turn'), 'faction_state_snapshots', ['turn'], unique=False)
    op.create_table('game_events',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('game_id', sa.Uuid(), nullable=False),
    sa.Column('turn', sa.Integer(), nullable=False),
    sa.Column('faction_id', sa.Uuid(), nullable=True),
    sa.Column('event_type', sa.String(length=50), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['faction_id'], ['game_factions.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['game_id'], ['games.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_game_events_event_type'), 'game_events', ['event_type'], unique=False)
    op.create_index(op.f('ix_game_events_game_id'), 'game_events', ['game_id'], unique=False)
    op.create_index(op.f('ix_game_events_turn'), 'game_events', ['turn'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('games_winner_faction_id_fkey', 'games', type_='foreignkey')
    op.drop_index(op.f('ix_game_events_turn'), table_name='game_events')
    op.drop_index(op.f('ix_game_events_game_id'), table_name='game_events')
    op.drop_index(op.f('ix_game_events_event_type'), table_name='game_events')
    op.drop_table('game_events')
    op.drop_index(op.f('ix_faction_state_snapshots_turn'), table_name='faction_state_snapshots')
    op.drop_index(op.f('ix_faction_state_snapshots_game_id'), table_name='faction_state_snapshots')
    op.drop_index(op.f('ix_faction_state_snapshots_faction_id'), table_name='faction_state_snapshots')
    op.drop_table('faction_state_snapshots')
    op.drop_index(op.f('ix_diplomatic_relations_game_id'), table_name='diplomatic_relations')
    op.drop_table('diplomatic_relations')
    op.drop_index(op.f('ix_game_factions_game_id'), table_name='game_factions')
    op.drop_table('game_factions')
    op.drop_index(op.f('ix_scenario_factions_scenario_id'), table_name='scenario_factions')
    op.drop_table('scenario_factions')
    op.drop_index(op.f('ix_games_scenario_id'), table_name='games')
    op.drop_table('games')
    op.drop_table('scenarios')

    bind = op.get_bind()
    game_status_enum.drop(bind, checkfirst=True)
    role_preset_enum.drop(bind, checkfirst=True)
    diplomatic_status_enum.drop(bind, checkfirst=True)
