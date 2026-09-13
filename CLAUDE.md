# CLAUDE.md — Order Wars

Guidance for Claude Code when working in this repository.

## What this project is

A multi-agent strategy/war-game simulation. The end goal is a "Total War meets
Civilization" simulation: an arbitrary number of factions (agents), each with
a user-assignable role/personality, expand territory, manage trade, form and
break truces/alliances, and fight over a real-world-derived map — configurable
and replayable from a UI, evaluated with DeepEval and improved via human
annotation.

**This is a production-oriented build.** Prioritize working, well-structured,
maintainable code over lesson pacing. Move efficiently through phases, use
sound engineering judgment, and don't hold back on completeness or cleverness
where it genuinely helps the system — but keep code readable and documented so
it stays maintainable as it grows.

## Agent & simulation design

Most decisions below shaped Phases 2, 4, and 5 as forward-looking design
intent at the time this section was written; the hierarchical-agents bullet
is now built (see "Progress log" for the multi-level agent hierarchy entry)
— new work should build toward the shape described here rather than a
simpler one that needs retrofitting later.

- **N factions, not two.** `GameState` and the graph must be built to scale to
  an arbitrary, user-configured number of factions, not hardcoded to a pair.
  Phase 2's "two+ agents" should be implemented as N from the start — cheap
  now, expensive to retrofit later.
- **Hierarchical agents per faction, not one monolithic decision-maker.**
  Built as: a **strategic leader** (`agents/graph.py`'s `_refresh_intent`)
  sets intent every few turns, not every tick, and a rule-based
  **dispatcher** (`_dispatch_specialist`, no LLM call) routes each turn's
  actual decision to one of three specialists executing that intent — a
  **military commander** (troop movement/sieges), a **diplomat/trade
  agent** (negotiation, war), and an **economic/logistics agent** (resource
  allocation, production) — rather than a single LLM call deciding
  everything. Each specialist has its own Pydantic schema restricting it to
  only its own domain's actions (`agents/actions.py`'s `MilitaryAction`/
  `DiplomaticAction`/`EconomicAction`), not just a prompt-level request.

  Two deliberate simplifications from the fuller vision, chosen when this
  was built (session cost/complexity tradeoff, not a technical limit): (1)
  **one dispatched decision per turn, not three parallel ones** — the
  dispatcher picks a single specialist to act (prioritizing an in-progress
  siege, then an incoming proposal, then a role-preset-ordered rotation —
  see `agents/roles.py`'s `ROLE_PRESET_SPECIALIST_ORDER`), so LLM call
  volume per turn is unchanged from the single-executor version, not
  tripled; (2) **diplomat agents still negotiate via the existing
  structured propose/accept `negotiate` action**, not live multi-message
  agent-to-agent dialogue — "agent-to-agent, not narrator-mediated" below
  is satisfied in the sense that the diplomat specialist (not a general
  executor) originates the action, but not in the sense of a back-and-forth
  conversation.

  This is still a planner/executor split: intent changes rarely, execution
  happens every turn within that intent. Re-planning is not yet
  event-driven (`_refresh_intent`'s cadence is still a fixed interval, not
  triggered by a deviation from expected state) — that refinement wasn't
  part of this pass, only the specialist split was.
- **Actions are structured tool calls, not free text.** Decision nodes must
  call schema-validated tools (e.g. `move_army`, `propose_trade`,
  `build_unit`) that mutate `GameState` directly, rather than emitting a
  decorative sentence like Phase 1's `last_decision`. Every pattern below
  depends on actions being structured enough for the game loop and other
  agents to react to.
- **Agent role presets, selectable per faction.** A small library of behavior
  profiles (e.g. Expansionist, Warmonger, Diplomat-Trader, Isolationist) —
  different system prompts / decision weights — that a user (via the Phase 5
  UI) or a config file can assign per faction, plus the ability to add
  factions dynamically rather than a fixed roster.
- **Diplomacy and coalitions are rule-triggered, not purely emergent.**
  Reliable emergent balance-of-power behavior from open-ended LLM reasoning
  alone is a known-hard problem — don't expect it to appear unscaffolded.
  Instead: game logic computes relative power (territory/units/resources) and
  *triggers* negotiation windows when it's lopsided (e.g. one faction now
  outweighs a plausible coalition of its neighbors); LLM diplomat agents
  handle the negotiation content and accept/counter/reject *within* that
  triggered window. Diplomatic status (war / truce / alliance) between every
  pair of factions is state, not narrative — it belongs in `GameState`
  alongside territory and resources once Phase 4 defines the schema.

## Persistence (Neon Postgres)

The ORM models, Alembic migration, and connection layer (`db/models.py`,
`db/migrations/`, `db/session.py`) are implemented and, as of Phase 5,
actually wired up and verified against the real Neon database in
`DATABASE_URL` — `db/session.py`'s `session_scope()` does real reads/writes.

- **Credential note:** `NEON_API_KEY` (already in `.env`) is Neon's
  management/control-plane API key (create/list projects & branches) — it is
  *not* a database connection string. Actually reading/writing rows needs a
  separate Postgres connection string, conventionally `DATABASE_URL`.
- **Schema isolation:** the Neon database behind `DATABASE_URL` is shared
  with unrelated projects — verified by inspection: it already held tables
  like `documents`, `materials`, `up_orders`, plus `auth`/`pgrst`/`neon_auth`
  schemas from other tooling entirely. Every Order Wars table therefore
  lives under a dedicated `order_wars` Postgres schema (`db/session.py`'s
  `DB_SCHEMA`), applied via SQLAlchemy's `schema_translate_map` at the
  engine level rather than hardcoded onto the models — so `db/models.py` and
  the SQLite-backed tests stay schema-agnostic, and only a real Postgres
  connection gets redirected. `db/migrations/env.py` creates the schema
  (`CREATE SCHEMA IF NOT EXISTS order_wars`, committed as its own explicit
  transaction — see the code comment for a real bug this caught: a
  SQLAlchemy 2.0 "begin once" connection auto-begins a transaction on the
  first raw `execute()`, and leaving that open for Alembic's
  `context.begin_transaction()` to inherit made Alembic treat it as
  externally managed and silently never commit — the *entire* migration,
  schema included, rolled back on connection close with no error raised;
  this bit twice, since a second raw statement added later, `SET search_path`,
  reintroduced the exact same uncommitted-transaction problem and needed its
  own explicit `.commit()` too) and puts `alembic_version` there too, so the
  whole thing is self-contained and `public`/other projects' tables are
  never touched. `schema_translate_map` only affects SQL Core-compiled
  DDL/DML, not Alembic's raw information_schema reflection queries used for
  autogenerate comparison (those follow the connection's actual default
  schema, i.e. Postgres's `search_path`) — without also setting
  `search_path` to `order_wars`, autogenerate compared against `public` and
  crashed trying to reflect unrelated tables it couldn't handle (e.g.
  `up_orders`, `weather_snapshots`). A custom `include_name` filter is also
  needed once you touch this at all — supplying one apparently suppresses
  Alembic's normal default protection for its own `alembic_version` table,
  which showed up as a proposed (never run) `op.drop_table('alembic_version')`
  in an autogenerate diff.
- **Postgres is the source of truth for game history — flat-file
  `logs/<game_id>/` is dropped, not mirrored.** `eval/run_eval.py` reads from
  Postgres once Phase 5 lands; there is no separate flat-file convention to
  keep in sync. (Decided over mirroring both, to avoid two systems of record.)
- **Scenario edits overwrite in place**, no versioning table. Reproducibility
  of past runs comes from `games.config_snapshot` (below), which freezes the
  scenario config at run time — editing the saved scenario afterward never
  changes what a past run used.
- **Schema** (implemented now in `db/models.py`; Phase 5 wires it into the app):
  - `scenarios` — a saved, reusable setup: `id`, `name`, `map_ref`,
    `max_turns`, `created_at`.
  - `scenario_factions` — one row per faction in a scenario, what the
    scenario-editor UI writes: `id`, `scenario_id`, `faction_name`,
    `role_preset` (Expansionist / Warmonger / Diplomat-Trader / Isolationist /
    custom), `starting_resources` (JSONB), `starting_units` (JSONB),
    `starting_territory` (province id list), optional `custom_prompt`.
    Resources/units stay JSONB rather than typed columns until Phase 4 fixes
    what a "unit" or "resource" actually is — avoids a premature migration.
  - `games` — one playthrough: `id` (the shared game id), `scenario_id`
    (nullable — ad-hoc runs allowed), `status`, `current_turn`,
    `winner_faction_id`, `config_snapshot` (JSONB, frozen at run start).
  - `game_factions` — live faction instances for one run: `is_alive`,
    `eliminated_at_turn`.
  - `game_events` — the tick-by-tick action log, replacing flat files:
    `game_id`, `turn`, `faction_id`, `event_type` (move / combat /
    trade_proposal / diplomacy_change / …), `payload` (JSONB).
  - `faction_state_snapshots` — per-turn resource/territory/unit totals per
    faction, so the UI can chart curves without replaying the full event log.
  - `diplomatic_relations` — pairwise status (war/truce/alliance) between
    every faction pair, with the turn it last changed.
  - `eval_scores`, `annotations` — Phase 6 only, DeepEval results and human
    annotation UI data. Not part of the Phase 5 schema.
- **Where it plugs in, by phase:**
  - Phase 2 (optional, low-risk) — LangGraph's Postgres checkpointer
    (`langgraph-checkpoint-postgres`) for resumable graph runs. Not required
    for the phase's functional goal, and uses its own tables, not the schema
    above.
  - Phase 5 — `game/run_game.py` writes `games`/`game_factions`/
    `game_events`/`faction_state_snapshots`/`diplomatic_relations` as ticks
    run (done). Still to come: the FastAPI backend reading it for the
    frontend, and a scenario-editor UI writing `scenarios`/
    `scenario_factions` (currently only `game/run_game.py` writes those,
    for its own ad-hoc runs).
  - Phase 6 — add `eval_scores`/`annotations`.

## How to work in this repo

- Build in phases, in order (see "Project phases" below), but phases can move
  as fast as makes sense — don't artificially slow down or gate on lesson-by-
  lesson pacing.
- Prefer complete, working implementations of a phase over minimal fragments,
  as long as they stay reviewable (don't dump the entire repo in one shot;
  land a phase in a small number of coherent, well-explained commits/edits).
- After adding code, give a concise summary of what it does and why — enough
  for review, not a tutorial.
- Aim for solid engineering practices appropriate to a real system: clear
  module boundaries, error handling, config via `.env`, and tests where they
  add real confidence — while staying appropriately scoped for a project at
  this stage (see "Non-goals").
- **Coding style / division of labor**: Claude writes and iterates on the code;
  the person reviews, runs it, and directs priorities.
- The agents call LLMs through a fallback chain (`agents/llm.py`): **Groq**
  and **OpenRouter** (free tiers) are tried first, **Anthropic Claude** (via
  `langchain-anthropic`) is the paid last resort. Keys (`GROQ_API_KEY`,
  `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`) live in `.env` — see
  `.env.example` for the template. At least one key must be set; unset
  providers are skipped, not treated as errors.

## Project phases (build in this order)

1. **LangGraph agent basics** — `agents/` — one simple agent, state/nodes/edges.
2. **Multi-agent coordination** — `agents/` — N factions (see "Agent &
   simulation design"), hierarchical per-faction roles, turn-taking, and
   actions expressed as structured tool calls rather than free text.
3. **Map/geo basics** — `map_data/` — geopandas, Natural Earth/OSM data, hex grid
   generation. No live Google Maps API — static, pre-generated GeoJSON.
4. **Connect agents to the map** — agents move between named provinces from
   `map_data/provinces.geojson` instead of abstract state; this is also where
   territory, resources, units, and per-faction-pair diplomatic status
   (war/truce/alliance) get defined in `agents/state.py`.
5. **Game loop + visualization** — `game/`, `backend/`, `frontend/` — tick loop,
   win conditions, FastAPI + WebSocket backend, Leaflet/D3 frontend, a
   scenario-editor UI (add factions, assign starting units/resources/role
   presets, tweak values, run, replay), backed by the Neon Postgres schema in
   "Persistence" as the sole store for game history (no flat-file logs).
6. **Eval + annotation** — `eval/` — DeepEval custom metrics scored against
   Postgres (see "Persistence"), plus a simple annotation UI for human review.
   Done — see the Progress log.

Check with the person before starting work in a folder for a phase beyond the current
one.

## Directory structure

```
order-wars/
├── agents/          # Phase 1-2: LangGraph agents, shared GameState, graph wiring
├── map_data/         # Phase 3: raw/processed geo data, hex-grid generation script
├── game/             # Phase 4-5: tick loop, capture/siege/trade rules, entrypoint
├── backend/          # Phase 5: FastAPI app, WebSocket tick broadcast
├── frontend/         # Phase 5: Leaflet/D3 map + scenario-editor UI
├── db/               # models.py + Alembic migrations (implemented); Phase 5 wires it into the app
├── eval/             # Phase 6: DeepEval metrics, batch eval runner, annotation UI
└── tests/
```

## Conventions

- Python 3.10+, virtual env in `venv/` (never commit).
- Secrets (e.g. `ANTHROPIC_API_KEY`, `DATABASE_URL`) live in `.env`, never
  committed, never hardcoded.
- Shared game state schema lives in `agents/state.py` — this is the single source of
  truth for what fields exist in `GameState`. Update it there first if a new field
  is needed, don't scatter ad-hoc dict keys elsewhere.
- Map data is generated once offline (`map_data/generate_map.py`) and committed as
  static GeoJSON (`map_data/provinces.geojson`) — the game never calls a live map API
  at runtime.
- Every game run writes tick-by-tick history to Postgres (`game_events`,
  `faction_state_snapshots` — see "Persistence") once Phase 5 lands; this is
  what `eval/run_eval.py` scores later, so schema changes there should stay
  backward-compatible or be called out explicitly, the same rule that used to
  apply to flat-file log format changes.

## Commands

- Run a game: `python -m game.run_game`
- Generate/refresh the map: `python map_data/generate_map.py`
- Run backend: `uvicorn backend.main:app --reload`
- Run frontend (dev server): `cd frontend && npm install && npm run dev`
- Run tests: `pytest tests/`
- Run eval on a completed game: `python -m eval.run_eval --game-id <id>`
- Apply DB migrations: `alembic upgrade head` (requires `DATABASE_URL` in `.env`)
- Create a new migration after changing `db/models.py`: `alembic revision --autogenerate -m "<message>"`

## Progress log

Running record of what's been built, so a future session knows where things
stand without re-deriving it from code. Update this when a phase or major
piece lands — check the box and add a one-line note if something non-obvious
came up.

- [x] Phase 0 — scaffolding (git, venv, `.env`, `requirements.txt`)
- [x] Phase 1 — LangGraph basics
  - [x] `agents/state.py`: `GameState` schema (`turn`, `max_turns`, `log`
        with an `operator.add` reducer, `last_decision`)
  - [x] Single-node `StateGraph`, `.compile()`/`.invoke()`
  - [x] Conditional edges / routing — `route_after_decision` loops
        `start_turn` -> `agent_decide` until `max_turns`, then `END`
  - [x] First real agent (calls an LLM via `agents/llm.py`'s fallback
        chain — Groq -> OpenRouter -> Claude; see the LLM provider note
        above. Not hardcoded to Claude, unlike the original plan.)
  - [x] `agents/graph.py` assembled, runnable as `python -m agents.graph`
  - [x] `tests/test_graph.py` covers the pure nodes, the routing function,
        and a full mocked run (no live API calls in the test suite)
  - **Superseded by Phase 2**: the single-faction `GameState`
        (`last_decision`, one `agent_decide` node) described above no
        longer exists in code — `agents/state.py` and `agents/graph.py` now
        hold the Phase 2 N-faction schema/graph below. Kept here as a record
        of what Phase 1 delivered at the time.
- [x] Persistence schema (ahead of Phase 5, by design — see "Persistence
      (Neon Postgres)")
  - [x] `db/models.py`: SQLAlchemy 2.0 models for all 7 tables (`scenarios`,
        `scenario_factions`, `games`, `game_factions`, `game_events`,
        `faction_state_snapshots`, `diplomatic_relations`) — resources/units
        are JSON (JSONB on Postgres) pending Phase 4's game-mechanics schema
  - [x] `db/session.py`: lazy engine/session, fails clearly if `DATABASE_URL`
        unset, never connects at import time
  - [x] Alembic wired up (`db/migrations/`), initial migration
        (`157d15fc6345_initial_schema.py`) verified against a real Postgres
        16 instance — upgrade/downgrade round-trips cleanly, `alembic check`
        reports zero drift against the models. Note: the autogenerated
        migration needed two hand-fixes verified by that testing — Postgres
        ENUM types aren't dropped by `op.drop_table` (had to add explicit
        `.create()`/`.drop()` calls), and a `use_alter=True` FK
        (`games.winner_faction_id` <-> `game_factions`, mutually referential
        tables) is silently dropped by `op.create_table` and needs an
        explicit `op.create_foreign_key()` after both tables exist
  - [x] `tests/test_db_models.py`: mocked against in-memory SQLite, no live
        DB in the test suite (same convention as `tests/test_graph.py`)
  - [ ] Not yet done: nothing in `agents/`, `game/`, or `backend/` imports
        `db.session` — the app itself doesn't connect to Postgres until
        Phase 5
- [x] Phase 2 — multi-agent coordination
  - [x] `agents/state.py`: `FactionState` (per-faction `intent` +
        `last_action`) and `GameState` generalized to N factions
        (`turn_order`, `active_faction_idx`) instead of a hardcoded pair —
        `turn` now counts completed full rounds, not individual actions
  - [x] `agents/roles.py`: 5 role-preset prompt fragments (expansionist,
        warmonger, diplomat_trader, isolationist, custom), mirroring
        `db.models.RolePreset`'s values by hand (no `db` import from
        `agents/` yet — that's still Phase 5)
  - [x] `agents/actions.py`: `FactionAction` Pydantic schema
        (`action_type`/`target_faction`/`rationale`) — deliberately abstract,
        no map/unit targets, since those don't exist until Phase 4
  - [x] `agents/llm.py`: `build_llm(schema=...)` binds
        `.with_structured_output()` per-provider *before* combining into the
        fallback chain (can't bind after — `RunnableWithFallbacks` isn't a
        `BaseChatModel`)
  - [x] `agents/graph.py`: single reusable `faction_turn` node loops over
        `turn_order` (works for any N, not just 2) with a two-layer
        hierarchy — a leader call refreshes `intent` every
        `INTENT_REFRESH_INTERVAL` (3) turns, an executor call decides that
        turn's structured `FactionAction` within it. Full role specialization
        (separate military/diplomat/economic agents) is deferred to
        Phase 4/5 — there's no distinct territory/resource/diplomacy state
        for them to act on yet, so splitting now would be hollow
  - [x] Verified live (not just mocked) against the real Groq -> OpenRouter
        -> Claude chain: a 3-faction demo run showed role presets visibly
        shaping behavior (Rome/expansionist expanded, Carthage/warmonger
        raided Gaul, Gaul/isolationist fortified). That run also surfaced a
        real bug — Groq's hidden-reasoning tokens truncated the structured
        tool-call JSON at `max_tokens=200`, same class of issue Phase 1 had
        already noted for plain completions — fixed by raising the budget
        (300 for intent, 600 for the action call)
  - [x] `tests/test_graph.py` rewritten for the N-faction node/routing (still
        fully mocked, no live calls in the suite)
- [x] Phase 3 — map/geo generation
  - [x] `map_data/generate_map.py`: downloads Natural Earth 50m land +
        admin-0 country polygons (cached under `map_data/raw/`, gitignored),
        clips to a Western/Central Mediterranean bounding box (Iberia,
        central-to-southern France, Italy, North Africa coast, Balkans,
        Greece — the Rome/Carthage/Gaul theater, not the whole Roman world;
        see the script docstring for why lat_max is capped at 47, not
        higher), tiles it with H3 hexagons at resolution 3 (~12,400 km²/hex)
  - [x] Uses `contain="overlap"` + a `land_frac >= 0.01` filter rather than
        H3's default center-point containment — verified empirically that
        center containment silently drops small/thin islands (missed
        Corsica, Cyprus, Malta, Mallorca at this resolution) even though
        they're real land
  - [x] Each province gets a `name` (dominant overlapping country + index,
        e.g. "Italy 1") and `neighbors` (H3 grid-adjacency intersected with
        the actual kept province set, so provinces only neighbor real land,
        not filtered-out sea hexes)
  - [x] `map_data/provinces.geojson` committed: 372 provinces, verified —
        unique ids/names, symmetric adjacency, no orphans or self-loops, and
        the three Phase 2 demo factions' homelands (Italy, Tunisia, France)
        all present
  - [x] `tests/test_map_data.py`: validates the committed GeoJSON directly
        (schema, uniqueness, adjacency symmetry) — regenerating the map
        needs geopandas/h3/network access and stays a separate offline step,
        not part of the test suite
  - [ ] Not yet done: nothing in `agents/`/`game/` reads `provinces.geojson`
        — wiring factions to real provinces is Phase 4
- [x] Phase 4 — agents on the map
  - [x] `map_data/loader.py`: runtime province lookups (`get_province`,
        `neighbors_of`, `province_ids_in_country`) — pure `json`, no
        geopandas/h3 needed at runtime, only for offline generation
  - [x] `agents/state.py`: `province_owner` (province id -> faction id) and
        `diplomatic_status` (pair-keyed, war/truce/alliance) added to
        `GameState`; `resources`/`units` added to `FactionState`. Territory
        is deliberately NOT duplicated onto `FactionState` — `province_owner`
        is the single source of truth (`game.rules.territory_of` derives it)
  - [x] `agents/actions.py`: `FactionAction` reworked from Phase 2's abstract
        `action_type`/`target_faction` to map-grounded fields —
        `target_province` for `move_army`, `proposal` for `negotiate` — plus
        `build_unit` and `declare_war`
  - [x] `game/rules.py` (new package): pure, LLM-free resolution of a
        sanitized action — income, capture, one-shot combat (simple
        unit-count comparison, ceiling-based attrition — floor rounding was
        tried first and caught in testing: `int(1 * 0.5) == 0` made any
        1-unit stack immortal), unit building, unilateral `declare_war`, and
        reciprocal `negotiate` (a proposal only resolves once the other side
        proposes the same thing back — no one-sided forced alliances)
  - [x] `agents/graph.py`: the executor prompt now lists real legal
        `move_army` targets (own territory + adjacency, from
        `map_data/loader.py`) and real other-faction ids/diplomatic status
        instead of free-floating names; `_sanitize_action` repairs/downgrades
        an LLM response that names an illegal province or faction anyway
        (verified live: this matters — LLMs occasionally ignore the listed
        options)
  - [x] `tests/test_rules.py`: full coverage of `game/rules.py` against real
        province ids/adjacency from the committed map (income, capture,
        both combat outcomes, build cost/insufficient-funds, unilateral war,
        one-sided vs. reciprocal negotiation)
  - [x] `tests/test_graph.py` rewritten: covers intent refresh, a legal
        move being applied, and an illegal move being sanitized to `hold`
  - [x] Verified live against the real Groq -> OpenRouter -> Claude chain: a
        3-turn, 3-faction run showed real territorial expansion into
        adjacent provinces and a real `declare_war` (Carthage on Gaul).
        Emergent finding, not a bug: Carthage and Gaul share no land border
        in this map (only land got tiled, no naval movement modeled), so
        that war is currently symbolic — neither side can ever `move_army`
        into the other without a land bridge. Worth knowing before treating
        "at war" as meaning "actively fighting."
- [x] Phase 5 — game loop + visualization (game loop, Postgres wiring,
      FastAPI/WebSocket backend, and the Leaflet frontend/scenario-editor UI
      all done — see below for what's still rough)
  - [x] Real Neon persistence wired up (`db/session.py` now actually used
        by the app, not just designed): `DATABASE_URL` added, connection
        verified, and every table isolated under an `order_wars` Postgres
        schema (`db/session.py`'s `DB_SCHEMA`) because the Neon database
        turned out to be shared with unrelated projects — see "Persistence"
        for the transaction/reflection bugs this surfaced and fixed
        (uncommitted-transaction rollback biting twice, and autogenerate
        crashing on other projects' tables via `public`'s default schema)
  - [x] Schema gap found and fixed via a proper migration, not a
        workaround: `faction_state_snapshots.territory_count` (a number)
        can't render a map — added `territory` (the actual province id
        list) so the frontend can reconstruct board state from the latest
        snapshot per faction without replaying `game_events`
  - [x] `agents/state.py`/`agents/graph.py`: added `last_event` to
        `GameState` — a self-describing record of the most recently
        resolved turn, so the persistence layer (and later the backend's
        broadcast) doesn't need to diff consecutive states to know what
        just happened. `agents/graph.py`'s `run()` refactored to share
        `initial_state_for()` with the new entrypoint below rather than
        duplicating state construction
  - [x] `game/run_game.py` (`python -m game.run_game`, per "Commands"):
        drives the graph via `.stream()` instead of `run()`'s single
        blocking `.invoke()`, persisting `GameEvent`/`FactionStateSnapshot`/
        `DiplomaticRelation` rows turn-by-turn and checking a win condition
        (`check_winner`: elimination — exactly one faction still holds
        territory) after each turn rather than only at the end. Writes real
        `Scenario`/`ScenarioFaction`/`Game`/`GameFaction` rows too — an
        ad-hoc CLI run is still a real one; the future scenario-editor UI
        is just another way to populate the same rows
  - [x] `tests/test_run_game.py`: in-memory SQLite (`StaticPool`, since the
        loop opens several separate sessions and plain `sqlite://` would
        otherwise hand each one an unrelated empty database), no live LLM
        or Neon calls
  - [x] Verified live end-to-end against real Groq/OpenRouter/Claude *and*
        real Neon: a 4-turn, 3-faction run produced 1 scenario, 3
        scenario_factions, 1 game (correctly marked COMPLETED, no winner —
        max turns reached with all 3 still alive), 3 game_factions, 12
        game_events, 12 faction_state_snapshots, and 1 diplomatic_relation
        (Carthage's declare_war on Gaul) — all under `order_wars`, `public`
        and other projects' tables untouched
  - [x] `game/run_game.py` split into `create_game()` (fast — just the DB
        writes) and `play_game()` (slow — the actual turn loop), so
        `backend/` can return a game id immediately from a request handler
        and run the LLM-driven loop in the background instead of blocking
        the request for however long the game takes. `run_game()` stays as
        the simple combined version for the CLI/tests. Also added
        `load_faction_configs()` to build `faction_configs` from an
        existing `Scenario`'s rows (slugifying faction names into the short
        ids `agents.graph` uses internally, deduped on collision), so a
        game can start `from scenario_id=...` and reuse a saved scenario
        instead of always creating a fresh ad-hoc one
  - [x] Bug fixed alongside this: per-faction `starting_resources`/
        `starting_units` were already being persisted onto
        `ScenarioFaction` but `agents.graph.initial_state_for` ignored them
        and used the global defaults for every faction regardless —
        `resources`/`units` overrides in `faction_configs` now actually
        reach the simulation, not just the DB record
  - [x] `backend/main.py` (`uvicorn backend.main:app --reload`): FastAPI
        app — `POST/GET /scenarios`, `POST /games` (from `scenario_id` or
        an ad-hoc `factions` list), `GET /games`, `GET /games/{id}`
        (status + each faction's latest resources/territory/units, derived
        from its newest `FactionStateSnapshot`), `GET /games/{id}/events`,
        `GET /map/provinces` (serves the committed GeoJSON), and
        `WS /games/{id}/live` for live updates while a game plays
  - [x] `backend/game_hub.py`: in-process registry running `play_game` on a
        background thread per game and fanning its updates out to
        WebSocket subscribers via a plain `queue.Queue` per subscriber
        (thread-safe; consumed on the async side via `asyncio.to_thread`,
        since touching `asyncio.Queue` from a non-event-loop thread isn't
        safe). Single-process only, by design — a multi-worker deployment
        would need a real pub/sub broker (e.g. Redis) instead; noted here
        rather than silently assumed
  - [x] `tests/test_game_hub.py`: `GameHub` in isolation, using
        `threading.Event` for deterministic ordering (not timing/sleeps)
  - [x] `tests/test_backend.py`: `TestClient` against in-memory SQLite,
        `GameHub.start` patched synchronous so most tests don't need to
        wait on a background thread — except the one WebSocket test, which
        deliberately restores real threading. That test surfaced a real
        design property worth knowing, not a bug: `GameHub` has no
        backlog/replay for a subscriber that joins after messages already
        broadcast to zero subscribers — connecting late can genuinely miss
        early turns (confirmed live too: a real websocket client, started
        right after `POST /games` returned, connected too slowly to catch
        anything but the final turn and `stream_end` on a fast 2-turn game.
        The REST endpoints (`GET /games/{id}`, `.../events`) are unaffected
        and remain the reliable way to get a game's full history)
  - [x] Verified live end-to-end with a real running server (not just
        `TestClient`): started `uvicorn`, created a real scenario, started
        a real game from it (real LLMs, real Neon), watched it over a real
        WebSocket connection, confirmed the final state and all 6 events
        via REST, then deleted the test scenario/game from Neon afterward
  - [x] Self-review pass over the whole phase turned up 5 real gaps, all
        fixed and tested (not just the obvious "did it run once" check):
      - No failure handling at all — a crashed `play_game` (e.g. no LLM key
        configured) left `Game.status` stuck at `RUNNING` forever, logged
        nothing server-side, and only reported to a WebSocket subscriber
        connected at that exact instant. `GameStatus.FAILED` existed in the
        schema but was never assigned anywhere. Fixed: `play_game` now
        wraps the run in try/except, logs via `logging`, and marks the
        `Game` `FAILED` with `ended_at` set before re-raising — verified
        live against real Neon by blanking the LLM env vars for one run.
      - `POST /games` with an invalid ad-hoc `role_preset` returned **404**
        instead of **422** — `start_game()`'s `except ValueError` conflated
        "scenario not found" with "bad input," since both raised the same
        exception type. Fixed with a dedicated `ScenarioNotFoundError`
        (a `ValueError` subclass) so the two are caught separately.
      - `GameFaction.is_alive`/`eliminated_at_turn` were dead fields —
        defined, exposed via `GameFactionOut`, never updated. `GET
        /games/{id}` reported every faction alive forever, even ones
        eliminated turns ago. Fixed: `_mark_eliminated_factions` checks
        every faction's territory after each event and flips `is_alive`
        the turn it first has none.
      - No validation that factions' starting provinces are unique or
        real — two factions sharing a `home_province` silently left one
        with zero territory (permanently stuck: no owned province means no
        legal `move_army` target, ever). Fixed: `create_game` now validates
        both before writing anything.
      - No upper bound on `max_turns` — the LLM fallback chain includes
        paid Anthropic Claude, so an accidental `max_turns=100000` had no
        cost ceiling. Fixed: `MAX_TURNS_LIMIT = 200` enforced in
        `create_game` (all callers), the Pydantic schemas (`ge=1,
        le=MAX_TURNS_LIMIT`), and the CLI's `--max-turns`.
  - [x] The three medium-priority items from that review fixed too:
      - No CORS middleware — added `CORSMiddleware`, origin list
        configurable via `CORS_ORIGINS` (defaults to `*`; no
        auth/cookie-based session exists to protect, see Non-goals).
        Verified live: `Access-Control-Allow-Origin` header present against
        a real running server, not just `TestClient`.
      - Unlocked race in `db/session.py`'s lazy `get_engine`/
        `get_sessionmaker` singletons — added a lock, double-checked
        inside each function. **First attempt used a plain
        `threading.Lock()` and deadlocked immediately**: `get_sessionmaker`
        calls `get_engine()` while already holding the lock, and a plain
        `Lock` isn't reentrant. Caught by the new test itself hanging, not
        by inspection — fixed with `threading.RLock()`.
      - `GameHub._subscribers` never shrank — `start()` now drops the
        `game_id` entry once its background thread finishes (success or
        error), so a long-running server doesn't accumulate one empty list
        per game ever played. A client that subscribes to that exact,
        already-finished `game_id` afterward still just waits forever for
        a message that will never come — same as before this fix, not a
        regression, since a late subscriber got nothing either way; this
        only stops the bookkeeping itself from leaking.
  - [x] `frontend/`: Vite + vanilla JS (no framework — there's no
        significant client-side state or logic to justify one; the backend
        owns every rule) + Leaflet for the map. Not React/Vue as might be
        assumed by default; a deliberate call since CLAUDE.md already said
        "Leaflet/D3 frontend," not a specific JS framework.
      - `mapView.js`: renders `provinces.geojson` as a Leaflet layer,
        colors provinces by faction (a stable palette assigned in
        first-seen order), supports a "pick a province" mode for the
        scenario editor
      - `scenarioEditor.js`: add/remove factions, set role preset/starting
        resources/units, click the map to set each faction's home
        province (rejects two factions picking the same one client-side,
        the same check `game/run_game.py`'s `create_game` already
        enforces server-side) — submits to `POST /scenarios`
      - `gameView.js`: start a game from a saved scenario, connect to
        `WS /games/{id}/live`, recolor the map and append to an event log
        as messages arrive; select a past game from `GET /games` to
        either watch it live (if still running) or load its full history
        at once via `GET /games/{id}/events` (replay, no live connection)
      - **Actually visually verified, not just API-checked** — this
        environment initially had no working headless browser (Playwright's
        Chromium was cached but `libnspr4.so`/`libnss3.so`/`libasound.so.2`
        were missing, and there's no root to `apt install` them). Worked
        around it without root: `apt-get download` (unlike `apt install`,
        needs no privileges) the `.deb`s, `dpkg-deb -x` to extract them to a
        local directory, then `LD_LIBRARY_PATH` pointed at that directory
        for the Chromium launch. From there, real screenshots plus a
        scripted Playwright run (fill the form, click the map to pick
        provinces, save, start a game, watch it play) against the real
        backend/Neon/LLMs — not just inspection — caught real bugs a
        build-only check never would have:
        - CARTO's free anonymous tile endpoint (`basemaps.cartocdn.com`)
          turned out to require an API key now — the basemap rendered as
          tiled "API KEY REQUIRED" watermarks. Switched to standard OSM
          tiles (still keyless), toned down with a CSS grayscale filter to
          keep the colored province hexes as the visual focus.
        - The faction-row layout (name/role/remove on one line, a
          province-picker button, gold/legion inputs on others) clipped
          the remove button past the sidebar's edge in a real 1400px
          screenshot — not apparent from reading the CSS. Rebuilt as
          stacked lines instead of one dense row.
        - Emoji icons (💰/⚔️) didn't render at all in this headless
          Chromium (no emoji font installed) — replaced with plain text
          labels ("Gold"/"Legions"), which is more robust across
          environments regardless of the cause.
        - `MapView.setSelected` was wiping ownership colors back to
          "unclaimed" while picking a home province mid-game; `loadReplay()`
          dropped `target_province`/`target_faction` when mapping REST
          events, so a replayed game's log never showed what an action
          targeted.
        - **The most significant one**: watching a live game, the map
          stayed completely gray through all of round 1 even though the
          event log updated normally — `game/run_game.py` only wrote
          `FactionStateSnapshot` rows (what map coloring reads) once a full
          round completed, not per action. Fixed by upserting a snapshot
          for every faction after every action instead, keyed on the
          event's round number (constant through a round) rather than
          `state["turn"]` (which only advances at the round boundary — using
          it would've given a round's earlier actions a different, stale
          key than its last one). That fix surfaced a second, subtler bug
          while writing its test: `play_game`'s `on_event` callback (what
          `backend/`'s WebSocket broadcast is built on) fired *before* the
          corresponding DB write committed, not after — a client reacting
          to a live broadcast with an immediate REST call could race the
          still-in-flight commit and read stale data, on every single
          update, not just during round 1. Reordered so `on_event` only
          fires once its transaction has committed. Both confirmed live: a
          screenshot taken ~4s after a game started showed both factions'
          territory correctly colored on the map while `current_turn` was
          still 0 (round 1 in progress).
- [x] Phase 6 — eval + annotation
  - [x] `eval/llm_wrapper.py`: `DeepEvalLLM(DeepEvalBaseLLM)` wraps the
        existing Groq -> OpenRouter -> Claude fallback chain (`agents/llm.py`)
        as DeepEval's judge model, instead of hardcoding a separate one.
        Uses `method="json_mode"` for structured output — GEval's prompts
        contain literal "return JSON" text instructions that collide with
        Groq's tool-calling and raised `groq.BadRequestError: attempted to
        call tool 'json' which was not in request.tools`; `json_mode` works
        across all three providers (Anthropic silently falls back to
        `json_schema` internally, with a harmless warning).
  - [x] `eval/metrics.py`: two metrics scored per faction-authored
        `GameEvent` — `LegalActionMetric` (rule-based `BaseMetric`, checks
        the action wasn't silently sanitized by `game/rules.py`'s
        `_sanitize_action`) and `Role Alignment` (`GEval`, LLM-judged,
        scores whether the chosen action fits the faction's assigned role
        preset). `build_role_alignment_metric()` passes
        `_include_g_eval_suffix=False` — GEval appends " [GEval]" to its
        `__name__` by default, which would have silently broken
        `run_eval.py`'s own summary aggregation (`if metric_name ==
        "Role Alignment"` would never match). Caught by a test asserting
        the exact stored `metric_name`, not by inspection.
  - [x] `eval/run_eval.py` (`python -m eval.run_eval --game-id <id>`, per
        "Commands"): scores every faction-authored event in a completed
        game and persists results as `EvalScore` rows, reusing
        `db.session`'s `scoped_session` (promoted from a private helper
        in `game/run_game.py` to a shared one, since both now need it).
        Also reachable from the backend as `POST /games/{id}/evaluate`.
  - [x] DB: `EvalScore` and `Annotation` tables added via migration
        `1022ae49fdae`, both FK'd to `game_events` with
        `cascade="all, delete-orphan"` — deleting a game cascades cleanly
        through events to their scores/annotations. `Annotation` holds a
        human reviewer's 1-5 `rating` and/or free-text `note` per event.
        Migration verified round-trip against real Neon, `alembic check`
        reports zero drift.
  - [x] `backend/main.py`: `POST /games/{id}/evaluate` (runs `run_eval`,
        404 if the game doesn't exist) and `POST /events/{id}/annotations`
        (422 if neither `rating` nor `note` given, 404 if the event
        doesn't exist). Also fixed a latent schema bug found along the
        way: `GameEventOut.faction_id` was typed non-nullable `uuid.UUID`
        even though the DB column already allows null (some events, like
        a `stream_end` marker, aren't faction-authored) — now
        `uuid.UUID | None`.
  - [x] `frontend/src/reviewView.js`: new "Review" tab — pick a completed
        game from a dropdown, click "Run evaluation" to trigger real
        scoring, see each event with color-coded pass/fail score badges
        (green/red via `.score-ok`/`.score-low`) and an inline annotation
        form (1-5 rating + note).
  - [x] Verified live end-to-end: played a real 3-faction, 2-turn game
        against real Neon/LLMs, ran `eval.run_eval` against it (12 scored
        event/metric pairs, all persisted and confirmed via direct query),
        then drove the Review tab with a headless Playwright script
        against real running `uvicorn`/Vite servers — selected the game,
        ran evaluation (real LLM calls), confirmed 12 score badges
        rendered, added a real annotation, and screenshotted every step.
        All four screenshots inspected visually and confirmed correct —
        map + sidebar layout holds up at 1400px with no clipping, unlike
        some of Phase 5's frontend bugs that only showed up this way.
  - [x] Along the way, found the project's LLM fallback chain was partly
        broken, unrelated to eval code itself: OpenRouter's free
        `meta-llama/llama-3.3-70b-instruct:free` (the default in
        `.env.example`/`agents/llm.py`) is now deprecated and 404s, and
        the configured Anthropic account has a $0 credit balance (paid
        fallback silently unusable). Surfaced to the person rather than
        worked around silently; asked how to proceed and got the go-ahead
        to research and swap the model. Replaced the OpenRouter default
        with `nvidia/nemotron-3-super-120b-a12b:free` (checked OpenRouter's
        public `/api/v1/models` for current free models, tried a couple of
        candidates live, picked this one — larger model, established lab,
        confirmed working through the fallback chain 3x in a row). That
        swap needed one more fix: the new model consumes more
        reasoning/output tokens than the old one, so `_decide_action`'s
        `max_tokens` in `agents/graph.py` went from 600 to 900 after a
        live `openai.LengthFinishReasonError` at 600 confirmed the cause.
        The Anthropic $0-credit issue is a known limitation, not something
        code can fix — needs billing credit added to that account.

## Gameplay depth rollout (Phase 7+)

After Phase 6, the user asked for a large set of gameplay-depth features
(naval movement, terrain, unit composition, sieges, supply lines, rebellion,
province development, trade, tribute, coalition wars, narrative event
tagging) built **in a dependency order that avoids rewriting earlier work**.
The order, and why:

1. **Map depth: terrain + naval lanes** (below) — done first since it
   regenerates the committed `provinces.geojson`; every later stage that
   references terrain or cross-water movement needs this data to exist
   already.
2. Multi-resource economy (grain/iron/gold tied to terrain) — no schema
   migration needed, since `FactionState.resources`/`.units` are already
   untyped `dict[str, int]` at every layer (agent state, `ScenarioFaction`
   JSONB, `FactionStateSnapshot` JSONB, backend Pydantic schemas).
3. Unit composition (infantry/cavalry/siege, rock-paper-scissors) — same
   reason, `units` is already the right shape.
4. Sieges (multi-turn capture) — reuses stage 3's combat code.
5. Supply lines/attrition — reuses stage 1's adjacency/terrain distance calc
   and stage 4's siege target as a "front line" proxy (the project
   deliberately has no per-province garrisons — one pooled army per
   faction — so this is a lightweight distance proxy, not a garrison
   rewrite).
6. Rebellion/unrest — reuses stage 5's distance-from-capital helper; the
   project's first RNG-based mechanic (seeded per game, for testability).
7. Province development — placed after sieges/rebellion so it has real
   defensive mechanics to plug into.
8. Trade agreements — reuses the existing reciprocal `pending_proposals`
   handshake from `negotiate` almost as-is.
9. Tribute/vassalage — same reused mechanism as trade.
10. Coalition wars (joint offensives among allies) — prompt-context +
    a rule-triggered event, no combat rewrite.
11. Narrative event tagging — last, so there's a rich set of event types
    (siege, rebellion, tribute, trade, coalition) to narrate.

### Stage 1 — terrain + naval movement (done)

- **Terrain** (`map_data/generate_map.py`'s `_classify_terrain`): a
  gameplay-flavor proxy derived only from data the pipeline already
  computes — no new Natural Earth layers (elevation/bathymetry) downloaded.
  `is_coastal` is true if a province borders a *real* water gap (an H3
  neighbor that was tiled and dropped for insufficient land — distinguished
  from a neighbor missing only because it falls outside the generation
  bbox, a clipping artifact that would otherwise misclassify ~44 provinces
  near the bbox's northern edge) OR the province is mostly water itself
  despite being topologically land-ringed at this hex resolution
  (`land_frac < 0.5` — several small Greek island/gulf hexes have
  `land_frac` as low as 0.02–0.15 despite all 6 neighbors being kept).
  Remaining provinces are `plains` (`land_frac >= 0.9`) or `hills` (the
  partial-water minority in between — non-coastal hexes on this map cluster
  overwhelmingly at exactly `land_frac == 1.0`, so 0.9 cleanly separates the
  genuine minority). Current map: 169 coastal / 178 plains / 25 hills (of
  372).
- **Naval lanes** (`_compute_sea_neighbors`): short cross-water `move_army`
  lanes between coastal provinces that aren't already land-adjacent (H3
  res-3 hexes already bridge some narrow straits — e.g. Gibraltar — as
  land adjacency, confirmed empirically, so no lane needed there). Pairs
  are filtered by centroid distance (LAEA CRS, cap 400km — calibrated so
  the Sicily/Tunisia crossing, the motivating case, is included) and by a
  trimmed-line-vs-land check: both centroids sit inside land by
  construction, so testing the raw centroid-to-centroid line against the
  real land polygon rejects real open-water crossings too (confirmed
  against real Adriatic/Ionian pairs) — trimming `min(30km, 30% of length)`
  off each end before testing the remaining "core" segment fixes this.
  Each province keeps its nearest 3 candidates, symmetrized by union (a
  few real chokepoints end up with more than 3 lanes — expected, not a
  bug). A fallback guarantees every coastal province at least one lane
  (its single nearest coastal province, land-adjacency excluded) even
  beyond the 400km cap — verified live: all 169 coastal provinces ended up
  with ≥1 sea lane. **Bug caught by the new symmetry tests, not by
  inspection**: the first version of that fallback didn't exclude
  provinces already reachable by land, so it could add a redundant sea
  lane duplicating an existing land border — fixed by excluding
  `neighbors.get(h3_id, ())` from the fallback's candidate set.
- `map_data/loader.py`: `Province` gained `terrain`, `sea_neighbors`, and
  `land_frac` (the last was silently dropped by the loader before this,
  despite being in the file and already required by
  `tests/test_map_data.py`'s `REQUIRED_PROPERTIES` — a pre-existing latent
  gap, fixed as part of this touch). New `sea_neighbors_of()` accessor
  mirrors `neighbors_of`.
- `agents/graph.py`'s `_legal_move_targets` now also includes
  `sea_neighbors_of(province_id)` for owned provinces — the only
  gameplay-facing change this stage. No change to `agents/actions.py`,
  `_sanitize_action`, or `game/rules.py`'s `move_army` resolution — a
  sea-lane target is just another string in the existing legal-target set,
  by design (no naval unit type or ship-building mechanic yet; that's
  deferred, not an oversight).
- `tests/test_map_data.py`: added terrain-value, sea-neighbor-symmetry,
  sea-neighbors-only-connect-coastal, no-redundant-land+sea-edge, and a
  direct "Tunisia can reach Italy by sea" regression test for the
  motivating case. 12/12 pass; full suite 98/98.
- Verified live: regenerated `provinces.geojson` (still exactly 372
  provinces — the algorithm changes properties, not which hexes are kept),
  confirmed via the loader that multiple Tunisia provinces now list
  Sicily/Italy provinces as `sea_neighbors`. No live LLM calls needed for
  this stage (mocked graph/rules tests already cover `_legal_move_targets`'
  consumers).

### Stage 2 — multi-resource economy (done)

Confirmed no DB/schema migration was needed: `FactionState.resources`/
`.units`, `ScenarioFaction.starting_resources`/`.starting_units` (JSONB),
`FactionStateSnapshot.resources` (JSONB), and the backend's Pydantic
schemas were already untyped `dict[str, int]` at every layer — this stage
is purely new keys + rules logic + frontend fields, exactly as the roadmap
predicted.

- `game/rules.py`: `TERRAIN_RESOURCE = {"coastal": "gold", "plains":
  "grain", "hills": "iron"}` — `_apply_income` now looks up each owned
  province's terrain (from Stage 1) and credits `INCOME_PER_PROVINCE` (1)
  of that terrain's resource, instead of a flat gold-per-province. Applied
  unconditionally every turn regardless of chosen action, same as before.
- `BUILD_UNIT_COST` (a bare int) replaced with `UNIT_COSTS: dict[str,
  dict[str, int]]` (currently `{"legion": {"gold": 10, "iron": 5}}`) — a
  dict-of-dicts on purpose so Stage 3 (more unit types) can add entries
  here without touching the build-resolution logic itself, which already
  checks/deducts an arbitrary set of resources.
- `agents/graph.py`: `STARTING_RESOURCES = {"gold": 20, "grain": 20,
  "iron": 10}` (was gold-only); the executor prompt now lists each legal
  move target's terrain and a one-line explanation of which terrain yields
  which resource, so the LLM can reason about *why* a province is worth
  taking, not just that it's legal.
- `backend/schemas.py`'s `ScenarioFactionIn.starting_resources` default
  updated to match, so API-created scenarios without explicit resources
  also start with a real 3-resource economy.
- `frontend/src/scenarioEditor.js`: added Grain/Iron starting-resource
  inputs alongside Gold. `frontend/src/gameView.js`'s faction summary now
  shows each faction's live resources/unit count (previously fetched from
  the API but never displayed) — closes the gap from the very first ask in
  this project ("test on the UI by adding units, resources... seeing
  outcomes").
- **Two real bugs caught by live screenshot verification, not
  inspection**: (1) three resource stats per row overflowed the sidebar —
  fixed by reverting to the already-proven 2-stats-per-row layout from
  Phase 5. (2) that didn't fully fix it — measuring the actual rendered
  widths found `.faction-stat input`'s `width: 3.2rem` had *never* been
  applying, beaten by the generic `form input[type="number"] { width:
  100% }` rule's higher CSS specificity (0,1,2 vs 0,1,1). This was a
  latent bug predating this stage (2 wide-open inputs happened to fit
  well enough before to go unnoticed); fixed by re-scoping the selector to
  `.faction-row .faction-stat input` (0,2,1), which now correctly wins.
- Verified without spending any LLM quota: `tests/test_rules.py` gained a
  direct terrain-income test (one faction owning a plains + coastal +
  hills province nets grain + gold + iron in one turn) and an updated
  build-cost test; both pass fully mocked. The frontend resources display
  was verified live by inserting a `Game`/`GameFaction`/
  `FactionStateSnapshot` row directly via the ORM (no LLM calls at all)
  and loading it in a real browser — confirmed the sidebar renders "21
  gold, 15 iron, 5 grain — 3 units" correctly, then cleaned up the test
  row from Neon. 99/99 tests pass.

### Stage 3 — unit composition, rock-paper-scissors combat (done)

- `agents/actions.py`: `FactionAction` gained `unit_type: UnitType | None`
  (`Literal["legion", "cavalry", "siege_engine"]`, defaults to legion if
  unset) — additive, no existing field changed.
- `game/rules.py`: `UNIT_COSTS` extended with `cavalry` (15 gold, 10 grain
  — horses need feeding, not mining) and `siege_engine` (20 iron, 5 gold —
  engineering-heavy, not manpower) alongside the existing `legion` entry;
  `build_unit`'s resolution logic needed zero changes to support them,
  exactly as Stage 2's dict-of-dicts shape was designed to allow.
  `BUILD_UNIT_TYPE` renamed `DEFAULT_UNIT_TYPE` (now genuinely a fallback,
  not the only option).
- New `COUNTERS` triangle (`cavalry > legion > siege_engine > cavalry`)
  and `_effective_strength()`, replacing the flat `_total_units()` sum
  (deleted — dead code once combat stopped calling it) in the `move_army`
  combat branch. Effective strength scales a side's raw count up by
  `COUNTER_BONUS` (0.5) in proportion to *how much of the enemy's specific
  composition* it counters (not a flat bonus for merely holding any
  countering unit) — a few cavalry can't claim a full bonus against an
  army that's mostly siege engines. Same-type-vs-same-type combat is
  mathematically unaffected (no countered type present → 0 bonus),
  preserving every Phase-4 combat test's original raw-headcount behavior.
- `agents/graph.py`'s executor prompt gained `_unit_options_summary()` —
  each unit type's cost and counter relationship, derived from
  `game.rules.UNIT_COSTS`/`COUNTERS` rather than hardcoded text, so it
  can't drift out of sync with the actual rules.
- Considered and rejected a runtime sanitization check for an invalid
  `unit_type`: unlike `target_province`/`target_faction` (plain strings an
  LLM can genuinely hallucinate past the prompt's listed options),
  `unit_type` is a closed Pydantic `Literal` — invalid values are already
  rejected at structured-output parse time, before `_sanitize_action` ever
  runs, making a runtime check dead code. Guarded the real risk instead
  (`UnitType`, `UNIT_COSTS`, and `COUNTERS` silently drifting out of sync
  by hand) with a direct consistency test.
- `tests/test_rules.py`: one test per side of the RPS triangle (each
  demonstrates a *numerically smaller* force winning via the counter
  bonus — 4 cavalry beats 5 legion, etc. — not just "wins," to prove the
  bonus is actually doing the work), a same-type-no-bonus regression test,
  new build-cost tests (default unit type, explicit `unit_type` choice),
  and the `UnitType`/`UNIT_COSTS`/`COUNTERS` consistency test. 107/107
  tests pass.
- No live LLM calls needed to build/verify this stage (pure rules-engine
  logic, fully covered by mocked/direct tests) — the executor prompt's
  token-budget headroom (`max_tokens=900`, tuned with real margin when
  bumped from 600 for a 4-field schema) hasn't been re-verified live
  against the now-5-field schema; flagged for whenever LLM quota is next
  confirmed available, not assumed safe.
- No frontend change this stage: the UI still shows an aggregate unit
  count (`FactionStateSnapshot.unit_count`, a plain int, unaffected by
  richer composition since it's still `sum(units.values())`) rather than
  a type breakdown — showing composition would need a new DB column/
  migration, which nothing in this stage's scope actually requires yet.

### Stage 4 — multi-turn sieges + terrain defense bonus (done)

- `agents/state.py`: new `GameState.sieges: dict[str, dict]` field — maps a
  besieged province id to `{"attacker_id": str, "progress": int}`.
  Deliberately ephemeral operational state alongside `province_owner`
  (never duplicates ownership), following the exact pattern already
  established by `pending_proposals`/`last_event` — no per-province
  garrisons added, since a siege tracks *who* is attacking *which*
  province, not where either side's pooled army physically sits.
- `game/rules.py`: `move_army` into enemy territory at war no longer
  resolves combat immediately. `SIEGE_TURNS_TO_DECIDE = 2` — the first
  `move_army` against a given enemy province begins a siege (progress 1,
  no combat, no attrition); the *same attacker* targeting the *same
  province* on their very next own turn presses it to progress 2, which
  triggers the decisive battle (reusing Stage 3's `_effective_strength`
  unchanged, exactly as the roadmap intended). Any of an attacker's
  in-progress sieges not being actively pressed this turn are abandoned
  with no losses to either side — pressing a different target, moving
  peacefully, or the war ending all lapse it. A different faction pressing
  an already-sieged province simply restarts progress at 1 under the new
  attacker's name (sieges don't stack across attackers; whoever pressed
  most recently owns the active one).
- `TERRAIN_DEFENSE_BONUS = {"hills": 0.3}` — the decisive battle multiplies
  the defender's effective strength by `1 + bonus` for their province's
  terrain (coastal/plains get none). Verified with a test where a
  numerically stronger attacker (6 vs 5) loses specifically *because* of
  the hills bonus (5 × 1.3 = 6.5 > 6) — not just "defender can win."
- `agents/graph.py`'s executor prompt gained a one-line explanation of the
  siege mechanic plus `_siege_summary()` (which of *this* faction's sieges
  are in progress and at what count), derived from live state — an agent
  needs to know it must press the same target again, or its first attack
  will look like it silently failed.
- `agents/actions.py`'s docstring/field description updated to describe
  sieges instead of the old "one-shot combat" simplification note.
- **Real, expected ripple through existing tests, not a sign of a design
  problem**: every test that depended on one-shot combat (Phase 4's
  original stronger/weaker-attacker tests, all four of Stage 3's RPS
  tests, and three "wins in exactly one action" fixtures in
  `test_run_game.py`/`test_backend.py`) needed updating — either via a new
  `_besiege()` test helper that drives a siege to its decisive call, or by
  pre-seeding `state["sieges"]` at `progress = SIEGE_TURNS_TO_DECIDE - 1`
  so a scripted fake-LLM test still resolves in exactly one real action.
  This is the *combat calculation code* being reused unchanged (per the
  roadmap's promise), not the tests — the roadmap never promised existing
  tests would survive untouched when a stage's whole point is changing
  when a decisive battle happens.
- No DB/migration changes: the new `sieges` state is transient
  (GameState-only) — the human-readable resolution string ("began a siege
  of X" / "pressed the siege of X" / "broke the siege of X, captured from
  Y" / "lost the siege of X") already flows into `GameEvent.payload`
  through the existing pass-through mechanism with zero `run_game.py`
  changes, and the frontend's event log already displays `resolution`
  verbatim — sufficient to narrate what happened without a new column.
- No live LLM calls needed: 112/112 tests pass, all mocked/direct.

### Stage 5 — supply-line attrition (done)

- `map_data/loader.py`: new `distance_between(a, b) -> int` — unweighted
  BFS hop-distance over the combined land+sea adjacency graph (a hex is a
  hex whether crossed by land or a naval lane). Verified against real data:
  HOME→NEIGHBOR = 1, HOME→FAR_AWAY (Italy↔Tunisia) = 4, terminates
  instantly even for far pairs since the whole graph is one connected
  component (confirmed when Stage 1 added naval lanes).
- `game/rules.py`: `_apply_supply_attrition`, called every turn right
  after `_apply_income` — a faction actively pressing a siege more than
  `SUPPLY_FREE_RANGE` (2) hexes from its own territory sheds
  `SUPPLY_ATTRITION_PER_HEX` (5%) of its pooled army per hex beyond that,
  capped at 100%. Uses the *farthest* of a faction's active sieges (one
  pooled army, strained by its most extended commitment, not summed
  across multiple sieges) — deliberately anchored to sieges specifically
  (not "wherever a faction's last move_army went") since a siege is the
  one persistent, well-defined "where is this faction's offensive
  currently committed" signal that already exists in state; a faction not
  currently sieging anywhere pays no supply cost. Only the attacker pays —
  a defender fighting on its own soil isn't straining supply lines.
  Confirmed this doesn't require per-province garrisons (deliberately out
  of scope, see the module docstring): the siege dict already records
  *which* province a faction is attacking, which is all the distance
  calculation needs.
- `agents/graph.py`'s `_siege_summary` now shows each of a faction's
  active sieges' hex-distance from supply, flagged when it's costing
  attrition, and the military specialist's prompt explains the mechanic —
  so the LLM can reason about overextension, not just discover it after
  the fact via a shrinking unit count.
- `tests/test_rules.py`: direct `distance_between` tests plus supply-
  attrition coverage (no active siege → no cost; within free range → no
  cost; beyond it → exact ceil-rounded loss, same rounding convention as
  combat's `_attrit`; farthest-of-multiple-sieges; attacker-only, not
  defender). 124/124 tests pass, no live LLM calls needed (pure rules
  logic).

### Stage 6 — rebellion/unrest (done)

- `agents/state.py`: three new `GameState` fields — `capitals` (faction id
  -> province id, set once in `initial_state_for` from each faction's
  starting `home_province` and never rewritten, even if the capital itself
  later falls — a fixed geographic anchor, not "wherever a faction
  currently holds"), `province_captured_turn` (province id -> the `turn`
  it was last captured — absent means held since game start, exempt from
  rebellion forever unless later lost and recaptured), and
  `rebellion_seed` (one real random draw per game, in `initial_state_for`,
  optionally pinnable for reproducible tests/replays).
- `game/rules.py`: this is the project's **first randomized mechanic**,
  and deliberately *not* real randomness — `_rebellion_roll` hashes
  `(rebellion_seed, province_id, turn)` into a deterministic pseudo-random
  float instead of calling `random.random()`, so `resolve_action` stays a
  pure function of its inputs (its own docstring's first sentence,
  unbroken through 6 stages now) and every rebellion outcome is exactly
  reproducible/testable without mocking a `random.Random` instance —
  unpredictability comes entirely from the once-per-game seed draw, not
  from impure per-call randomness.
- `_check_rebellions`: a province recently captured by its current owner
  (within `REBELLION_GRACE_TURNS` = 3 turns) **and** far from that owner's
  capital (beyond `REBELLION_DISTANCE_THRESHOLD` = 3 hexes, via Stage 5's
  `distance_between`) risks reverting to unclaimed each turn
  (`REBELLION_CHANCE_PER_TURN` = 15%). Deliberately measured from the
  **capital**, not nearest-owned-territory like supply attrition (Stage
  5) — a distinct concern (administrative reach/legitimacy vs. logistics):
  a faction with plenty of nearby holdings can still fail to pacify a
  far-flung new conquest. Once a province survives its grace period, it's
  considered settled and never rebels again regardless of distance, until
  it changes hands once more.
- `move_army`'s peaceful-capture and decisive-siege-victory branches now
  record `province_captured_turn`; re-reinforcing already-owned territory
  does not reset the clock (not a new capture). A rebellion appends to the
  turn's `resolution` string (flows into `GameEvent.payload` through the
  existing generic pass-through, zero `run_game.py` changes) rather than
  needing a new structured field, matching how the siege/supply mechanics
  already communicate through resolution text.
- `agents/graph.py`'s military specialist prompt gained a one-line
  explanation of the mechanic (no per-province risk display yet, unlike
  the siege-distance line Stage 5 added — there's no consolidation action
  to act on that information with until Stage 7's province development,
  so a detailed readout would be informational noise without a lever to
  pull).
- `tests/test_rules.py`: real computed roll values (not arbitrary
  fixtures) — found via a small script that `seed=3` rolls 0.1006 for the
  FAR_AWAY test province at turn 1, below the 15% threshold, giving an
  exact, reproducible positive test case alongside the negative ones
  (held-since-start, within distance, grace-period-expired, and that
  rebellion only fires on the owning faction's own turn). 129/129 tests
  pass, no live LLM calls needed (pure rules logic; the only prompt change
  is explanatory text).

### Stage 7 — province development (done)

- `agents/actions.py`: new `"develop_province"` action type, added to both
  `FactionAction.action_type` and `EconomicAction.action_type` (the
  military/diplomatic specialists never see it — development is the
  economic/logistics agent's domain). Reuses `FactionAction`'s existing
  `target_province` field rather than adding a new one.
- `agents/state.py`: new `GameState.province_development: dict[str, int]`
  (absent = level 0). Keyed purely by province, not by owner — it
  deliberately **persists through a change of ownership**: development
  represents built infrastructure (roads, fortifications, administration),
  not the previous owner's loyalty, so capturing a well-developed enemy
  province stays valuable rather than resetting to 0. This was a real
  design fork (reset-on-capture would arguably be more "realistic" in a
  scorched-earth sense) resolved in favor of rewarding conquest, not
  punishing it.
- `game/rules.py`'s new `develop_province` branch: raises a faction's own
  province's development level by 1, capped at `MAX_PROVINCE_DEVELOPMENT`
  (3). Cost scales with the level being bought — `DEVELOP_BASE_COST` (15
  gold) × (current level + 1) — a deliberate diminishing-returns curve so
  one province can't cheaply stack indefinitely. "Not your territory" and
  "already maxed" are handled as graceful no-op resolutions inside
  `resolve_action` (same precedent as `build_unit`'s "lacked resources"
  case), not `_sanitize_action` downgrades — unlike `move_army`'s
  adjacency check, "do you own this province" doesn't need the same
  prompt-time-computed legal-target-set machinery, and reusing that
  machinery here would have meant duplicating the "owned" concept between
  two modules for no real benefit.
- Development plugs into every mechanic Stages 4-6 built, exactly as
  planned when this stage was placed after them in the roadmap: each level
  adds `DEVELOPMENT_YIELD_BONUS_PER_LEVEL` (1) to a province's terrain
  income (`_apply_income`), subtracts
  `DEVELOPMENT_REBELLION_REDUCTION_PER_LEVEL` (0.05) from its effective
  rebellion chance (`_check_rebellions`, floored at 0), and adds
  `DEVELOPMENT_DEFENSE_BONUS_PER_LEVEL` (0.1) on top of Stage 4's
  `TERRAIN_DEFENSE_BONUS` during a decisive siege battle there.
- `agents/graph.py`'s economic specialist prompt gained a
  `_development_summary()` (each owned province's current level and the
  gold cost to raise it, derived from live state rather than hardcoded)
  and an explanation of what `develop_province` does.
- `tests/test_rules.py`: full coverage of the action itself (raises level,
  cost scaling, rejected when not owned, rejected at max level, rejected
  when short on gold) plus one test per downstream effect — income bonus,
  and two tests that isolate development's contribution from Stage 4's
  terrain bonus by reusing the *exact* rebellion/combat scenarios from
  Stages 4 and 6 with development added on top, showing the same roll/
  matchup that used to rebel/lose now doesn't, because of development
  specifically. 137/137 tests pass, no live LLM calls needed (pure rules
  logic; the only prompt change is explanatory text).

### Stage 8 — trade agreements (done)

- `agents/actions.py`: `ProposalType` gained `"trade"`; `FactionAction`/
  `DiplomaticAction` gained `offer_resource`/`offer_amount` (what the
  proposer commits to give per turn, once agreed).
- **The one real design fork this stage**: `negotiate`'s existing
  reciprocal check (`pending_proposals.get(incoming_key) == proposal`)
  matches truce/alliance by exact equality — the same status agreed by
  both sides. Trade can't use that: the two sides' resource/amount are
  expected to *differ* (that's the point of a trade), so "propose the
  identical thing" can never be satisfied. `_negotiate_trade` implements
  different reciprocal semantics instead — a trade activates once *both*
  sides have *any* outstanding trade offer to each other, regardless of
  whether the terms match. `pending_proposals`'s value type stays a plain
  string throughout (not widened to a dict) by encoding a trade offer as
  `"trade:{resource}:{amount}"` — every existing reader of that field
  (`_diplomacy_summary`) keeps working unchanged; `_describe_proposal`
  decodes it for display.
- New `GameState.trade_agreements` (keyed like `diplomatic_status`, by
  `pair_key`) is a **persistent** agreement, unlike ephemeral
  `pending_proposals` entries — once activated it stays in effect
  indefinitely. `_apply_trade_agreements` delivers each side's committed
  resource to the other as part of that side's own per-turn upkeep
  (alongside income/supply attrition — same established pattern), so a
  full round completes the bidirectional exchange; verified with a test
  that on Rome's turn only Rome's committed resource moves; Carthage's
  committed resource moves only when Carthage itself acts. A faction
  short on its commitment gives what it can rather than the agreement
  breaking outright — no "trade broken" consequence yet, a deliberate
  simplification flagged in the module docstring, not silently dropped.
- `agents/graph.py`'s diplomatic prompt gained a `_trade_summary()`
  (active agreements, what's given/received, derived from live state) and
  an explanation of the propose/accept-with-different-terms mechanic.
  `_sanitize_action` downgrades a trade proposal missing
  `offer_resource`/`offer_amount` to hold, same precedent as negotiate
  without a `proposal` type.
- `tests/test_rules.py`: one-sided offer encoding, activation with
  deliberately mismatched terms (proving the reciprocal check isn't
  exact-match), per-actor delivery isolation, and partial delivery when
  short on the committed resource. 143/143 tests pass, no live LLM calls
  needed (pure rules logic; the only prompt change is explanatory text).

### Stage 9 — tribute/vassalage (done)

- No new fields on `agents/actions.py`'s schemas — tribute reuses
  `target_province` (added for `develop_province`), `offer_resource`/
  `offer_amount` (added for trade), and the existing `proposal`/
  `target_faction` fields wholesale. `ProposalType` gained `"tribute"`.
  This is the cleanest confirmation yet that the roadmap's "extends the
  same proposal plumbing" prediction for this stage held exactly.
- **A third distinct reciprocal-negotiation semantics**, alongside
  truce/alliance's exact-match and trade's any-offer-both-ways: tribute is
  a one-sided peace offer, not a mutual exchange, so the recipient accepts
  an *existing* offer from the payer rather than making a matching one of
  their own. `_negotiate_tribute` checks for an incoming `"tribute:"`-
  prefixed proposal exactly like `_negotiate_trade` checks for
  `"trade:"`, but on acceptance resolves the payment **immediately**
  (one-time, via `factions`/`province_owner` mutation in the same call) —
  no new persistent `*_agreements`-style state, unlike trade's standing
  agreement.
- A real edge case handled explicitly: the province a payer offered to
  cede might no longer be theirs by the time the offer is accepted (lost
  to a rebellion or another war in between) — `_negotiate_tribute`
  re-checks `province_owner.get(ceded_province) == target` at acceptance
  time, transfers the resource payment regardless, and notes in the
  resolution when the land specifically couldn't be handed over. Same
  non-breaking-on-shortfall simplification as trade for the resource part.
- `_sanitize_action`'s tribute check had to be smarter than trade's: it
  first checks whether an incoming tribute offer exists (accepting needs
  no `offer_resource`/`offer_amount` of its own) before requiring those
  fields — otherwise a legitimate acceptance would get wrongly downgraded
  to hold.
- `agents/graph.py`'s diplomatic prompt gained the faction's own territory
  list (needed to cede a real, owned province) and an explanation of the
  propose/accept-immediately mechanic; `_describe_proposal` decodes a
  `"tribute:"`-prefixed pending proposal for display, same pattern as
  trade's decoding.
- `tests/test_rules.py`: one-sided offer encoding (with and without a
  ceded province), acceptance transferring resources and declaring a
  truce, the still-owned-vs-no-longer-owned ceded-province branch, and
  partial payment when short on gold. 151/151 tests pass, no live LLM
  calls needed (pure rules logic; the only prompt change is explanatory
  text).

Stages 10–11 will each get their own short validation pass against the
real code before implementation (the same way stages 1-9 needed real
data/code to calibrate correctly, not just up-front assumptions), landing
as their own commits in this same order.

## Multi-level agent hierarchy (done, separate from the gameplay-depth
## rollout above — an agent-architecture change, not a game mechanic)

Realizes the "Hierarchical agents per faction" bullet in "Agent &
simulation design" above, which had stood as forward-looking intent since
Phase 2 (there wasn't yet distinct territory/resource/diplomacy state for
separate specialists to act on independently — the gameplay-depth stages
changed that). Before designing anything, asked the user two clarifying
questions given they'd just exhausted a day's LLM quota on one game:
one dispatched decision/turn vs. three parallel specialist calls/turn
(chose dispatched — no LLM call increase), and live agent-to-agent
negotiation dialogue vs. keeping the existing structured propose/accept
`negotiate` action (chose to keep it). Both choices are documented as
deliberate simplifications in "Agent & simulation design" above, not
silently dropped scope.

- `agents/actions.py`: added `MilitaryAction` (`move_army`/`hold`),
  `EconomicAction` (`build_unit`/`hold`), `DiplomaticAction`
  (`negotiate`/`declare_war`/`hold`) — each a genuinely narrower Pydantic
  schema (not just a prompt-level restriction) mirroring the relevant
  slice of `FactionAction`'s fields. `FactionAction` itself, along with
  `_sanitize_action` and `game.rules.resolve_action`, is completely
  unchanged — a specialist's result converts directly into a
  `FactionAction` (`FactionAction(**result.model_dump())`) since its
  fields are always a subset with `FactionAction`'s own defaults covering
  the rest.
- `agents/graph.py`: `_dispatch_specialist(state, faction_id)` — pure,
  no LLM call — picks which specialist decides this turn, in priority
  order: (1) an active siege this faction is pressing always routes to
  military, since a siege lapses if not pressed every one of the
  attacker's own turns (Stage 4) and leaving that to chance would make
  sieges nearly impossible to complete; (2) an incoming pending proposal
  routes to diplomatic, so offers get answered instead of going stale;
  (3) otherwise a role-preset-ordered rotation
  (`agents.roles.specialist_order`). `_decide_action` now builds one of
  three focused, domain-specific prompts (military doesn't see unit costs,
  economic doesn't see move targets, diplomat doesn't see either) instead
  of one prompt covering everything — each narrower than the old
  monolithic prompt/schema, so `_ACTION_MAX_TOKENS` (900, carried over
  unchanged) has more headroom per call than before, not less; not
  re-tuned down without live data confirming it's safe to.
- `agents/roles.py`: `ROLE_PRESET_SPECIALIST_ORDER` — a fixed permutation
  of all three domains per role preset (e.g. `warmonger: ["military",
  "diplomatic", "economic"]`, `isolationist: ["economic", "military",
  "diplomatic"]`) — always includes every domain at least once every 3
  turns, so no faction is ever permanently locked out of expansion/
  economy/diplomacy; only the *priority order* differs by preset. Extends
  the existing "role preset shapes behavior" pattern from prompt tone
  alone to actual action-domain frequency.
- `last_event` (and therefore `GameEvent.payload`, via the existing
  generic pass-through — zero `run_game.py` changes) gained a
  `"specialist"` key recording which domain decided the turn.
  `frontend/src/gameView.js`'s event log shows it (e.g. `Turn 3 — Rome
  [military]: move_army ...`), so the architecture is visible when
  watching a game, not just inferable from resolution text.
- **Real, expected ripple, not a sign of a design problem** (same
  precedent as Stage 4's siege-timing change): the log-line format
  assertion in two `test_graph.py` tests needed updating for the new
  `[specialist]` tag, and the shared fake-LLM test helper needed to become
  schema-aware (return an instance of whichever specialist schema
  `build_llm` was actually bound to, not a hardcoded `FactionAction` —
  otherwise a real dispatch/schema mismatch could pass silently). Existing
  intent-refresh/sanitization tests get `_dispatch_specialist` monkeypatched
  to a fixed domain so they stay focused on what they originally tested
  rather than getting coupled to the rotation formula.
- New direct tests for `_dispatch_specialist` (siege override, proposal
  override — including that an *outgoing* proposal correctly does **not**
  trigger it, only an incoming one — role-preset rotation, unknown-role
  fallback) and for `_decide_action`'s schema-to-domain wiring. 118/118
  tests pass, all mocked/direct — no live LLM calls needed to build or
  verify this (pure prompt/schema/dispatch logic).
- **Verified live** (`python -m agents.graph`, 3 factions/3 turns, real
  Groq/OpenRouter/Anthropic fallback chain, no DB persistence in this demo
  path): zero schema parsing errors or truncation across all three
  specialist schemas. The rotation and override logic both worked exactly
  as designed against real model output — Rome (expansionist) went
  military → economic → diplomatic and Gaul (isolationist) went economic →
  military → diplomatic, both matching their configured order exactly;
  Carthage (warmonger, configured `[military, diplomatic, economic]`) went
  military → diplomatic → **diplomatic again** on turn 3 (not the
  "economic" its rotation would suggest) because Rome proposed an alliance
  to it that same round — the incoming-proposal override correctly
  preempted the rotation, and Carthage (true to its warmonger prompt)
  responded by declaring war instead of accepting.

## Non-goals

- No live Google Maps API calls in the core game loop (cost/quota, and historical
  factions don't belong on modern road networks).
- No premature production concerns unrelated to the game itself (auth, deployment,
  scaling, multi-tenant infra) — this is still a portfolio-scale project, just
  built with production-quality code rather than lesson-paced fragments.
- No reliance on fully emergent, unscaffolded multi-agent diplomacy — coalition
  and alliance formation should be rule-triggered (power-balance based), with
  LLM agents handling negotiation content within that scaffold, not expected
  to spontaneously emerge from open-ended prompting (see "Agent & simulation
  design").
- The ORM models/migrations in `db/` were built ahead of Phase 5 by explicit
  request (see "Persistence" and the Progress log) — that was schema design
  before the app needed it, not a violation of the phase gate above. Actual
  wiring (the app reading/writing through `db.session`) is Phase 5, now
  underway.
