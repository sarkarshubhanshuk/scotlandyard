# Project: Scotland Yard Multi-Agent System

## Purpose

This file is Claude Code's auto-loaded project context — high-level project status and
architecture rationale: what phase the project is in, the major components and why they're
built the way they are. It sits one level above `docs/mechanics/game_mechanics.md`'s
implementation-level detail. It is not the place for game rules, mechanic-by-mechanic
implementation detail, or a bug/limitation log — those live in the docs below.

## Documentation Map

| File | Purpose |
|---|---|
| `README.md` | GitHub-facing landing page — what the project is, links into the docs below. |
| `CLAUDE.md` (this file) | Project status and architecture rationale, for Claude Code sessions. |
| `docs/rules/rules.md` | Immutable source of truth for what the game rules *allow* — not how the code implements them. |
| `docs/mechanics/game_mechanics.md` | How the system *implements* each mechanic — algorithms, state transitions, LLM orchestration. |
| `docs/issues/known_issues.md` | The running log of known bugs, gaps, and non-goals — check here before investigating a weird behavior, and log new ones here once confirmed. |
| `frontend/README.md` | Generic Vite/React tooling reference for the frontend dev environment — not project-specific documentation. |

## Overview

A digital adaptation of the board game Scotland Yard. The backend uses a multi-agent LangGraph system featuring 5 AI-driven detectives. The game implements game-theoretic behavior (selfish goals vs. team goals) via sequential debate and structured voting. The system uses the Model Context Protocol (MCP) to strictly enforce game rules and prevent LLM hallucination.

## Current Progress

- **Phase 1 & 2:** Completed (Environment, basic logic, JSON map data).

- **Phase 3:** Completed (Multi-Agent LangGraph System).

- **Phase 4:** Completed (React + Phaser.js Frontend).

## Architecture & Code Rationale

### 1. The MCP Bridge (Zero Hallucination Enforcement)

The AI agents cannot simply guess their moves. They must query a local Game Master server.

- `backend/game_master.py`: An MCP Server built with `fastmcp`. It holds the `map_graph.json` and exposes tools like `get_valid_moves`. We route its logs to `sys.stderr` to prevent standard output from corrupting the JSON-RPC data stream.

- `backend/mcp_client.py`: Uses Langchain's `MultiServerMCPClient` to manage a persistent, stateless connection to the `game_master.py` server. This ensures the connection does not drop during long LangGraph debate cycles.

### 2. The LangGraph State & Agents

- `backend/state.py`: Defines `ScotlandYardState`. Tracks Mr. X's travel log `transport_history`), ticket inventories, and handles dictionary reducers to merge move proposals without overwriting data.

- `backend/agents.py`: Contains three primary nodes `propose_node`, `debate_node`, `vote_node`). 

  - Uses `get_psychology_prompt()` to enforce 3 goals (1. Team Win, 2. Selfish Glory, 3. Efficiency). Agents become more desperate and willing to compromise as the round number approaches 24.

  - Debate is sequential (D1 speaks, D2 replies, etc.).

  - Voting requires a 3/5 threshold to lock a move.

  - All three nodes inject a "Mr. X Possible-Zone Context" — a server-side board-topology BFS (`game_master.py:compute_mrx_zone`/`compute_distances_to_zone`, called in-process, not via MCP) giving detectives spatial grounding: where Mr. X could plausibly be, and each candidate move's hop-distance to that zone. See `docs/mechanics/game_mechanics.md` §1 for the full design (and why it's a single per-move number, not a full distance matrix).

- `backend/graph.py`: The State Machine router. It loops the propose/debate/vote cycle up to a maximum of 3 times per round. If consensus fails after 3 loops, it triggers a fallback where agents execute their self-proposed moves.

- `backend/test_phase3.py` / `backend/test_full_round_e2e.py`: async testing scripts that drive `detective_graph` end-to-end via real LLM calls; both log every LLM call's full input/output to a transcript file for debugging agent behavior.

### 3. Phase 4: Hybrid Frontend (React + Phaser)

- **Tech Stack:** React 19 (UI layer, via `react-router-dom` for `/` and `/game/:gameId`) + Phaser 4
  (Canvas layer — `phaser` had no pinned major version when installed; the classic Phaser 3 APIs
  used here are unaffected). No global state library (Zustand, considered up front, turned out
  unnecessary — `GameScreen`/`LoadedGame` lift the one `PublicGameState` and pass it down to the
  board and sidebar, which was enough).

- **Layout:** 75% Left Pane (`BoardCanvas`, one Phaser `Scene` mounted once and updated
  imperatively via `updateGameState`/`updateHighlights` rather than recreated per render), 25%
  Right Pane (`TicketInventory`, `MoveSelector`, `ChatLog`, `TravelLog`, stacked).

- **Backend additions this phase required** (`backend/server.py`, `game_master.py`, `mrx_turn.py`):
  `GET /games/{id}/map` (serves `map.json` + `node_positions.json` — the frontend never bundles
  its own copy, keeping the backend the single source of truth for board data), and
  `GET /games/{id}/mrx/legal-moves` extended with optional `from_node`/`ticket_type_spent` query
  params to preview a double-move's hop-2 options (reuses the same legality logic
  `submit_mr_x_move` itself trusts, rather than duplicating it in TypeScript).

- **Move Selector** (`hooks/useMrXMoveWizard.ts`): the single/double-move state machine — pick a
  legal node on the board, choose a ticket type, and for a double-move, repeat for hop 2 using
  the preview endpoint above before submitting both hops atomically.

- **Live AI debate** (`hooks/useRoundStream.ts`): opens `GET /round/stream` (SSE) as soon as the
  game enters `detective_loop_running`, renders `proposal`/`debate`/`vote_tally`/`round_finalized`
  events into `ChatLog`, and on the terminal `round_result` reports the fresh game state back up
  — which is also what makes the move wizard reset itself for the next round, with no extra
  coordination code needed between the two hooks.

- **Travel Log** (`components/TravelLog.tsx`): renders Mr. X's full `transport_history` as ticket
  icons (always visible, per rules.md) plus a single "last known position" line. It does **not**
  attempt to show a per-round history of past surfacing reveals — `state.py`'s `MrXState` only
  ever retains the *latest* `last_known_node`/`last_known_round`, so anything earlier is no longer
  available from the backend to reconstruct.

- **Game over**: `GameOverBanner` reads `status`/`winner` only. Since Mr. X's real position is
  never serialized (even at game end), a "detectives win" is deliberately **not** worded as a
  capture — it could equally be rules.md's "Mr. X has no legal move" condition, which the client
  has no way to distinguish.

## LLM Configuration

- **Model:** `deepseek/deepseek-v4-flash-0731`, served via OpenRouter (an OpenAI-API-compatible
  aggregator), configured in `backend/mcp_client.py`. Previously Gemini's free tier - switched
  after its 15 requests/minute cap turned out too low to sustain a single propose/debate/vote
  loop (~15 calls), which meant votes could never actually pass.

- **Env:** API keys are managed via `.env` files (excluded via `.gitignore`). Requires
  `OPENROUTER_API_KEY` in `backend/.env`.