# Scotland Yard: Game Mechanics (Implementation Reference)

## Purpose

This document explains **how the system implements** game mechanics — the actual algorithms,
state transitions, and LLM orchestration behind them. It is a companion to, not a replacement
for, the following:

- `docs/rules/rules.md` — the official Scotland Yard rules. States *what* is allowed. Immutable
  source of truth (see `.cursorrules`).
- `docs/map/map.json` — the board graph. Immutable source of truth.
- `PROJECT_CONTEXT.md` — high-level project history, phase status, and architecture summary.

This file goes one level deeper than `PROJECT_CONTEXT.md`: it's where new contributors (human
or AI) should look to understand exactly how a given mechanic works before touching the code
that implements it.

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
- **Known Limitations** — what's intentionally not built yet, so it isn't mistaken for a bug.

When a new mechanic (Mr. X movement, ticket economy, capture/win detection, double-moves,
surfacing turns, etc.) is implemented, add a new `##` section here following the same template
and link it from the Table of Contents below.

## Table of Contents

1. [Detective Move Decision Cycle](#1-detective-move-decision-cycle)

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

**Phase 2 — Debate** (`debate_node`)
- Strictly sequential: Detective 1 speaks first, then Detective 2 (who sees D1's remark),
  through Detective 5 (who sees the full transcript so far). This is the one phase that cannot
  be parallelized, by design — each speaker is meant to react to what was already said.
- All 5 detectives speak every loop, including already-locked ones.

**Phase 3 — Vote** (`vote_node`)
- Every detective casts one ballot, voting on a target node for every *still-undecided*
  detective only (same dynamic-schema shrinking as the propose phase, via
  `build_ballot_schema`). Ballots are simultaneous/independent (no voter sees another's
  ballot), so the 5 calls run concurrently.
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

### Design Rationale

- **3-vote majority threshold**: matches "most of the team agrees" without requiring full
  unanimity, which would make consensus nearly impossible with 5 independently-motivated agents.
- **Server-side legality (MCP)**: the LLM (`openai/gpt-oss-20b`, a small model) cannot be
  trusted to reliably honor prompt-only constraints, so both ticket legality and node-occupancy
  are computed by `get_valid_moves` and re-validated in code after every LLM response, rather
  than relied upon as prompted behavior.
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
- **Prompt-level conflict hints + one retry, backed by deterministic code enforcement**: a
  small model (today's `openai/gpt-oss-20b`) can't be trusted to reliably self-enforce
  "propose distinct nodes," but a stronger production model shouldn't need to pay for the
  same heavy-handed correction every time either. Naming contested nodes up front and
  allowing one feedback-guided retry reduces how often the deterministic reconciliation pass
  has to intervene, but that pass — not the prompt or the retry — is what actually guarantees
  rules (a)-(d) hold, independent of which LLM is behind `get_detective_llm`.

### Known Limitations

- `build_next_round_state` does not apply `final_moves` to detective positions or deduct
  tickets from the spent transport type — that logic (a "resolve round" / "apply moves" step)
  has not been built yet.
- `vote_node` does not check whether two *different* detectives' tallies both reach majority
  for the same node in the same loop (each detective's tally is still computed independently).
  This is a cross-voter, cross-tally case — distinct from the within-one-ballot duplicate
  check described below — and is worth revisiting alongside a proper "apply moves" step.
