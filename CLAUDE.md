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
(selfish goals vs. team goals) via turn-wise play: each detective proposes a move, the other
four respond to it, and the mover then commits.

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
- **Phase 6:** Completed (Turn-wise detective play, replacing the simultaneous propose/debate/
  vote consensus loop — **ADR-0009**; then per-turn move application and the pawn-animation
  handshake that paces a round to the board — **ADR-0010**).

## Architecture & Code Rationale

### 1. Rule Enforcement (Zero Hallucination)

Every LLM output is treated as untrusted, and re-derived against the board before it can
affect game state. There are four independent layers, and each one is load-bearing:

- `agents.py:fetch_legal_moves` computes the mover's legal targets **before** building any
  prompt, so the model is only ever offered real options — every node another detective is
  standing on is excluded. Since ADR-0010 each move is applied at the end of its own turn, so
  that exclusion is exact rather than reconstructed, and it is what makes two detectives sharing
  a node **structurally impossible**: a constraint on what can be proposed, not a check applied
  afterwards.
- `agents.py:_enforce_legal_node` overrides whatever the model actually named if it isn't in
  that set. The mover gets one self-correction retry; the deterministic pass is what guarantees
  correctness. A responder's *advisory* preference is dropped rather than rewritten, so an
  invented node never feeds into the mover's decision prompt.
- `agents.py:_invoke` bounds every call with a real wall-clock deadline
  (`LLM_CALL_DEADLINE_SECONDS`), so a hung or slow call resolves deterministically instead of
  stalling the round — `llm_client.py`'s own `timeout=45` is only an idle-gap timeout
  (ISSUE-009), which mattered far less when calls ran concurrently.
- `graph.py:finalize_round_node` *asserts* destination uniqueness, and
  `round_resolver.py:resolve_round` re-derives legality once more before deducting a single
  ticket.

`game_master.py` still exposes this logic over MCP (`python -m scotland_yard.game_master`,
wired up in `.cursor/mcp.json`) for external clients such as an IDE assistant — but the
application itself calls the plain functions in-process. **See ADR-0001**, which records why
the original "MCP prevents hallucination" framing stopped being true and what replaced it.

- `llm_client.py` holds the two cached OpenRouter chat clients (a mover's proposal/decision,
  and a responder's answer). Neither binds tools.

### 2. The LangGraph State & Agents

- `rules_constants.py`: every value transcribed from `rules.md` (board pool, ticket
  inventories, `MAX_ROUND`, `SURFACING_ROUNDS`, `DETECTIVE_IDS`, `AGENT_DISPLAY_NAMES`) plus
  this project's own turn parameters (`COLLABORATION_TIERS`, `CALLS_PER_TURN`,
  `LLM_CALL_DEADLINE_SECONDS`). Imports nothing else in the package, so anything can import it.
  It asserts at import that the collaboration ladder is contiguous and covers every round up to
  `MAX_ROUND`, so retuning a boundary fails loudly rather than dropping a round off the table.

- `state.py`: defines `ScotlandYardState`. Tracks Mr. X's travel log (`transport_history`),
  ticket inventories, `committed_moves`/`turn_records`/`captured_by` for the round in progress,
  and dictionary reducers that merge each turn's update without overwriting the previous turns'.
  `detectives` and `mr_x` now change *during* a round, as each turn applies its own move.
  `recent_positions` is the one exception to "a round starts fresh": it carries forward, because
  it is the only thing that lets a detective notice it is shuttling between two nodes.

- `travel_log.py`: reads `transport_history` back out round-by-round (the flat log interleaves a
  `"double"` sentinel before a double-move's two hops). Imports nothing, so the module that
  writes the log and the module that narrows Mr. X's zone with it can agree on its format
  without importing each other.

- `agents.py`: `turn_node` — one detective's whole turn, six LLM calls (**ADR-0009**).
  - A turn is: the mover proposes and broadcasts (1 call) → the other four respond once each,
    sequentially, in cyclic order from the mover's successor (4 calls) → the mover commits
    (1 call). Responses are **advisory**: a responder states what it would do on its own turn
    but reserves nothing.
  - `get_psychology_prompt()` enforces 3 goals (1. Team Win, 2. Selfish Glory, 3. Efficiency).
    Underneath them, a collaboration tendency stated as a literal percentage scales how much
    weight a teammate's argument gets: rounds ≤4 → 1%, 5–8 → 25%, 9–12 → 50%, 13–16 → 75%,
    ≥17 → 99%. A prompt tier, not a random draw, so rounds stay reproducible.
  - Each detective has an internal id (`DETECTIVE_IDS`) and a human-readable callsign
    (`AGENT_DISPLAY_NAMES`, e.g. "Agent Red") used in all LLM-facing prompt text and the turn
    transcript, so agents refer to each other by callsign. Confirmed in practice: the model's
    own free-text rationale adopts these names unprompted.
  - Every call injects a "Mr. X Possible-Zone Context" giving detectives spatial grounding they
    otherwise have none of: where Mr. X could be, and each candidate's hop-distance to it. The
    zone is narrowed by his own travel log — one ticket-*typed* graph layer per hop he has
    logged since surfacing, not an untyped ball, which roughly halves it at every distance
    (**ADR-0013**, `travel_log.py` + `game_master.py:compute_mrx_zone_from_tickets`). Black
    tickets widen it back, by design. Recomputed per turn, since occupancy changes five times a
    round. See `game_mechanics.md` §1.
  - Candidates carry three more deterministic annotations, for the same reason the zone distance
    is computed rather than asked for — a small model gets board arithmetic quietly wrong:
    `onward_moves_after` (moves left next round, ticket already deducted — stops it stranding
    itself), `zone_size_after` (how much of Mr. X's escape space standing there closes off —
    expresses *containment*, which distance alone cannot, and is dropped when every candidate
    scores alike), and `you_were_here_recently` (the only memory a detective has across rounds,
    and what stops two nodes becoming a shuttle). Options are rendered best-first.
  - The objective and the annotation legend live in `get_psychology_prompt`, so all six calls in
    a turn share them. They used to sit only in the mover's proposal, which left the four
    responders — and the mover's own final, binding commit — with no stated objective at all.
  - `turn_node` emits a custom stream event **per LLM call**, which is what lets the Chat Log
    read as a conversation unfolding rather than a stage landing all at once.
  - `apply_detective_move` ends the turn by actually moving the detective, transferring its
    ticket, and deciding capture (**ADR-0010**) — so the next detective reasons about a board
    that already reflects it, exactly as `rules.md` §2 describes. The turn then waits for the
    client to finish animating that pawn (`session.await_pawn_settled`, bounded by
    `TURN_ACK_TIMEOUT_SECONDS`) before the next detective's first call goes out.

- `graph.py`: `build_detective_graph()` compiles the state machine (a factory, so a variant can
  be built for tests). It loops `turn` once per detective and then finalizes — or finalizes
  early if a detective caught Mr. X, since the game ends at that instant and the detectives
  behind it never move. There is no fallback resolution and no move application left to do:
  every turn ends in a move that already happened. `finalize_round_node` summarises the round
  from `turn_records` (the origin nodes are no longer derivable from state, because the
  detectives have left them) and asserts destination uniqueness.

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
- `POST /games/{id}/turn-ack` is how the board tells the backend a pawn finished animating,
  releasing the next detective's turn (**ADR-0010**). It deliberately does *not* take
  `session.lock` — the lock is held for the whole round by the stream route, and this request
  exists to unblock that loop from the inside.
- `round/stream` re-checks game status **inside** the session lock. A second subscriber to an
  already-resolved round gets a terminal `round_already_resolved` event instead of re-running
  the loop — which is what React StrictMode's double-invoke used to cause, at a full round of
  billable LLM calls a time (ISSUE-027).
- Most stream events are `agents.py`'s own per-call payloads, relayed verbatim
  (`turn_started`/`turn_proposal`/`turn_response`/`turn_decision`); only `round_finalized` and
  `round_result` come from `serializers.py:serialize_loop_event`.
- CORS defaults to the Vite dev origin and uvicorn binds `127.0.0.1`. Every endpoint is
  unauthenticated and the stream endpoint spends real money, so neither default is incidental.
- `session.py` keeps games in a plain in-memory dict with TTL eviction — **ADR-0005**. Each
  session also records the browser that owns it.
- **Access control without accounts (ADR-0014):** `POST /games` mints an opaque token, returns it
  in an `HttpOnly` cookie, and stores it on the session; every game-scoped route requires a
  match. A shared URL therefore carries the game id but not the cookie, so it grants nothing.
  Ownership is per-browser — there is no way to resume a game elsewhere, by design.
- `limits.py` caps concurrent games, new games per IP, and a rolling daily LLM-call budget
  (checked before a round starts, never mid-round). These are the polite layer; the real bound
  on a public deployment is a hard credit limit on the OpenRouter key itself.
- In a deployment the same app also serves the built SPA, so the frontend and API share one
  origin — which is what lets the ownership cookie ride along on the `EventSource` round stream,
  since `EventSource` cannot set headers. See the `Dockerfile` and **ADR-0014**.
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
- **Live AI turns** (`hooks/useRoundStream.ts`): renders one Chat Log entry per streamed LLM
  call, each tagged with the turn it belongs to, so `ChatLog` can group a round into five
  turns — the mover's proposal, four indented responses, the mover's decision. It also mirrors
  each applied move into game state as it arrives, which is what animates that detective's pawn
  and keeps ticket counts live mid-round, and acks the backend once the tween lands. On
  `round_result` it reports the fresh state up — which is also what resets the move wizard,
  with no coordination code between the two hooks.
- **Pawn movement:** every pawn, Mr. X's and all five detectives', is reused across renders and
  tweened to its new node over `PAWN_MOVE_DURATION_MS` (`boardDimensions.ts` — the Phaser-free
  module, so `useRoundStream` can read it without pulling Phaser into the main bundle).
- **Turn halo** (`BoardScene.ts:renderTurnHalo`, **ADR-0011**): a ring cycles across whichever
  pawn currently has the turn — Mr. X while `gameState.status === "awaiting_mr_x_move"`, then
  each detective in turn (`useRoundStream`'s `activeTurnDetective`, set on `turn_started` and
  cleared on `turn_decision`), back to Mr. X once the round resolves. Same dimensions/radius rule
  as the legal-target halo it was generalized from (`MR_X_HALO_*` renamed `TURN_HALO_*`); never
  needs to track a moving pawn, since a pawn only starts moving the instant its own turn ends.
- **Board pawns / Travel Log:** every pawn is hoverable; Mr. X's pawn is always rendered at his
  real node, alpha-toggled by whether he is currently surfaced (a reminder for the human
  player, not an information-hiding mechanism — ADR-0007). The Travel Log is a fixed 24-slot
  grid; `transport_history` is walked round-by-round, with a `"double"` sentinel collapsing a
  double-move's two hops into one slot.
- **Last-known-location ghost** (`BoardScene.ts:renderLastKnownGhost`, **ADR-0012**): a dashed,
  unfilled outline of the pawn shape (`data/ui/pawn_last_known.svg` — the exact same silhouette
  as `pawn.svg`, traced as a stroke-only path) at `mr_x.last_known_node`. Hidden before his first
  surfacing (`last_known_node` still `null`) and during the round he surfaces
  (`last_known_round === round_number`, since his real pawn is already opaque there that round);
  shown every other round.
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
