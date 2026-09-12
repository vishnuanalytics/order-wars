# CLAUDE.md — Order Wars

Guidance for Claude Code when working in this repository.

## What this project is

A multi-agent strategy/war-game simulation. The end goal is a "Total War meets
Civilization" simulation: factions (agents) expand territory, manage trade, and
fight over a real-world-derived map, evaluated with DeepEval and improved via
human annotation.

**This is a production-oriented build.** Prioritize working, well-structured,
maintainable code over lesson pacing. Move efficiently through phases, use
sound engineering judgment, and don't hold back on completeness or cleverness
where it genuinely helps the system — but keep code readable and documented so
it stays maintainable as it grows.

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
2. **Multi-agent coordination** — `agents/` — two+ agents sharing state, turn-taking.
3. **Map/geo basics** — `map_data/` — geopandas, Natural Earth/OSM data, hex grid
   generation. No live Google Maps API — static, pre-generated GeoJSON.
4. **Connect agents to the map** — agents move between named provinces from
   `map_data/provinces.geojson` instead of abstract state.
5. **Game loop + visualization** — `game/`, `backend/`, `frontend/` — tick loop, win
   conditions, FastAPI + WebSocket backend, Leaflet/D3 frontend.
6. **Eval + annotation** — `eval/` — DeepEval custom metrics scored against
   `logs/`, plus a simple annotation UI for human review.

Check with the person before starting work in a folder for a phase beyond the current
one.

## Directory structure

```
order-wars/
├── agents/          # Phase 1-2: LangGraph agents, shared GameState, graph wiring
├── map_data/         # Phase 3: raw/processed geo data, hex-grid generation script
├── game/             # Phase 4-5: tick loop, capture/siege/trade rules, entrypoint
├── backend/          # Phase 5: FastAPI app, WebSocket tick broadcast
├── frontend/         # Phase 5: Leaflet/D3 map visualization
├── eval/             # Phase 6: DeepEval metrics, batch eval runner, annotation UI
├── logs/             # per-game/tick logs — written by game/, read by eval/
└── tests/
```

## Conventions

- Python 3.10+, virtual env in `venv/` (never commit).
- Secrets (e.g. `ANTHROPIC_API_KEY`) live in `.env`, never committed, never hardcoded.
- Shared game state schema lives in `agents/state.py` — this is the single source of
  truth for what fields exist in `GameState`. Update it there first if a new field
  is needed, don't scatter ad-hoc dict keys elsewhere.
- Map data is generated once offline (`map_data/generate_map.py`) and committed as
  static GeoJSON (`map_data/provinces.geojson`) — the game never calls a live map API
  at runtime.
- Every game run writes tick-by-tick logs to `logs/<game_id>/` — this is what
  `eval/run_eval.py` scores later, so log format changes should stay backward-
  compatible or be called out explicitly.

## Commands

- Run a game: `python -m game.run_game`
- Generate/refresh the map: `python map_data/generate_map.py`
- Run backend: `uvicorn backend.main:app --reload`
- Run tests: `pytest tests/`
- Run eval on a completed game: `python -m eval.run_eval --game-id <id>`

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
- [ ] Phase 2 — multi-agent coordination
- [ ] Phase 3 — map/geo generation
- [ ] Phase 4 — agents on the map
- [ ] Phase 5 — game loop + visualization
- [ ] Phase 6 — eval + annotation

## Non-goals

- No live Google Maps API calls in the core game loop (cost/quota, and historical
  factions don't belong on modern road networks).
- No premature production concerns unrelated to the game itself (auth, deployment,
  scaling, multi-tenant infra) — this is still a portfolio-scale project, just
  built with production-quality code rather than lesson-paced fragments.