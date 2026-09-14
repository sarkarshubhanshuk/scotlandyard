# Scotland Yard

A digital adaptation of the board game Scotland Yard. **Mr. X is played by a human**; the 5
detectives are played by LLM agents that take turns each round via a LangGraph multi-agent
system. On its turn, a detective proposes a move, the other four respond to it one at a time,
and then it commits.

The agents never decide a move unsupervised: every proposal, every response, and the final
applied move are re-derived and validated against the board graph server-side in plain Python,
so an LLM cannot hallucinate an illegal move into the game state.

![Scotland Yard](docs/ui/game_logo.jpg)

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.12+ (developed on 3.14) | |
| Node.js | 20+ | For the Vite/React frontend. |
| OpenRouter API key | — | Free to create at [openrouter.ai/keys](https://openrouter.ai/keys). Required for the detective agents. |

## Setup

Clone the repository, then set up each half independently.

### 1. Backend

```bash
cd backend

# Create and activate a virtual environment
python -m venv venv
source venv/Scripts/activate     # Windows (Git Bash)
# venv\Scripts\activate.bat      # Windows (cmd)
# source venv/bin/activate       # macOS / Linux

# Install the backend plus its dev/test extras
pip install -e ".[dev]"

# Configure the LLM provider
cp .env.example .env
# ...then open .env and paste in your OPENROUTER_API_KEY
```

### 2. Frontend

```bash
cd frontend
npm install
```

The frontend defaults to a backend at `http://localhost:8000`. To point it elsewhere, copy
`frontend/.env.example` to `frontend/.env.local` and set `VITE_API_BASE_URL`.

## Running the game

The two halves run as separate processes, so use **two terminals**.

**Terminal 1 — backend** (serves the HTTP + SSE API on port 8000):

```bash
cd backend
source venv/Scripts/activate
python -m scotland_yard.server
# ...or, for auto-reload during development:
# uvicorn scotland_yard.server:app --reload
```

**Terminal 2 — frontend** (serves the UI on port 5173):

```bash
cd frontend
npm run dev
```

Then open <http://localhost:5173> and click **New Game**.

> **Games are in-memory only.** Restarting the backend discards every in-progress game, and the
> frontend will report the game as not found. This is a deliberate design decision — see
> [ADR-0005](docs/adr/0005-in-memory-session-store.md).

## Deploying it for other people

**Live:** <https://scotland-yard-e8cz.onrender.com>

The whole app - SPA and API - ships as one container (`Dockerfile`, built from the repository
root). It runs on **Render**, on a paid instance type (Render's free tier can't sleep mid-round
without breaking the turn-ack handshake below, and Hugging Face's equivalent free tier turned out
to require a PRO subscription for any Docker Space - see the **Update** in **ADR-0014** for why
the platform choice changed). Render builds straight from this Dockerfile on every push to
`main`, using whatever Docker or Docker-Compose-compatible host you prefer works the same way:

```bash
docker build -t scotland-yard .
docker run --rm -p 7860:7860   -e OPENROUTER_API_KEY=sk-...   scotland-yard
# then open http://localhost:7860
```

Set `OPENROUTER_API_KEY` as a platform secret/environment variable - never in the image;
`.dockerignore` excludes `.env` so it cannot be copied in by accident. The container listens on
whatever `PORT` the platform injects (Render's default is `10000`; `7860` is only the Dockerfile's
own fallback for a bare `docker run`), so no Dockerfile change is needed to move platforms.

**Run exactly one replica/instance.** Games live in memory and `/turn-ack` has to reach the very
coroutine awaiting it (ADR-0005, ADR-0010), so a second replica would strand acks on the wrong
process and stall every turn. See **ADR-0014** for the full reasoning. On Render, confirm the
service's Scaling settings are fixed at one instance rather than autoscaling.

Since the deploy is connected to this GitHub repo, **merging to `main` redeploys automatically** -
there is no separate push-to-deploy step to remember, but it also means a merge to `main` goes
live immediately.

### What protects a public link

| Concern | Control |
|---|---|
| Someone else playing your game from a shared URL | A per-browser token in an `HttpOnly` cookie; game routes 403 without it |
| The API key leaking | Server-side only, never serialized; supplied as a platform secret |
| Someone using the LLM for their own ends | No endpoint accepts free text - Mr. X's input is a node id and a ticket type, and prompts are built server-side |
| Someone running up the bill | `limits.py` caps concurrent games, games per IP, and a daily call budget - **plus a hard credit limit on the OpenRouter key**, which is the only bound that survives a bug |

Tunable via environment: `MAX_ACTIVE_GAMES`, `MAX_GAMES_PER_IP_PER_HOUR`,
`MAX_TRACKED_CLIENTS`, `DAILY_LLM_CALL_BUDGET`, `ALLOWED_ORIGINS`, `COOKIE_SECURE`,
`TRUST_PROXY_HEADERS`, `FRONTEND_DIST`, `HOST`, `PORT`.

> **`TRUST_PROXY_HEADERS` defaults to `true`, and should stay that way on Render.** The per-IP
> limit is keyed on `X-Forwarded-For`, which Render's proxy sets. Turning this off makes every
> visitor appear to come from the proxy itself, which collapses them into one bucket and turns
> `MAX_GAMES_PER_IP_PER_HOUR` into a *global* cap. Set it to `false` only when the container is
> exposed directly, with nothing in front of it. The header is spoofable either way, so the
> per-IP cap is a speed bump, not an identity — `MAX_TRACKED_CLIENTS` bounds what spoofing can
> cost, and the daily budget plus the key's own credit limit bound the spend.

A completed 24-round game is roughly **720 LLM calls**, so size the budget accordingly. Games
do not survive a restart, redeploy or sleep - that is ADR-0005, not a deployment bug.

## Testing

```bash
cd backend
pytest                  # fast suite: unit + API tests, no network, no API key needed
pytest -m llm           # opt-in: real, billable LLM calls against a live OPENROUTER_API_KEY
```

```bash
cd backend
ruff check .            # lint
mypy                    # type-check (scoped - see [tool.mypy] in pyproject.toml)
```

```bash
cd frontend
npm run lint
npm test                # Vitest: the two hooks' state machines
npm run build           # also guards the Phaser bundle-split (see frontend/README.md)
```

## Project structure

```
backend/                 Python: game engine, LangGraph agents, MCP server, HTTP API
  scotland_yard/         The application package
  tests/                 pytest suite
  pyproject.toml         Pinned dependencies and pytest configuration
data/                    Immutable game data: board graph, node positions, board/pawn/ticket art
docs/                    Prose documentation only (see the map below)
frontend/                React 19 + Phaser 4 client
tools/                   One-off authoring utilities for the board data
```

## Documentation

| File | Purpose |
|---|---|
| `CLAUDE.md` | Project status and architecture rationale — start here for the technical picture. |
| `docs/adr/` | Architecture Decision Records — why the load-bearing choices were made. |
| `docs/rules/rules.md` | The official game rules (source of truth for what's *allowed*). |
| `docs/mechanics/game_mechanics.md` | How the system *implements* each mechanic. |
| `docs/issues/known_issues.md` | The running log of known bugs and limitations. |
| `frontend/README.md` | Frontend architecture, commands, and constraints. |
| `tools/README.md` | The board-data authoring utilities. |

## License

MIT
