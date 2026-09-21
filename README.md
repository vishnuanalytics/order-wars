# Order Wars

A multi-agent strategy/war-game simulation — "Total War meets Civilization,"
built as a production-oriented portfolio project rather than a lesson-paced
exercise. An arbitrary number of AI-controlled factions, each assigned a
role/personality, expand territory, build armies, trade, negotiate truces
and alliances, and fight over a real-world-derived Mediterranean map. Every
decision an agent makes is a structured, schema-validated action — not
narrated free text — so the game engine, other agents, and a human reviewer
can all react to it the same way. Full games persist to Postgres, get
scored automatically against DeepEval metrics, and can be reviewed and
annotated by a human afterward. A human can also take over a faction
mid-game and hand control back to the AI at any time.

Live game state, event logs, and diplomacy are served over a FastAPI +
WebSocket backend and rendered on a Leaflet map in a small vanilla-JS
frontend, with optional Google Sign-In for attributing scenarios and
annotations to a real account.

## How it works

**Agents.** Each faction is not one LLM call — it's a small hierarchy.
A strategic leader sets an `intent` every few turns (now a cheap rule-based
heuristic, not an LLM call — see "LLM cost" below); a rule-based dispatcher
picks *one* specialist to act each turn (military, diplomatic, or
economic), prioritizing an in-progress siege, then an unanswered proposal,
then a role-preset-ordered rotation; that specialist makes one LLM call,
constrained to a Pydantic schema covering only its own domain's actions
(`move_army`/`hold` for military, `negotiate`/`declare_war`/`hold` for
diplomatic, `build_unit`/`develop_province`/`hold` for economic). The whole
thing is built on [LangGraph](https://github.com/langchain-ai/langgraph) and
calls out through a provider fallback chain — Groq and OpenRouter's free
tiers first, Anthropic Claude as a paid last resort — so a game can run
without ever spending money.

**The map.** A real Natural Earth-derived Mediterranean map (Iberia,
France, Italy, North Africa, the Balkans, Greece — the Rome/Carthage/Gaul
theater) tiled into ~370 H3 hexagon provinces, each with terrain (coastal,
plains, hills, desert, forest) that determines what resource it yields,
what defense bonus it grants, and whether it needs a naval lane to reach.
Rivers and notable cities are layered on for visual flavor. The map is
generated once offline and committed as static GeoJSON — nothing at
runtime calls a live maps API.

**The rules engine** (`game/rules.py`) is a pure function of game state:
income by terrain, unit-composition combat with a rock-paper-scissors
counter system (cavalry > legion > siege engine > cavalry), multi-turn
sieges with terrain/development defense bonuses, supply-line attrition for
overextended attackers, deterministic-but-unpredictable rebellion in
recently-conquered territory, province development (an investment sink that
compounds income/defense/rebellion-resistance), trade agreements, one-sided
tribute/vassalage offers, and rule-triggered coalition-war prompts when one
faction badly outweighs an ally's opponent. None of this is emergent LLM
behavior by itself — the game engine computes the triggers and legality;
agents decide what to do within them.

**Persistence.** Every game writes tick-by-tick to a dedicated
`order_wars` schema in Postgres (Neon) — scenarios, per-turn events, per-turn
per-faction snapshots, and pairwise diplomatic status — so a completed game
can be replayed, scored, and reviewed without needing to re-run it.

**Eval + review.** `eval/run_eval.py` scores every faction-authored action
in a completed game against three DeepEval metrics: two free/rule-based
(was the action legal? did it waste the turn on something unaffordable or
illegitimate?) and one LLM-judged (`GEval`, does the action fit the
faction's assigned role preset?). Scores and human 1-5 ratings/notes persist
alongside the game and surface in the frontend's Review tab, plus a
cross-game Insights tab comparing how each role preset tends to score on
average.

**Live play.** A viewer can watch a game over a WebSocket, take over any
faction's turns mid-game through a real action form, and hand it back to
the AI at any point (an idle human auto-falls-back to the AI after 45s, so
an abandoned tab can't stall a spectated game). Multiple browsers can watch
and control different factions in the same game simultaneously.

## Directory structure

```
order-wars/
├── agents/          # LangGraph agents: GameState, hierarchical decision graph, LLM fallback chain
├── map_data/        # Map generation script + committed provinces/rivers/cities GeoJSON
├── game/             # Pure rules engine (game/rules.py), narrative tagging, the game loop entrypoint
├── db/               # SQLAlchemy models + Alembic migrations (Neon Postgres, order_wars schema)
├── backend/          # FastAPI app: REST + WebSocket API, Google Sign-In, background game runner
├── frontend/         # Vite + vanilla JS: scenario editor, live game view, review, insights
├── eval/             # DeepEval metrics + batch eval runner, scored against Postgres
└── tests/            # pytest — fully mocked, no live LLM/DB calls
```

`CLAUDE.md` has the full build history and design rationale for every piece
above, phase by phase, including bugs found and fixed along the way — this
README is the shorter "what is this and how do I run it" version.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in at least one LLM provider key
```

Agents call an LLM through a fallback chain (`agents/llm.py`): Groq and
OpenRouter (free tiers) are tried first, Anthropic Claude is the paid last
resort — at least one provider key is required, unset ones are skipped. See
`.env.example` for provider keys, `DATABASE_URL` (Neon Postgres connection
string, separate from `NEON_API_KEY`), and the optional Google Sign-In
variables (`GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`, `SESSION_SECRET`).

## Run it

```bash
python -m agents.graph                  # quick demo: N factions, no persistence
python -m game.run_game                 # full game loop, persists to Postgres (needs DATABASE_URL)
uvicorn backend.main:app --reload       # HTTP/WebSocket API (needs DATABASE_URL)
python -m eval.run_eval --game-id <id>  # score a completed game with DeepEval
python map_data/generate_map.py         # regenerate provinces/rivers/cities.geojson (offline, not needed to just play)
alembic upgrade head                    # apply DB migrations (needs DATABASE_URL)
pytest tests/                           # fully mocked, no live API/DB calls
```

`game.run_game` and `backend/` need `DATABASE_URL` — they write every game
to Postgres. `agents.graph`'s demo doesn't persist anything, useful for a
quick check without a database configured. With the backend running,
`GET /docs` has the interactive API reference (FastAPI's Swagger UI).

### Frontend

```bash
cd frontend
npm install
cp .env.example .env   # only needed if the backend isn't on localhost:8000, or to enable Google Sign-In
npm run dev            # http://localhost:5173 — needs the backend running too
```

Build a scenario: add factions, pick each one's capital on the map (this
auto-claims a small starting territory cluster around it too — adjustable
by hand afterward), assign a role preset and starting resources/units, save
it, then start a game and watch it play live. Four tabs:

- **Scenario** — the editor above.
- **Games** — watch a game live (notable-event ticker, predict-the-winner
  mini-game, taking over a faction's turns yourself and handing it back to
  the AI anytime) or scrub through a finished game turn-by-turn with a
  replay slider.
- **Review** — DeepEval scores plus your own annotations per decision, with
  a plain-English metric glossary, per-metric trend sparklines, and a CSV
  export.
- **Insights** — how each role preset (Expansionist, Warmonger,
  Diplomat-Trader, Isolationist) tends to score, averaged across every
  evaluated game so far, not just one.

Signing in with Google (if `VITE_GOOGLE_CLIENT_ID`/backend `GOOGLE_CLIENT_ID`
are configured) attributes scenarios and annotations to your account — it's
additive, not a gate: every flow above works fully signed-out.

### LLM cost

Only one LLM call happens per faction per turn (the dispatched specialist's
decision) — the leader's `intent` refresh is a rule-based heuristic, not a
model call, and role-specialist schemas are narrow enough that a single
free-tier Groq/OpenRouter call comfortably covers each turn. Useful to know
if you're running on free-tier quota: a 3-faction, N-turn game costs
roughly 3×N LLM calls, not more.

## Tech stack

- **Agents/orchestration:** LangGraph, LangChain, Pydantic-structured tool
  calls, Groq / OpenRouter / Anthropic Claude via a fallback chain
- **Map:** geopandas, Shapely, H3 hex grids, Natural Earth data (offline
  generation only)
- **Persistence:** SQLAlchemy 2.0, Alembic, Postgres (Neon), schema-isolated
  under `order_wars`
- **Backend:** FastAPI, WebSockets, `google-auth`/`pyjwt` for Sign-In
- **Frontend:** Vite, vanilla JS, Leaflet
- **Eval:** DeepEval (custom rule-based metrics + `GEval` LLM-judged metric)
- **Tests:** pytest, fully mocked — no live LLM or database calls in the suite

## Project status

All planned phases are complete and verified live end-to-end (real LLM
calls, real Neon Postgres, real browser sessions — not just mocked tests):

- [x] Phase 0 — project scaffolding
- [x] Phase 1 — LangGraph agent basics (superseded by Phase 2's N-faction schema)
- [x] Phase 2 — multi-agent coordination
- [x] Phase 3 — map/geo generation
- [x] Phase 4 — connecting agents to the map (territory, resources, diplomacy)
- [x] Phase 5 — game loop, Postgres persistence, FastAPI/WebSocket backend, Leaflet frontend
- [x] Phase 6 — DeepEval scoring + human annotation
- [x] Gameplay-depth rollout (11 stages) — terrain, naval movement,
      multi-resource economy, unit composition/combat, multi-turn sieges,
      supply-line attrition, rebellion, province development, trade,
      tribute/vassalage, rule-triggered coalition wars, and narrative event
      tagging
- [x] Multi-level agent hierarchy — strategic leader + dispatched
      military/diplomatic/economic specialists, each with its own
      restricted action schema
- [x] Engagement/learnability pass — metric glossary, cross-game Insights
      tab, gamified annotation, live-play spectacle, letting a viewer take
      over a faction mid-game, replay scrubbing, multi-province starting
      territory, and a real cut in per-game LLM cost
- [x] Authentic map depth — desert/forest terrain, real rivers, real cities
- [x] Google Sign-In — real backend-verified accounts, additive (nothing
      in the app requires signing in)

See `CLAUDE.md`'s "Progress log" for the detailed history of what landed,
in what order, and why — including real bugs found during live
verification and how they were fixed.
