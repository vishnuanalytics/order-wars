# Order Wars

A multi-agent strategy/war-game simulation, built as a production-oriented
portfolio project. The end goal: factions (LangGraph agents) expand
territory, trade, and fight over a real-world-derived map, with the results
evaluated by DeepEval and improved through human annotation.

See `CLAUDE.md` for how work in this repo is organized (phased, in order,
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
python -m agents.graph              # Phase 4 demo: N factions, no persistence
python -m game.run_game             # Phase 5: full game loop, persists to Postgres (needs DATABASE_URL)
uvicorn backend.main:app --reload   # Phase 5: HTTP/WebSocket API (needs DATABASE_URL)
python -m eval.run_eval --game-id <id>  # Phase 6: score a completed game with DeepEval
pytest tests/                        # mocked, no live API/DB calls
```

`game.run_game` and `backend/` need `DATABASE_URL` in `.env` (see
`.env.example`) — they write every game to Postgres. `agents.graph`'s demo
doesn't persist anything, useful for a quick check without a database
configured. With the backend running, `GET /docs` has the interactive API
reference (FastAPI's auto-generated Swagger UI).

`eval.run_eval` scores every faction-authored `GameEvent` in a completed game
against three DeepEval metrics — two rule-based/free (legality, and whether a
legal action still wasted the turn) and one LLM-judged role-alignment score
via `GEval` — and persists the results as `EvalScore` rows. The same thing is
reachable from the frontend's Review tab (`POST /games/{id}/evaluate`), which
also lets a human add a 1-5 rating and note per event (`Annotation` rows).

### Frontend

```bash
cd frontend
npm install
cp .env.example .env   # only needed if the backend isn't on localhost:8000
npm run dev            # http://localhost:5173 — needs the backend running too
```

Build a scenario: add factions, pick each one's capital on the map (this
auto-claims a small starting territory around it too — adjustable by hand
afterward), save it, then start a game and watch it play live. Four tabs:

- **Scenario** — the editor above.
- **Games** — watch a game live (with a notable-event ticker, a
  predict-the-winner mini-game, and the option to take over a faction's
  turns yourself and hand it back to the AI anytime) or scrub through a
  finished one turn-by-turn.
- **Review** — DeepEval scores plus your own annotations per decision, with
  a plain-English glossary, score trends, and a CSV export.
- **Insights** — how each role preset (Expansionist, Warmonger, ...) tends
  to score, averaged across every game evaluated so far, not just one.

## Phase status

- [x] Phase 0 — project scaffolding
- [x] Phase 1 — LangGraph agent basics (`agents/`) — superseded by Phase 2
- [x] Phase 2 — multi-agent coordination (`agents/`)
- [x] Phase 3 — map/geo generation (`map_data/`)
- [x] Phase 4 — connect agents to the map (`agents/`, `game/`)
- [x] Phase 5 — game loop + visualization (`game/`, `backend/`, `frontend/`)
- [x] Phase 6 — eval + annotation (`eval/`)
- [x] Phase 7 — gameplay depth (terrain, naval movement, multi-resource
      economy, unit composition, sieges, supply lines, rebellion, province
      development, trade, tribute, coalition wars, narrative event tagging)
      and a full multi-level agent hierarchy (strategic leader + a
      dispatched military/economic/diplomatic specialist per turn)
- [x] Post-rollout: an engagement/learnability pass over the whole app —
      a metric glossary and cross-game Insights tab, gamified annotation,
      live-play spectacle, letting a viewer take over a faction mid-game
      and hand it back, replay scrubbing, multi-province starting
      territory, a real cut in per-game LLM cost, and authentic map depth
      (desert/forest terrain, rivers, notable cities)

See `CLAUDE.md` for what each phase covers and the "Progress log" for the
detailed history of what's landed and why.
