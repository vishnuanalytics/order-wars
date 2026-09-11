# Order Wars

A multi-agent strategy/war-game simulation, built as a **learning project**.
The end goal: factions (LangGraph agents) expand territory, trade, and fight
over a real-world-derived map, with the results evaluated by DeepEval and
improved through human annotation.

This is a learning project first, portfolio piece second — see `claude.md`
for how work in this repo is paced (small, explained, lesson-by-lesson steps
rather than large generated dumps).

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in ANTHROPIC_API_KEY
```

## Phase status

- [ ] Phase 0 — project scaffolding
- [ ] Phase 1 — LangGraph agent basics (`agents/`)
- [ ] Phase 2 — multi-agent coordination (`agents/`)
- [ ] Phase 3 — map/geo generation (`map_data/`)
- [ ] Phase 4 — connect agents to the map
- [ ] Phase 5 — game loop + visualization (`game/`, `backend/`, `frontend/`)
- [ ] Phase 6 — eval + annotation (`eval/`)

See `claude.md` for what each phase covers and the "Learning log" for
concepts already learned.
