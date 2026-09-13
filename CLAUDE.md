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

Decisions below shape Phases 2, 4, and 5. They're forward-looking design
intent, not yet built (see "Progress log") — but new work in those phases
should build toward this shape rather than a simpler one that needs
retrofitting later.

- **N factions, not two.** `GameState` and the graph must be built to scale to
  an arbitrary, user-configured number of factions, not hardcoded to a pair.
  Phase 2's "two+ agents" should be implemented as N from the start — cheap
  now, expensive to retrofit later.
- **Hierarchical agents per faction, not one monolithic decision-maker.** Each
  faction is composed of role-specialized agents rather than a single LLM call
  deciding everything:
  - **Strategic leader** — sets intent (expand north, seek peace with X) every
    few turns, not every tick.
  - **Military commander** — tactical unit orders each tick, constrained by
    current leader intent.
  - **Diplomat/trade agent** — negotiates directly with other factions'
    diplomat agents (agent-to-agent, not narrator-mediated).
  - **Economic/logistics agent** — resource allocation, production.

  This is a planner/executor split: intent changes rarely, execution happens
  every tick within that intent, and re-planning is event-driven (triggered by
  a deviation from expected state) rather than redone from scratch each turn.
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
