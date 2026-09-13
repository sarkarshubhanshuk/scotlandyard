# Scotland Yard

A digital adaptation of the board game Scotland Yard. **Mr. X is played by a human**; the 5
detectives are played by LLM agents that propose, debate, and vote on their moves each round
via a LangGraph multi-agent system.

The agents never decide a move unsupervised: every proposal, every ballot, and the final
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

## Testing

```bash
cd backend
pytest                  # fast suite: unit + API tests, no network, no API key needed
pytest -m llm           # opt-in: real, billable LLM calls against a live OPENROUTER_API_KEY
```

```bash
cd frontend
npm run lint
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
