# Project: Scotland Yard Multi-Agent System

## Purpose

This file is Claude Code's auto-loaded project context — high-level project status and
architecture rationale: what phase the project is in, the major components and why they're
built the way they are. It sits one level above `docs/mechanics/game_mechanics.md`'s
implementation-level detail. It is not the place for game rules, mechanic-by-mechanic
implementation detail, or a bug/limitation log — those live in the docs below.

**Decisions with real alternatives now live in `docs/adr/`, not here.** This file says *what*
the architecture is; an ADR says *why that and not the other thing*. When adding a section
here that starts justifying a choice at length, that is the signal to write an ADR and link it.

## Documentation Map

| File | Purpose |
|---|---|
| `README.md` | GitHub-facing landing page — what the project is, plus prerequisites, setup, and how to run it. |
| `CLAUDE.md` (this file) | Project status and architecture rationale, for Claude Code sessions. |
| `docs/adr/` | Architecture Decision Records — one per load-bearing decision, with alternatives and consequences. |
| `docs/rules/rules.md` | Immutable source of truth for what the game rules *allow* — not how the code implements them. |
| `docs/mechanics/game_mechanics.md` | How the system *implements* each mechanic — algorithms, state transitions, LLM orchestration. |
| `docs/issues/known_issues.md` | The running log of known bugs, gaps, and non-goals — check here before investigating a weird behavior, and log new ones here once confirmed. |
| `frontend/README.md` | Frontend architecture, commands, and the constraints worth knowing before editing it. |
| `tools/README.md` | The board-data authoring utilities (not part of the running app). |

## Overview

A digital adaptation of the board game Scotland Yard. The backend is a multi-agent LangGraph
system driving 5 AI detectives against a human Mr. X, implementing game-theoretic behavior
(selfish goals vs. team goals) via sequential debate and structured voting.

**The agents cannot make an illegal move.** That guarantee comes from deterministic,
in-process validation at every stage — not from the LLM being asked nicely, and (since
ADR-0001) not from a protocol boundary either.

## Repository Layout

```
backend/
  scotland_yard/      The application package (see its __init__.py for a module map)
  tests/              pytest suite; `pytest` runs it, `pytest -m llm` adds the billable ones
  pyproject.toml      Exactly-pinned dependencies + pytest config
data/                 Immutable game data: board graph, node positions, board/pawn/ticket art
docs/                 Prose only — ADRs, rules, mechanics, issue log
frontend/             React 19 + Phaser 4 client
tools/                One-off board-data authoring utilities
```

`data/` is deliberately separate from `docs/`: the board graph is loaded at import by
`game_master.py`, so it is runtime data, not documentation. `.cursorrules` marks it immutable.

## Current Progress

- **Phase 1 & 2:** Completed (Environment, basic logic, JSON map data).
- **Phase 3:** Completed (Multi-Agent LangGraph System).
- **Phase 4:** Completed (React + Phaser.js Frontend).
- **Phase 5:** Completed (Architecture audit — see `known_issues.md` Group E and `docs/adr/`).

## Architecture & Code Rationale

### 1. Rule Enforcement (Zero Hallucination)

Every LLM output is treated as untrusted, and re-derived against the board before it can
affect game state. There are four independent layers, and each one is load-bearing:

- `agents.py:fetch_legal_moves` computes each detective's legal targets **before** building any
  prompt, so the model is only ever offered real options. It reserves both other detectives'
  current nodes *and* destinations already locked this round.
- `agents.py:find_proposal_conflicts` + `propose_node`'s deterministic backup enforcement
  checks and, if necessary, overrides the model's proposal. A proposer gets one self-correction
  retry; the deterministic pass is what actually guarantees correctness.
- `vote_node`'s tally discards any ballot entry that is illegal or duplicates another within
  the same ballot.
- `graph.py:finalize_round_node` de-duplicates fallbacks and then *asserts* destination
  uniqueness, and `round_resolver.py:resolve_round` re-derives legality once more before
  deducting a single ticket.

`game_master.py` still exposes this logic over MCP (`python -m scotland_yard.game_master`,
wired up in `.cursor/mcp.json`) for external clients such as an IDE assistant — but the
application itself calls the plain functions in-process. **See ADR-0001**, which records why
the original "MCP prevents hallucination" framing stopped being true and what replaced it.

- `llm_client.py` holds the two cached OpenRouter chat clients (propose/vote, and debate).
  Neither binds tools.

### 2. The LangGraph State & Agents

- `rules_constants.py`: every value transcribed from `rules.md` (board pool, ticket
  inventories, `MAX_ROUND`, `SURFACING_ROUNDS`, `DETECTIVE_IDS`, `AGENT_DISPLAY_NAMES`) plus
  this project's own consensus parameters (`VOTE_THRESHOLD`, `MAX_DEBATE_LOOPS`). Imports
  nothing else in the package, so anything can import it. It asserts
  `VOTE_THRESHOLD * 2 > NUM_DETECTIVES` at import — the invariant that makes cross-tally
  collisions arithmetically impossible (ISSUE-010).

- `state.py`: defines `ScotlandYardState`. Tracks Mr. X's travel log (`transport_history`),
  ticket inventories, and dictionary reducers that merge move proposals without overwriting.

- `agents.py`: the three primary nodes (`propose_node`, `debate_node`, `vote_node`).
  - `get_psychology_prompt()` enforces 3 goals (1. Team Win, 2. Selfish Glory, 3. Efficiency).
    Agents grow more desperate and willing to compromise as the round number approaches 24.
  - Debate is sequential: each detective speaks once per loop, in `DETECTIVE_IDS` order — the
    first speaker pitches proactively, every later speaker is told not to just agree.
  - Voting requires 3/5 to lock a move; up to 3 loops. **See ADR-0006** for why those numbers.
  - Each detective has an internal id (`DETECTIVE_IDS`) and a human-readable callsign
    (`AGENT_DISPLAY_NAMES`, e.g. "Agent Red") used in all LLM-facing prompt text and the debate
    transcript, so agents refer to each other by callsign. Confirmed in practice: the model's
    own free-text rationale adopts these names unprompted.
  - All three nodes inject a "Mr. X Possible-Zone Context" — a board-topology BFS
    (`game_master.py:compute_mrx_zone`/`compute_distances_to_zone`) giving detectives spatial
    grounding: where Mr. X could plausibly be, and each candidate move's hop-distance to that
    zone. Memoized per round. See `game_mechanics.md` §1.

- `graph.py`: `build_detective_graph()` compiles the state machine (a factory, so a variant can
  be built for tests). Loops propose/debate/vote up to 3 times; on failure to reach consensus,
  `finalize_round_node` falls back to each unlocked detective's own self-proposal —
  **de-duplicated** in `DETECTIVE_IDS` order, because independently-generated self-proposals
  can and do collide (ISSUE-026). It also computes `final_move_details` for the frontend Chat
  Log via `transport.py:determine_move_transport`, the same helper `resolve_round` uses to
  apply moves, so the two can never disagree about which ticket a move spends.

- `logging_config.py`: stderr-only logging (preserving `game_master.py`'s MCP stdio
  constraint package-wide), `LOG_LEVEL` env var, and a contextvar binding a game id into every
  line so concurrent games stay separable. `[VALIDATION]` lines — deterministic enforcement
  overriding an LLM — log at WARNING, which makes them the one thing worth alerting on.

### 3. The API Layer

- `server.py` (Starlette — **ADR-0002**) exposes `POST /games`, `GET /games/{id}`,
  `GET /games/{id}/map`, `GET /games/{id}/mrx/legal-moves`, `POST /games/{id}/mrx/move`, and
  `GET /games/{id}/round/stream` (SSE).
- `requests.py` validates request shapes with Pydantic at the route boundary, so a malformed
  body is a 400 with a field-level message rather than a 500 (ISSUE-028).
- `round/stream` re-checks game status **inside** the session lock. A second subscriber to an
  already-resolved round gets a terminal `round_already_resolved` event instead of re-running
  the loop — which is what React StrictMode's double-invoke used to cause, at ~15 billable LLM
  calls a time (ISSUE-027).
- CORS defaults to the Vite dev origin and uvicorn binds `127.0.0.1`. Every endpoint is
  unauthenticated and the stream endpoint spends real money, so neither default is incidental.
- `session.py` keeps games in a plain in-memory dict with TTL eviction — **ADR-0005**.
- `serializers.py` is the single choke point for outward-facing payloads. It includes Mr. X's
  real `current_node`, which is safe for a specific reason — **ADR-0007**.

### 4. Phase 4: Hybrid Frontend (React + Phaser)

Commands, component map, and the constraints worth knowing are in `frontend/README.md`.
Highlights:

- **Tech Stack:** React 19 (`react-router-dom` for `/` and `/game/:gameId`) + Phaser 4. No
  global state library — **ADR-0008**.
- **Bundle splitting:** `GameScreen.tsx` lazy-loads `BoardCanvas` so Phaser only downloads once
  a game is entered. Easy to undo by accident; CI now fails if the main chunk exceeds 600 kB
  (it should sit around 245 kB). See ISSUE-019 and `frontend/README.md`.
- **Layout:** the board pane is sized by height plus a CSS `aspect-ratio` derived from the
  board's own resolution, so it fills the viewport height exactly without letterboxing or
  distortion; the sidebar takes the remaining width via `flex: 1`.
- **Move Selector** (`hooks/useMrXMoveWizard.ts`): the single/double-move state machine — pick
  a legal node, choose a ticket, and for a double-move repeat for hop 2 using the preview
  endpoint before submitting both hops atomically.
- **Live AI debate** (`hooks/useRoundStream.ts`): renders `proposal`/`debate`/`vote_tally`/
  `round_finalized` SSE events into `ChatLog`. `stage_started` events fire the instant each
  stage's first LLM call goes out, so the header updates as a stage *begins* rather than when
  it finishes. On `round_result` it reports the fresh state up — which is also what resets the
  move wizard, with no coordination code between the two hooks.
- **Board pawns / Travel Log:** every pawn is hoverable; Mr. X's pawn is always rendered at his
  real node, alpha-toggled by whether he is currently surfaced (a reminder for the human
  player, not an information-hiding mechanism — ADR-0007). The Travel Log is a fixed 24-slot
  grid; `transport_history` is walked round-by-round, with a `"double"` sentinel collapsing a
  double-move's two hops into one slot.
- **Art** comes from `data/`, copied into `frontend/public/` by `scripts/sync-assets.mjs` on
  every dev/build. Edit the originals in `data/`; the copies are gitignored generated output.

## LLM Configuration

- **Model:** `deepseek/deepseek-v4-flash-0731` via OpenRouter, configured in `llm_client.py`,
  with reasoning **disabled**. The provider choice and the four-configuration reasoning-budget
  experiment behind that setting are recorded in **ADR-0004**.
- **Env:** `OPENROUTER_API_KEY` in `backend/.env` (see `backend/.env.example`). `.env` files are
  gitignored.

## Testing

```bash
cd backend && pytest          # fast: unit + API, no network, no API key
cd backend && pytest -m llm   # opt-in: real, billable LLM calls
cd frontend && npm run lint && npm run build
```

CI (`.github/workflows/ci.yml`) runs the fast suite, the frontend lint/build, and the bundle
size guard. It has no API key and should never be given one.
