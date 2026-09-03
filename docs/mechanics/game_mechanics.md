# Scotland Yard: Game Mechanics (Implementation Reference)

## Purpose

This document explains **how the system implements** game mechanics — the actual algorithms,
state transitions, and LLM orchestration behind them. It is a companion to, not a replacement
for, the following:

- `docs/rules/rules.md` — the official Scotland Yard rules. States *what* is allowed. Immutable
  source of truth (see `.cursorrules`).
- `docs/map/map.json` — the board graph. Immutable source of truth.
- `CLAUDE.md` — high-level project history, phase status, and architecture summary.

This file goes one level deeper than `CLAUDE.md`: it's where new contributors (human or AI)
should look to understand exactly how a given mechanic works before touching the code that
implements it.

**Known issues:** This file is scoped to explaining how the mechanics work as designed — it
does not track bugs, gaps, or unfinished work. For the current list of known issues affecting
anything described below, see **`docs/issues/known_issues.md`**.

**Players:** Mr. X is played by a human. The 5 detectives are played by LLM agents. This is a
deliberate design choice for the game, not a gap — there is no Mr. X decision-making mechanic
in this codebase by design, and none should be added unless explicitly requested. Phase 4 (the
React + Phaser.js frontend) is where Mr. X's human input surface belongs; it has no equivalent
on the detective side.

## Document Conventions

Every mechanic below is documented as its own `##` section, using this fixed template:

- **Overview** — one or two sentences on what the mechanic does.
- **Trigger** — what invokes it, and how often.
- **Flow** — the actual step-by-step sequence.
- **State Involved** — which `ScotlandYardState` fields (`backend/state.py`) it reads/writes.
- **Implementation References** — `file.py:function` pointers. Code is not duplicated here;
  if the doc and the code disagree, the code wins and this doc needs updating.
- **Design Rationale** — the *why* behind non-obvious choices.

Known bugs and limitations are not part of this template — see **`docs/issues/known_issues.md`**
for those instead.

When a new mechanic (Mr. X movement, ticket economy, capture/win detection, double-moves,
surfacing turns, etc.) is implemented, add a new `##` section here following the same template
and link it from the Table of Contents below.

## Table of Contents

1. [Detective Move Decision Cycle](#1-detective-move-decision-cycle)
2. [Round Resolution, Mr. X's Turn, and the API Layer](#2-round-resolution-mr-xs-turn-and-the-api-layer)

---

## 1. Detective Move Decision Cycle

### Overview

Once per round, the 5 detectives collectively decide their next moves through a structured
propose → debate → vote cycle, repeated up to 3 times ("loops") until every detective's move
has either passed a majority vote or the loop budget is exhausted, at which point any
undecided detectives fall back to their own last self-proposed move.

### Trigger

Runs once per round, orchestrated by the `detective_graph` LangGraph state machine
(`backend/graph.py`). The graph is invoked fresh per round (it has no memory between separate
`.astream()`/`.ainvoke()` calls); chaining rounds is done externally via `build_next_round_state`
(see Round Boundaries below).

### Flow

```
START -> propose -> debate -> vote -> [router] -> propose (loop) OR finalize -> END
```

**Phase 1 — Propose** (`propose_node`)
- Every detective (all 5, regardless of lock status) independently proposes a move for every
  *still-undecided* detective. A detective whose own move already locked earlier this round
  keeps proposing — they still have a stake in where the remaining detectives go.
- Before proposing, the node calls the `get_valid_moves` MCP tool (`backend/game_master.py`)
  for each still-undecided detective, passing `occupied_nodes` = every detective's current
  position **plus every node already in `locked_moves`**. This returns the true legal move set
  (ticket-legal, unoccupied, *and* not already claimed as a locked detective's destination),
  computed server-side rather than trusted from the LLM. Folding locked destinations into
  `occupied_nodes` is what enforces rule (d) below — from loop 2 onward, a locked detective's
  future node is simply never offered as legal to anyone else, even though that detective is
  still physically standing at their old node until `finalize`.
- The structured-output schema requested from the LLM (`build_strategy_schema`) is built fresh
  each loop and contains fields **only** for still-undecided detectives — a detective already
  locked is never re-asked about, which is what keeps the schema (and token cost) shrinking as
  a round progresses.
- The prompt also names any "contested" nodes — ones reachable by more than one still-undecided
  detective this turn — so the proposer sees exactly where a collision is possible, and
  instructs it to keep every destination in its response distinct.
- After the LLM responds, `find_proposal_conflicts` checks the raw proposal for out-of-bounds
  or duplicate destinations. If any are found, the *same* proposer gets exactly one retry: a
  follow-up prompt naming the specific conflicts and asking for a corrected full proposal. This
  is a best-effort quality improvement, not a correctness guarantee — see the deterministic
  pass below, which fixes whatever the retry doesn't.
- Every proposal (retried or not) then goes through deterministic, code-enforced reconciliation,
  processing targets in fixed `DETECTIVE_NAMES` order so conflicts resolve in favor of the
  earlier detective:
  - **(a) Must move if possible** — a target is only left at their current node when no legal,
    unclaimed move exists for them; it is never the default outcome for an illegal or
    duplicate proposal.
  - **(b) Within legal bounds** — the destination must be in that target's own precomputed
    legal-move set.
  - **(c) Unique within this call** — a destination already claimed by an earlier target in the
    same proposal is rejected, even if it would otherwise be legal for this target too.
  - **(d) Not a locked destination** — already guaranteed by (b), since locked nodes were
    excluded from every target's legal-move set up front.
  - When (a)-(c) reject a proposed node, the target is reassigned to the lowest-numbered
    remaining legal-and-unclaimed node for them; only if none remains do they stay at their
    current node. Locked detectives' slots are always filled from `locked_moves` directly,
    never from the LLM's output.
- The 5 proposals (and their conflict-triggered retries) run concurrently (`asyncio.gather`),
  since they're blind/independent of each other — no detective sees another's proposal before
  making their own.
- Every legal-move candidate is also annotated with `distance_to_mrx_zone` — see "Mr. X
  Possible-Zone Context" below.

**Mr. X Possible-Zone Context** (shared by all three phases — `agents.py:compute_mrx_zone_context`)
- Computed once per node invocation (not per detective) from `mr_x.last_known_node`/
  `last_known_round` and the board graph, then injected as prompt text into every propose,
  debate, and vote call. Gives detectives spatial grounding they otherwise have none of — see
  `docs/issues/known_issues.md` ISSUE-005.
- **The zone**: every node reachable from Mr. X's last-known node within
  `min(round_number - last_known_round, 4)` hops, blocked through currently-occupied detective
  nodes (per rules.md's "Mr. X cannot move to, or pass through, a Node occupied by a
  Detective"), ticket-blind (documented tradeoff — `known_issues.md` ISSUE-015). Shown as a
  literal node list when ≤20 nodes; above that, only the count — a larger list would cover most
  of the 199-node board and add prompt cost without adding real narrowing-down value. `None`
  (handled explicitly, with a "hasn't surfaced yet" message) during rounds 1-2.
- **The distance signal**: one multi-source BFS from the whole zone gives every board node's
  hop-distance to the nearest zone node. Rather than a full candidate-move × zone-node matrix
  (which would run to thousands of numbers at wide hop counts), each candidate move, each
  already-proposed destination (in debate), and each detective's own current position gets a
  single `distance_to_mrx_zone` integer — 0 means that node IS a possible current location.
- All three prompts also gained a "use only the data given to you in this prompt, do not seek
  outside information" instruction (`get_psychology_prompt`), added alongside this context.

**Phase 2 — Debate** (`debate_node`)
- Strictly sequential: Detective 1 speaks first, then Detective 2 (who sees D1's remark),
  through Detective 5 (who sees the full transcript so far). This is the one phase that cannot
  be parallelized, by design — each speaker is meant to react to what was already said.
- All 5 detectives speak every loop, including already-locked ones.
- Each proposer's already-proposed destinations are annotated with `distance_to_mrx_zone_per_move`
  (see "Mr. X Possible-Zone Context" above), so debaters can argue about whether a plan actually
  closes in on him.

**Phase 3 — Vote** (`vote_node`)
- Every detective casts one ballot, voting on a target node for every *still-undecided*
  detective only (same dynamic-schema shrinking as the propose phase, via
  `build_ballot_schema`). Ballots are simultaneous/independent (no voter sees another's
  ballot), so the 5 calls run concurrently.
- The prompt also shows each still-undecided detective's legal-move candidates (mirroring
  `propose_node`'s display) annotated with `distance_to_mrx_zone` — voters are no longer voting
  blind on distance/positioning, though they still don't see the proposers' own rationale text
  (`known_issues.md` ISSUE-004, still open).
- Votes are validated against the same legal-move set computed for this phase; an illegal vote
  is discarded before tallying, never counted.
- Within one voter's own ballot, a vote is also discarded if it duplicates a node the same
  voter already cast for a different still-undecided detective (checked in the ballot's fixed
  `pending_targets`/`DETECTIVE_NAMES` order, so the earlier-listed detective's vote wins).
  Unlike a proposal, a discarded vote has no fallback to reassign — it's simply not counted,
  since no rule requires a voter to vote for every target. This does not cover a *different*
  voter's ballot naming the same node for a different detective — see Known Limitations.
- A still-undecided detective's move **locks** the moment it receives ≥3 of 5 votes (simple
  majority threshold, hardcoded in `vote_node`).

**Router** (`check_vote_status` in `graph.py`)
- If all 5 detectives are locked → go to `finalize`.
- Else if `debate_loop_count >= 3` → go to `finalize` anyway (loop budget exhausted).
- Else → loop back to `propose` for another round of the same three phases, now with fewer
  still-undecided detectives (and correspondingly smaller schemas/prompts).

**Phase 4 — Finalize** (`finalize_round_node`)
- Any detective that locked via voting takes their locked node.
- Any detective that never locked falls back to the move they most recently proposed for
  *themselves* (`proposed_strategies[det_id]["proposed_board_moves"][det_id]`).
- If even that's missing (e.g. every proposal attempt for them errored), the emergency fallback
  is to stay at their current node.

### Round Boundaries

`locked_moves`, `proposed_strategies`, and `debate_loop_count` are intentionally cumulative
*within* a round (that's how the loop tracks who's already decided). They do **not** reset
themselves — `detective_graph` has no cross-invocation memory, so resetting is the caller's
responsibility. `build_next_round_state()` (`backend/graph.py`) takes a completed round's final
state and returns a fresh `initial_state` for the next round: `round_number` incremented,
`debate_loop_count: 0`, `locked_moves: {}`, `proposed_strategies: {}`, `final_moves: {}`,
`messages: []`, while carrying `detectives` and `mr_x` forward unchanged. It does **not** apply
`final_moves` to positions or deduct tickets — see Known Limitations.

### State Involved

| Field | Type | Role |
|---|---|---|
| `round_number` | `int` | Drives the psychology/desperation curve (see below) |
| `debate_loop_count` | `int` | Current loop (0-indexed internally, capped at 3) |
| `locked_moves` | `Dict[str, int]` | Detective → node, accumulates within a round |
| `proposed_strategies` | `Dict[str, DetectiveStrategy]` | Latest proposal per proposer, incl. rationale |
| `final_moves` | `Dict[str, int]` | Output of `finalize_round_node`; the round's resolved moves |
| `messages` | `List[BaseMessage]` | Debate transcript (one `AIMessage` per loop) |
| `detectives` | `Dict[str, Detective]` | Current positions/tickets; read-only input to this cycle |

### Implementation References

- `backend/agents.py:get_psychology_prompt` — round-based desperation curve (arrogant/selfish
  through round 12, compromising through round 18, panicked/consensus-seeking after)
- `backend/agents.py:build_strategy_schema`, `build_ballot_schema` — dynamic per-loop schemas
- `backend/agents.py:find_proposal_conflicts` — detects (b)/(c) violations in a raw proposal,
  used to decide whether a proposer gets its one retry
- `backend/agents.py:propose_node`, `debate_node`, `vote_node`
- `backend/graph.py:check_vote_status`, `finalize_round_node`, `build_next_round_state`
- `backend/game_master.py:get_valid_moves` — ticket + occupancy legality, server-side
- `backend/mcp_client.py:get_detective_llm` — cached LLM/tool binding shared across all nodes
- `backend/game_master.py:compute_mrx_zone`, `compute_distances_to_zone` — the board-topology
  BFS behind the Mr. X Possible-Zone Context described above
- `backend/agents.py:compute_mrx_zone_context`, `format_mrx_zone_block`, `zone_distances_for_moves`
  — builds and renders that context into each of the three prompts

### Design Rationale

- **3-vote majority threshold**: matches "most of the team agrees" without requiring full
  unanimity, which would make consensus nearly impossible with 5 independently-motivated agents.
- **Server-side legality (MCP)**: the LLM cannot be trusted to reliably honor prompt-only
  constraints, so both ticket legality and node-occupancy are computed by `get_valid_moves` and
  re-validated in code after every LLM response, rather than relied upon as prompted behavior.
  (Current model: `deepseek/deepseek-v4-flash-0731` via OpenRouter — see `CLAUDE.md`.)
- **Dynamic schemas that shrink each loop**: once a detective locks, neither proposers nor
  voters are ever asked about them again — this is a direct token/latency saving, not just a
  correctness nicety.
- **Locked detectives keep proposing/debating/voting for others**: their own outcome is
  decided, but they still have a legitimate stake (team win, or watching a rival get the
  "glory") in where the remaining detectives end up.
- **Propose and vote run concurrently; debate does not**: propose and vote are simultaneous/
  independent decisions by nature (sealed proposals, secret-ish ballots), so parallelizing them
  only changes latency, not outcome. Debate is deliberately sequential — the whole point is
  that each speaker reacts to what was already said.
- **Prompt-level conflict hints + one retry, backed by deterministic code enforcement**: the
  LLM can't be trusted to reliably self-enforce "propose distinct nodes" on prompt instructions
  alone (see `known_issues.md` ISSUE-002), but a stronger model shouldn't need to pay for the
  same heavy-handed correction every time either. Naming contested nodes up front and
  allowing one feedback-guided retry reduces how often the deterministic reconciliation pass
  has to intervene, but that pass — not the prompt or the retry — is what actually guarantees
  rules (a)-(d) hold, independent of which LLM is behind `get_detective_llm`.
- **One scalar per move, not a full distance matrix, for the Mr. X zone context**: an earlier
  design pass considered giving every candidate move its distance to *every* node Mr. X could
  possibly be at, but a real check against `docs/map/map.json` showed the zone can cover up to
  ~84% of the board at 4 hops — a full matrix would run to thousands of numbers per call in the
  worst case, adding real cost and complexity right on top of already-logged latency/reliability
  issues (`known_issues.md` ISSUE-006/007/009). A single "distance to the *nearest* possible
  node" per candidate keeps the added cost to a measured ~304 tokens per call while still
  answering the actual decision-relevant question ("does this move get me closer"). The
  tradeoff, accepted deliberately: this scalar alone can't support true multi-detective
  triangulation (knowing several detectives' distances to *some* plausible node doesn't reveal
  whether they're all closing in on the same spot or covering different ones) — it's a
  "getting warmer/colder" signal per detective, not a coordinated-encirclement one.

---

## 2. Round Resolution, Mr. X's Turn, and the API Layer

### Overview

Everything §1 leaves unfinished: applying `final_moves` to the board, deducting/transferring
tickets, detecting capture and the other win conditions, handling Mr. X's own (human-played)
turn including double-moves and surfacing reveals, and a live server a browser frontend can
actually talk to.

### Trigger

One full round = Mr. X's human-submitted turn (`backend/mrx_turn.py`), then the detective
decision cycle (§1, `detective_graph`), then round resolution (`backend/round_resolver.py`).
Orchestrated per-game by a `GameSession` (`backend/session.py`) and driven externally via the
Starlette API (`backend/server.py`).

### Flow

**Game creation** (`session.create_game`)
- Randomly draws 6 unique starting nodes from the rules' pool for Mr. X + the 5 detectives (or
  accepts `seed_positions` for deterministic tests), assigns rules-mandated starting tickets,
  and sets `round_number=1`. Games live in a plain in-memory `GAMES` dict — no persistence, no
  TTL; a game exists only as long as the server process does, matching "a page refresh discards
  the game."

**Mr. X's turn** (`mrx_turn.submit_mr_x_move`)
- `get_mr_x_legal_moves` computes Mr. X's real legal destinations (occupied nodes always
  excluded) annotated with which ticket type(s) could pay for each — an empty result means Mr.
  X truly cannot move this round, which is an immediate Mr. X loss under the rules (he can never
  forfeit), checked proactively rather than discovered via a rejected submission.
- A single request carries an entire move — one hop, or both hops of a double-move
  (`{"hop1": {...}, "hop2": {...}}`) — and nothing commits unless the whole thing validates.
  There is no cross-request "pending double-move" state; the human client decides both hops
  itself before submitting.
- Each hop names its own `ticket_type_spent` explicitly (never inferred) — unlike detectives,
  Mr. X is a human who gets the real tactical choice of spending a matching ticket vs. a black
  ticket (mandatory for `boat` connections). A double-move's second hop is validated against
  tickets remaining **after** the first hop's deduction (a hand-built snapshot, not Mr. X's live
  inventory), so spending the same scarce ticket type on both hops is correctly rejected.
- On a surfacing round (3/8/13/18/24), a double-move reveals only the **intermediate** node
  (`last_known_node`/`last_known_round`) — the final destination stays hidden, same as a
  non-surfacing round. (Confirmed with the project owner: the rules' literal text describes this
  case ambiguously, since surfacing is a per-round, not per-hop, property.)
- On success, flips `session.status` to `"detective_loop_running"` — this is the signal the API
  layer uses to know the round-stream endpoint is now live.

**Detective loop + resolution** (`round_resolver.run_detective_loop`, `round_resolver.resolve_round`)
- `run_detective_loop` drives `detective_graph.astream(state, stream_mode=["updates", "values"])`
  — `"updates"` chunks identify which node just ran for event labeling, the last `"values"`
  chunk becomes the new `session.state` directly, reusing `state.py`'s own reducers rather than
  reimplementing them.
- `resolve_round` applies `final_moves` to `detective_1..5` **sequentially, in fixed order**,
  never trusting the graph's output for legality (re-derived via `game_master.compute_valid_moves`
  — the same posture §1 already applies to every LLM response). When a target node is reachable
  via more than one transport type (verified real case: map.json's node 1 ↔ node 46 via both bus
  and metro), `pick_transport` deterministically prefers whichever type the detective holds the
  most tickets of, tie-broken taxi > bus > metro — a server-side apply-time decision, not
  something detectives ever choose themselves (see Design Rationale).
- **Capture is checked after EVERY individual detective's move**, not once at the end — the
  instant a detective's new node equals Mr. X's real `current_node`, the round stops and the
  remaining detectives never move.
- If no capture: checks whether Mr. X now has any legal move at all (detectives win if not),
  then whether all 5 detectives are simultaneously trapped (Mr. X wins if so), then whether
  `round_number == 24` was just completed (Mr. X wins). Otherwise calls `build_next_round_state`
  (§1) unchanged and sets `status = "awaiting_mr_x_move"` for the next round.

**API layer** (`backend/server.py`, `backend/serializers.py`)
- `POST /games`, `GET /games/{id}`, `GET /games/{id}/mrx/legal-moves`,
  `POST /games/{id}/mrx/move`, `GET /games/{id}/round/stream` (SSE via `sse_starlette`, chosen
  over WebSocket since this is one-directional server→client data once opened).
- `serializers.serialize_public_state` is the **single choke point** every route and streamed
  event goes through to build an outward-facing payload — this is what structurally guarantees
  `mr_x.current_node` can never leak to a client, rather than relying on handler-by-handler
  discipline. Only `mr_x`'s ticket counts, `last_known_node`/`last_known_round`, and
  `transport_history` are ever exposed for him (ticket counts are public per the rules'
  Inventory Visibility rule; position is not).

### State Involved

| Field | Role in this mechanic |
|---|---|
| `mr_x.current_node` | Mr. X's real, secret position — added in this mechanic; never read by any detective-facing code path (§1) and never serialized by `serialize_public_state` |
| `mr_x.last_known_node` / `last_known_round` | Set only by `mrx_turn` on a surfacing-round move |
| `mr_x.transport_history` | Appended to by `mrx_turn` on every hop (records the ticket type spent, not necessarily the underlying route type — a black ticket is logged as `"black"`, matching the rules' obfuscation intent) |
| `detectives[*].node_id`, ticket counts | Mutated by `resolve_round`, never by the graph itself |
| `final_moves` | Read (never written) by `resolve_round`; still produced exactly as §1 describes |

### Implementation References

- `backend/state.py` — `MrXState.current_node` (additive field)
- `backend/game_master.py:compute_valid_moves` — pure function extracted from the `get_valid_moves`
  MCP tool so server-side code can call it in-process, without the MCP stdio subprocess round-trip
  that only LLM tool-calling actually needs
- `backend/session.py:GameSession`, `create_game`
- `backend/mrx_turn.py:get_mr_x_legal_moves`, `submit_mr_x_move`
- `backend/round_resolver.py:pick_transport`, `run_detective_loop`, `resolve_round`
- `backend/serializers.py:serialize_public_state`, `serialize_loop_event`
- `backend/server.py` — the Starlette app and its routes
- `backend/test_phase4_resolve.py`, `backend/test_api_smoke.py` — verification (see Known
  Limitations for what these do *not* cover)

### Design Rationale

- **Transport-type ambiguity resolved at apply-time, not in the LLM schema**: extending §1's
  `build_strategy_schema`/`build_ballot_schema` to also require a transport-type field would mean
  rewriting the just-hardened duplicate/legality/contested-node logic for negligible strategic
  value — a detective's target-node choice is what its psychology prompt reasons about, not the
  specific ticket spent to get there. `pick_transport`'s "most remaining tickets, tie-broken
  taxi > bus > metro" rule is a reasonable default that conserves the scarce metro allotment; it
  can be revisited if ticket-economy strategy ever becomes a design priority.
- **Starlette, not FastAPI**: `starlette`, `uvicorn`, `sse-starlette`, and `websockets` were
  already present as transitive installs; FastAPI was not, and there is no
  `requirements.txt`/`pyproject.toml` anywhere pinning either. For this small, fixed route set,
  FastAPI's main value-adds (auto request validation / OpenAPI docs) weren't worth a new
  dependency.
- **A single atomic request for a double-move**: avoids a cross-request "pending double-move"
  state machine entirely. The client (a human, unlike the detectives) decides both hops itself
  before submitting; nothing commits unless the whole thing validates.
- **No persistence / session TTL**: accepted non-goal — matches "refreshing the page discards
  the game" from the project's own UI design decisions.

