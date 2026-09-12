# Order Wars

A multi-agent strategy/war-game simulation, built as a production-oriented
portfolio project. The end goal: factions (LangGraph agents) expand
territory, trade, and fight over a real-world-derived map, with the results
evaluated by DeepEval and improved through human annotation.

See `claude.md` for how work in this repo is organized (phased, in order,
with sound engineering practice over lesson pacing).

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in at least one LLM provider key
```

Agents call an LLM through a fallback chain (`agents/llm.py`): Groq and
OpenRouter (free tiers) are tried first, Anthropic Claude is the paid last
resort. See `.env.example` for the keys and model overrides.

## Run it

```bash
python -m agents.graph      # Phase 4 demo: N factions, no persistence
python -m game.run_game     # Phase 5: full game loop, persists to Postgres (needs DATABASE_URL)
pytest tests/                # mocked, no live API/DB calls
```

`game.run_game` needs `DATABASE_URL` in `.env` (see `.env.example`) — it
writes every game to Postgres. `agents.graph`'s demo doesn't persist
anything, useful for a quick check without a database configured.

## Phase status

- [x] Phase 0 — project scaffolding
- [x] Phase 1 — LangGraph agent basics (`agents/`) — superseded by Phase 2
- [x] Phase 2 — multi-agent coordination (`agents/`)
- [x] Phase 3 — map/geo generation (`map_data/`)
- [x] Phase 4 — connect agents to the map (`agents/`, `game/`)
- [~] Phase 5 — game loop + visualization (`game/`, `backend/`, `frontend/`)
      — game loop + Postgres persistence done; backend/frontend not started
- [ ] Phase 6 — eval + annotation (`eval/`)

See `claude.md` for what each phase covers and the "Progress log" for what's
landed so far.
