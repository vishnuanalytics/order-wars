# CLAUDE.md — Order Wars

Guidance for Claude Code when working in this repository.

## What this project is

A multi-agent strategy/war-game simulation used as a **learning project**. The end goal
is a "Total War meets Civilization" simulation: factions (agents) expand territory,
manage trade, and fight over a real-world-derived map, evaluated with DeepEval and
improved via human annotation.

**This is a learning project, not a race to a finished product.** The person building
this is learning LangGraph, multi-agent systems, geospatial data, and eval pipelines
from the ground up. Prioritize teaching and small, understandable steps over speed or
cleverness.

## How to work in this repo

- Build in phases, in order. Do not jump ahead to a later phase's folder even if it
  would be convenient — see "Project phases" below.
- Prefer small, runnable pieces over large generated files. After adding code, explain
  what it does before moving on.
- Default pace is beginner/lesson-by-lesson: introduce one new concept at a time,
  check it works, then continue. Don't dump a fully-built phase in one shot unless
  explicitly asked to.
- Keep code simple and readable over "production-grade" — this is for learning first.
- **Coding style / division of labor**: Claude writes the code and explains it step
  by step; the person runs it, reads it, and asks questions. This is not a "you type
  it yourself" pairing style — the learning happens through reading working code and
  running it, not through typing it from scratch.
- The LLM backing the agents is **Anthropic Claude**, via `langchain-anthropic`,
  reading `ANTHROPIC_API_KEY` from `.env` (see `.env.example` for the template).

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

## Learning log

Running record of concepts already covered, so a future session knows where
we left off without re-deriving it from code. Update this when a lesson is
completed — check the box and add a one-line note if something non-obvious
came up.

- [ ] Phase 0 — scaffolding (git, venv, `.env`, `requirements.txt`)
- [ ] Phase 1 — LangGraph basics
  - [ ] Lesson 1: single-node `StateGraph`, `GameState`, `.compile()`/`.invoke()`
  - [ ] Lesson 2: two nodes + unconditional edge
  - [ ] Lesson 3: conditional edges / routing
  - [ ] Lesson 4: first real agent (calls Claude via `langchain-anthropic`)
  - [ ] Lesson 5: `agents/graph.py` assembled, runnable as `python -m agents.graph`
- [ ] Phase 2 — multi-agent coordination
- [ ] Phase 3 — map/geo generation
- [ ] Phase 4 — agents on the map
- [ ] Phase 5 — game loop + visualization
- [ ] Phase 6 — eval + annotation

## Non-goals

- No live Google Maps API calls in the core game loop (cost/quota, and historical
  factions don't belong on modern road networks).
- No premature production concerns (auth, deployment, scaling) — this is a learning
  and portfolio project.