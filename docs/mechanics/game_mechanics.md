# Scotland Yard: Game Mechanics (Implementation Reference)

## Purpose

This document explains **how the system implements** game mechanics — the actual algorithms,
state transitions, and LLM orchestration behind them. It is a companion to, not a replacement
for, the following:

- `docs/rules/rules.md` — the official Scotland Yard rules. States *what* is allowed. Immutable
  source of truth (see `.cursorrules`).
- `data/board/map.json` — the board graph. Immutable source of truth.
- `CLAUDE.md` — high-level project history, phase status, and architecture summary.
- `docs/adr/` — Architecture Decision Records. Where this file explains *how* a mechanic works,
  an ADR explains *why that approach and not the alternatives*. Several sections below now link
  out to one instead of restating the argument inline.

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
- **State Involved** — which `ScotlandYardState` fields (`backend/scotland_yard/state.py`) it reads/writes.
- **Implementation References** — `file.py:function` pointers. Code is not duplicated here;
  if the doc and the code disagree, the code wins and this doc needs updating.
- **Design Rationale** — the *why* behind non-obvious choices.

Known bugs and limitations are not part of this template — see **`docs/issues/known_issues.md`**
for those instead.

When a new mechanic (Mr. X movement, ticket economy, capture/win detection, double-moves,
surfacing turns, etc.) is implemented, add a new `##` section here following the same template
and link it from the Table of Contents below.

## Table of Contents

1. [Detective Turn Cycle](#1-detective-turn-cycle)
2. [Round Resolution, Mr. X's Turn, and the API Layer](#2-round-resolution-mr-xs-turn-and-the-api-layer)

---

## 1. Detective Turn Cycle

### Overview

Once per round, each of the 5 detectives takes a turn, one at a time, in fixed `DETECTIVE_IDS`
order. A turn is three phases and six LLM calls: the detective whose turn it is (the *mover*)
proposes a destination for itself and broadcasts it; the other four respond to that proposal
one at a time; the mover then commits.

The move is then **applied immediately** — the detective physically moves, its ticket transfers
to Mr. X, and capture is checked — and the turn waits for the board to finish animating that
pawn before the next detective's first LLM call goes out. So a round plays out the way
`rules.md` §2 describes it: "Mr. X moving first, followed by Detectives 1 through 5 moving in
sequential order", each one deciding against "the current board state". See **ADR-0010**.

This replaced a simultaneous propose → debate → vote consensus loop in which all five
detectives decided at once and a 3-of-5 majority locked each move. See **ADR-0009** for why,
and **ADR-0006** (superseded) for what the old design was and what its parameters bought.

### Trigger

Runs once per round, orchestrated by the `detective_graph` LangGraph state machine
(`backend/scotland_yard/graph.py`). The graph is invoked fresh per round (it has no memory between separate
`.astream()`/`.ainvoke()` calls); chaining rounds is done externally via `build_next_round_state`
(see Round Boundaries below).

### Flow

```
START -> turn -> [router] -> turn (next detective) OR finalize -> END
```

The router advances `turn_index` through `DETECTIVE_IDS` until all five have moved — or stops
early if one of them caught Mr. X, since the game ends at that instant and the detectives behind
it never move. Each `turn` node run is one detective's whole turn:

**Phase 1 — Propose** (1 call, the mover)
- Before building any prompt, the node calls `game_master.compute_valid_moves` in-process
  (ADR-0001 — this used to be an MCP tool round-trip), passing `occupied_nodes` = every other
  detective's current position. This returns the true legal move set (ticket-legal and
  unoccupied), computed server-side rather than trusted from the LLM.
- That single exclusion is the whole collision guarantee, and it is **exact** rather than
  reconstructed: since each move is applied at the end of its own turn (ADR-0010), a detective
  that has already moved is genuinely standing on its new node. A later mover is therefore never
  offered a node someone is on — duplicate destinations are structurally impossible rather than
  something a vote tally rules out afterwards.
- Equally, the node an earlier detective **vacated** is simply available again, which is what
  `rules.md` §3's occupancy rule describes. This used to be forbidden: the old scheme reserved a
  mover's destination *and* left it standing on its origin, so both nodes stayed blocked for the
  rest of the round.
- The mover sees its own current node, its ticket inventory, and its legal destinations
  annotated with `distance_to_mrx_zone` (see "Mr. X Possible-Zone Context" below) and
  `onward_moves_after` (see "Onward-Move Annotation" below).
- The structured-output schema (`MoveChoice`) is fixed, not built per call: one `target_node`
  and a short `rationale`. Under the old design every call had to name a node for all five
  detectives, so the schemas were generated dynamically from the still-undecided set.
- If the named node is not in the mover's legal set, the *same* mover gets exactly one retry —
  a follow-up prompt naming the legal options. This is a best-effort quality improvement, not a
  correctness guarantee; see `_enforce_legal_node` below, which fixes whatever the retry doesn't.
- The proposal is then broadcast: every responder sees it, the mover's reasoning, and the full
  set of destinations the mover could have chosen from.

**Phase 2 — Respond** (4 calls, sequential, everyone else)
- Every non-mover answers exactly once, in cyclic `DETECTIVE_IDS` order starting from the
  mover's immediate successor. Agent Red's turn is answered by Blue, Green, Orange, Purple;
  Agent Green's by Orange, Purple, Red, Blue.
- Strictly sequential by design — each responder sees the responses already given this turn and
  is told not to repeat them. This phase cannot be parallelized; that is the point.
- Each responder sees the mover's options and proposal, **its own** legal destinations with the
  same two annotations, its own tickets, and the transcript so far. It returns a 2-3 sentence
  `response` plus a `preferred_node`: the node it would take on its own turn.
- **`preferred_node` is advisory.** It reserves nothing, and the responder is free to decide
  otherwise when its own turn arrives — by then the board has changed. If the named node is not
  one of that responder's legal moves, it is **dropped** from the transcript (logged
  `[VALIDATION]`) rather than reassigned. Rewriting it would manufacture an opinion the
  detective never held, and that invented node would then feed into the mover's decision prompt.
- Uses `llm_client.py:get_debate_llm()` — a dedicated `ChatOpenAI` instance with **no** tools
  bound, separate from the one the mover's own calls use via `get_detective_llm()`. This is a
  prose-first task with no tool-execution loop to handle a tool-call response, so it must never
  be able to emit one — see `docs/issues/known_issues.md` ISSUE-003 (Fixed).
- **A responder that has already taken its turn** is handled differently: it is told it has
  committed to Node N and cannot change it, and is not shown a menu of destinations or asked
  what it "would take on its own turn" — its turn is over, so that question would be
  straightforwardly false. It still gets a voice on where the mover goes, which is why it is
  asked at all. Its recorded `preferred_node` is its committed node, taken from
  `committed_moves` rather than from whatever the model echoed back. Legal moves and
  onward-move annotations are computed only for detectives that still have a move to make.
- **Per-turn transcript**: each turn's conversation starts fresh. The last responder's prompt is
  no larger than the first's, so per-round token cost stays flat across all five turns. What
  earlier turns decided is still visible — as committed moves on the board, which is the part
  that actually binds.

**Phase 3 — Decide** (1 call, the mover)
- The mover sees everything from its proposal prompt, plus its own proposal and all four
  responses, and commits. Its collaboration tendency (below) is what the prompt tells it to
  apply when weighing those responses against its own read.
- Same `MoveChoice` schema, same one-retry-then-deterministic enforcement.
- `agents.py:apply_detective_move` then makes the move real: the detective's `node_id` changes,
  the ticket it spends transfers to Mr. X (rules.md §3), and capture is decided the instant it
  lands. Legality is re-derived here rather than trusted from the turn that chose it — a target
  that is not actually reachable is a forfeit (no ticket spent, none transferred), logged at
  WARNING because it always means an upstream bug.
- **Capture ends the round here.** `state["captured_by"]` is set, the router skips straight to
  `finalize`, and the detectives behind this one never take a turn — the rules-correct outcome,
  and ~24 billable LLM calls not spent deliberating a decided game.

**The pawn-animation handshake** (ADR-0010)
- A turn does not return until the board has finished sliding that pawn to its new node, so the
  next detective never starts deliberating over a board that is still visibly rearranging.
- The board is in the browser, so the client says when: it POSTs `/games/{id}/turn-ack` on tween
  completion, and `session.await_pawn_settled` releases. The ack names `(round_number,
  detective)` and is ignored unless it matches what the loop is waiting on, so a late ack for an
  earlier turn cannot release the current one.
- **Always bounded** (`TURN_ACK_TIMEOUT_SECONDS`, 3s). Nobody may be watching — the round runs
  whether or not a client is listening (ISSUE-027) — the tab may be backgrounded with its tweens
  throttled, or the connection may have dropped. Timing out logs at INFO and continues; it can
  never stall a round.
- A detective that forfeited never animates, so the client acks it immediately rather than
  letting the backend wait out the full timeout.
- Mr. X's own move needs no ack: the client simply holds the round stream closed for the
  animation's duration, and the backend does not start the round until that stream opens.

**Deterministic enforcement** (`agents.py:_enforce_legal_node`)
- Applies to both of the mover's calls. Whatever the model named, the node actually recorded
  must be in the legal set this module computed itself.
- An illegal or missing answer is reassigned to the mover's **lowest-numbered free legal move**
  rather than leaving it stationary: a detective must move whenever a legal move is available.
  It stays put only when it genuinely has none.
- Under the old design a bad answer could still be caught downstream by the vote tally
  discarding it. There is no tally now, so this is the only thing between a hallucinated node
  and `committed_moves`.

**Per-call deadline** (`agents.py:_invoke`)
- Every call is wrapped in `asyncio.wait_for(LLM_CALL_DEADLINE_SECONDS)`. `llm_client.py`'s
  `timeout=45` is enforced by the HTTP client as an *idle-gap* timeout, reset by each streamed
  chunk — it kills a genuinely stuck call but not a merely slow one (`known_issues.md`
  ISSUE-009). That was tolerable while propose/vote fired five calls concurrently and a
  straggler overlapped its siblings; with all 30 of a round's calls sequential, one slow call
  adds its full duration to the round.
- A timeout or error is logged at WARNING and returns `None` rather than raising — every caller
  has a deterministic fallback, and a round must always produce a legal move for every detective.

**Mr. X Possible-Zone Context** (shared by all six calls — `agents.py:compute_mrx_zone_context`)
- Computed from `mr_x.last_known_node`/`last_known_round` and the board graph, then injected as
  prompt text into every proposal, response, and decision call. Gives detectives spatial
  grounding they otherwise have none of — see `docs/issues/known_issues.md` ISSUE-005.
- **Recomputed at the start of every turn**, not memoized per round. It used to be cached once
  per round, which was correct while detectives did not physically move until the round resolved
  — but under per-turn application (ADR-0010) the occupancy this BFS is blocked through changes
  five times a round, and a round-start snapshot would show a later detective paths blocked
  through nodes its teammates had already vacated. Ten graph walks per round instead of two is
  nothing beside 30 LLM calls; `state["mrx_zone_context"]` and its memo wrapper are gone.
- **The zone** (**ADR-0013**): one ticket-typed layer per hop Mr. X has logged since the
  reveal, not an untyped ball — `S₀ = {last_known_node}`, then each successive layer keeps only
  edges whose type matches the ticket that hop actually spent (`"black"` widens to every type,
  which is exactly what a black ticket buys). Blocked through currently-occupied detective nodes
  per rules.md's "Mr. X cannot move to, or pass through, a Node occupied by a Detective". On a
  surfacing round the sequence is empty and the zone is the single node he is standing on, since
  detectives move after he does.
- **No hop cap.** The old `min(…, 4)` cap existed because the untyped ball saturated the board
  (88 nodes at 4 hops); the typed walk averages 35 at the same depth. A cap cannot be applied to
  it anyway — "the last four tickets" means nothing without knowing which set they were spent
  from. The cap survives only on the fallback ball.
- **Fallback, and why it is one-directional**: this set is used to rule locations *out*, so
  wider than the truth only makes detectives cautious while narrower makes the game unfair.
  Anything that cannot be reconciled — a malformed log, a round count that disagrees, a walk
  that dead-ends — falls back to the untyped, hop-capped ball and logs `[ZONE FALLBACK]` at
  WARNING rather than narrowing on a guess.
- Shown as a literal node list when ≤20 nodes; above that, only the count. Narrowing moved the
  2-hop case (max 19 nodes) under that threshold, so the prompt now shows real candidate nodes
  where it used to show an unusable count — the step change, more than the smaller number
  itself. `None` (handled explicitly, with a "hasn't surfaced yet" message) during rounds 1-2.
- The prompt also states the ticket sequence the narrowing is *based on*, so the model can see
  why four nodes rather than thirteen — a number to trust rather than argue with.
- **The distance signal**: one multi-source BFS from the whole zone gives every board node's
  hop-distance to the nearest zone node. Rather than a full candidate-move × zone-node matrix
  (which would run to thousands of numbers at wide hop counts), each candidate move and each
  detective's own current position gets a single `distance_to_mrx_zone` integer — 0 means that
  node IS a possible current location.
- Every prompt also carries a "use only the data given to you in this prompt, do not seek
  outside information" instruction (`get_psychology_prompt`), added alongside this context.

**Onward-Move Annotation** (`agents.py:annotate_onward_options`)
- Each candidate destination also carries `onward_moves_after`: how many distinct nodes that
  detective would still be able to reach **next** round from there, with the ticket this move
  costs already deducted. A `0` is a dead end.
- Which ticket a move spends is resolved by `transport.determine_move_transport` — the same
  helper `resolve_round` uses to actually deduct it — so this can never disagree with what the
  move will really cost.
- Occupancy for the onward calculation is simply where the other four are standing right now
  (`other_detective_nodes`) — exact for anyone who has already moved this round, and a snapshot
  for anyone who has not, since they will move again before this lookahead comes true.
- Computed deterministically rather than left to the model, for the same reason
  `distance_to_mrx_zone` is: it is a cheap board lookup, and asking a small model to simulate
  ticket arithmetic is exactly the kind of thing it gets quietly wrong. Costs one integer per
  candidate.

**Containment Annotation** (`agents.py:annotate_zone_shrink`) — **ADR-0013**
- Each candidate also carries `zone_size_after`: how many nodes Mr. X could still reach next
  round *if this detective stands there*, computed by re-projecting the zone one untyped hop
  with that destination blocked. Lower is better — it means standing on an escape route rather
  than merely near him.
- It exists because hop-distance alone cannot express containment. Five detectives each
  minimising their own `distance_to_mrx_zone` all converge on whichever side of the zone faces
  them, and Mr. X leaves by the other — the convergence-not-triangulation trap recorded in
  `known_issues.md` ISSUE-016. The shrink is directly measurable, so it is measured rather than
  described to the model in words it would have to apply geometrically.
- Untyped by necessity: his *next* ticket has not been played yet, so there is nothing to filter
  the edges by (unlike the zone walk itself).
- **Dropped entirely when every candidate scores the same** — the common case for a detective
  several hops out, where nothing it can reach this round touches his space. A column of
  identical numbers is prompt weight carrying no decision.

**Revisit Annotation** (`agents.py:annotate_revisits`, `state.py:recent_positions`)
- Flags candidates the detective vacated within the last two rounds, and reports whether any
  exist so `format_options_block` can omit the whole warning when nothing can trigger it.
- Detectives are otherwise **completely stateless across rounds**: `messages` is written once per
  turn but never read back into a prompt, and everything else resets at the round boundary. So
  stepping back onto the node just left looked exactly as attractive as it did the first time,
  which is how two nodes become a shuttle. `recent_positions` is the only per-detective memory
  that survives `build_next_round_state`, for precisely that reason.
- Only recorded when the detective actually moved: a forfeited turn leaves it standing where it
  was, and "recently vacated" would then flag the node it is still on.

**Candidate Ordering** (`agents.py:sort_candidates`)
- Options are rendered best-first — `(distance asc, zone_size_after asc, onward desc, node id)`.
  The list previously came out in `map.json`'s own connection order, which carries no meaning.
  Free in both tokens and latency, and small models weight what they read first. The node-id tie
  break exists purely so the ordering is reproducible.

**Intel-Freshness Ladder** (`agents.py:get_surfacing_proximity_prompt`, injected by `get_psychology_prompt`)
- How much Mr. X's location is worth knowing swings on a fixed cycle, so the emphasis swings
  with it. Four rungs, keyed purely on `round_number`:

  | Rounds | Rung | Emphasis |
  |---|---|---|
  | 1, 6, 11, 16, 22 | 2 before a reveal | weigh `onward_moves_after` a little more |
  | 2, 7, 12, 17, 23 | 1 before a reveal | weigh `onward_moves_after` strongly — today's zone estimate is about to be replaced |
  | 3, 8, 13, 18, 24 | **the reveal itself** | **converge** — the zone is a single node and will never be tighter |
  | 4, 9, 14, 19 | 1 after a reveal | **keep closing** while the trail is warm |
  | 5, 10, 15, 20, 21 | — | balanced default, no extra paragraph |

- Recency is checked before proximity: acting on a position already known outranks preparing for
  one that is coming. They cannot currently both match (reveals are ≥5 rounds apart and only two
  rungs reach each way), so the ordering is a statement of intent rather than a live tiebreak.
- The "converge" rungs exist because detectives were observed drifting away from Mr. X in exactly
  the rounds his position was best known — the window where the zone is tightest is also the only
  one in which closing distance can actually corner him, and every round of hesitation widens it
  again.
- Deliberately **no new annotation** — `onward_moves_after` already measures the thing this is
  after (how many next-round moves a candidate destination would leave, with *this* detective's
  actual remaining tickets deducted). A raw board-connectivity count (nodes/transport types)
  would be a cruder, ticket-blind version of the same signal, and `known_issues.md` ISSUE-016
  already rejected a similarly-flavored addition on the small model's already-documented
  reasoning-budget fragility (ISSUE-006/007). Reweighting existing data was chosen over adding
  more of it.
- Gated **purely on `round_number`**, shared verbatim by every one of a turn's six calls (same
  mechanism as the collaboration-tendency paragraph below) — so it shapes the mover's proposal
  and final decision *and* every other detective's advisory own-move preference identically,
  with no per-detective distance check.

**Collaboration tendency** (`agents.py:get_collaboration_tier`, `get_psychology_prompt`)
- `get_psychology_prompt` keeps its three motivations unchanged (1. Team Win, 2. Selfish Glory,
  3. Efficiency). The ladder underneath them used to be a 3-step *desperation* scale whose job
  was to make the vote converge before the loop cap. There is no vote and no cap now, so it
  expresses **collaboration tendency** instead: how much weight a detective gives a teammate's
  argument relative to its own Selfish Glory goal.
- Five tiers, from `rules_constants.COLLABORATION_TIERS`, with the literal percentage stated in
  the prompt alongside the label:

  | Rounds | Tendency | Label |
  |---|---|---|
  | ≤ 4 | 1% | MINIMUM |
  | 5–8 | 25% | LOW |
  | 9–12 | 50% | MEDIUM |
  | 13–16 | 75% | HIGH |
  | ≥ 17 | 99% | MAXIMUM |

- It is a **prompt tier, not a random draw**. An LLM cannot act on a probability, only on an
  instruction; a per-call RNG would make rounds irreproducible for no behavioural gain. The
  number is stated because "25%" is a sharper instruction than "LOW" alone.
- `rules_constants.py` asserts at import that the ladder is contiguous and covers every round up
  to `MAX_ROUND`, so retuning a boundary fails loudly rather than silently dropping a round off
  the table.

**Finalize** (`finalize_round_node`)
- Assembles `final_moves` and `final_move_details` from `turn_records`. There is no fallback
  resolution and no move application left to do: every turn ends in a move that already happened.
  Both values are read straight out of the records rather than recomputed, because the detectives
  have already left the nodes they started from — `from_node` has to be captured while it is
  still true. A round cut short by a capture finalizes with only the turns that actually
  happened; otherwise a missing record raises rather than being papered over.
- *Asserts* destination uniqueness (excluding detectives that stayed put, which cannot collide).
  Unreachable given the reservation above, which is exactly why it is asserted — a duplicate
  arriving here means that exclusion has regressed, and it must fail loudly rather than degrade
  into a silently forfeited turn.
- Also computes `final_move_details` — per detective, `{from_node, to_node, transport}` — via
  `backend/scotland_yard/transport.py:determine_move_transport`, the same legality/tie-break helper §2's
  `resolve_round` itself calls to actually apply moves. This exists purely so the frontend Chat
  Log's "Final Moves" line can show what's about to happen (e.g. "Agent Red moves from Node 13 to
  Node 46 via Metro") the moment this node's own SSE event fires — `resolve_round` isn't called
  until the whole `detective_graph` run finishes (see §2). Using the identical helper on both ends
  guarantees the preview can never disagree with what `resolve_round` later actually deducts.
  `transport` is `None` only if `from_node == to_node` (the rare "stayed put" case above).

### Streaming Granularity

`turn_node` emits a custom stream event via `get_stream_writer()` **per LLM call**, not per
graph node: `turn_started`, `turn_proposal`, one `turn_response` per responder, and
`turn_decision`. `server.py` relays these verbatim as named SSE events.

This matters as much as the turn structure itself. A node-level `updates` chunk only arrives
once a whole six-call turn has finished, which is exactly the all-at-once delivery turn-wise
play exists to get rid of — so `serializers.serialize_loop_event` swallows the `turn` update
rather than re-sending content already streamed, and translates only `finalize`.

### Round Boundaries

`committed_moves`, `turn_records`, `captured_by` and `turn_index` are intentionally cumulative
*within* a round (that's how the turn sequence tracks who has already moved). They do **not**
reset themselves — `detective_graph` has no cross-invocation memory, so resetting is the caller's
responsibility. `build_next_round_state()` (`backend/scotland_yard/graph.py`) takes a completed round's final
state and returns a fresh `initial_state` for the next round: `round_number` incremented,
`turn_index: 0`, `committed_moves: {}`, `turn_records: {}`, `captured_by: None`,
`final_moves: {}`, `final_move_details: {}`, `messages: []`, while carrying `detectives` and
`mr_x` forward unchanged. Left unreset, `turn_index` would already be at 5 and the next round
would route straight to `finalize` without a single detective taking a turn.

Positions and ticket inventories need no handling here: each move was applied when its own turn
ended, so `detectives` and `mr_x` are already current.

### State Involved

| Field | Type | Role |
|---|---|---|
| `round_number` | `int` | Drives the collaboration-tendency ladder (see above) |
| `turn_index` | `int` | Index into `DETECTIVE_IDS` of the detective currently taking its turn; the router finalizes once it reaches 5 |
| `committed_moves` | `Dict[str, int]` | Detective → node, accumulates within a round; a record of who has already moved, not a set of reservations |
| `captured_by` | `Optional[str]` | The detective that landed on Mr. X, set the instant it happens; the router reads it to skip every remaining turn |
| `recent_positions` | `Dict[str, List[int]]` | The last two nodes each detective vacated. The ONLY per-detective memory that survives a round boundary (`build_next_round_state` carries it forward) — without it a detective cannot tell it is shuttling. See `annotate_revisits` |
| `turn_records` | `Dict[str, TurnRecord]` | Per turn: the proposal, the four responses, the origin node, the ticket spent, and the final decision |
| `final_moves` | `Dict[str, int]` | Output of `finalize_round_node`; the round's resolved moves |
| `final_move_details` | `Dict[str, dict]` | `finalize_round_node`'s from/to/transport preview per detective, for the frontend Chat Log - see Finalize above |
| `messages` | `List[BaseMessage]` | One `AIMessage` per completed turn, holding that turn's rendered transcript |
| `detectives` | `Dict[str, Detective]` | Current positions/tickets; **written** by each turn as its move is applied (ADR-0010), not frozen for the round |
| `mr_x` | `MrXState` | Also written each turn: a detective's spent ticket transfers into his inventory |

### Implementation References

- `backend/scotland_yard/rules_constants.py:DETECTIVE_IDS` — the 5 detectives' internal identifiers (`agent_red`,
  `agent_blue`, `agent_green`, `agent_orange`, `agent_purple`), and the fixed turn order;
  `AGENT_DISPLAY_NAMES`/`agent_names` map them to their human-readable callsigns ("Agent Red",
  etc.) used anywhere a detective's identity appears in LLM-facing prompt text or the turn
  transcript, so the agents' own reasoning refers to itself/peers by callsign rather than the
  internal id. Dict keys and console logs still use the raw id.
- `backend/scotland_yard/rules_constants.py:COLLABORATION_TIERS`, `CALLS_PER_TURN`, `LLM_CALL_DEADLINE_SECONDS`
  — this project's own turn parameters (not from rules.md); see ADR-0009
- `backend/scotland_yard/agents.py:turn_node` — the whole turn: propose, four responses, decide,
  apply, then wait for the pawn to finish animating
- `backend/scotland_yard/agents.py:apply_detective_move` — the move itself: position, ticket
  transfer, capture check, all re-derived rather than trusted
- `backend/scotland_yard/session.py:expect_pawn_ack`, `acknowledge_pawn_settled`,
  `await_pawn_settled` — the bounded pawn-animation handshake
- `backend/scotland_yard/server.py:turn_ack_route` — `POST /games/{id}/turn-ack`, which
  deliberately does not take `session.lock` (the round holds it)
- `backend/scotland_yard/agents.py:responders_for` — the cyclic response order for a given mover
- `backend/scotland_yard/agents.py:get_psychology_prompt`, `get_collaboration_tier` — the 3 motivations and the
  round-based collaboration ladder
- `backend/scotland_yard/agents.py:get_surfacing_proximity_prompt`, `SURFACING_PROXIMITY_GUIDANCE`,
  `SURFACING_RECENCY_GUIDANCE`, `HOW_TO_READ_YOUR_OPTIONS` — the intel-freshness ladder, and the
  annotation legend every one of a turn's six calls now shares (it used to appear only in the
  mover's proposal, leaving the four responders and the mover's own final commit with no stated
  objective at all)
- `backend/scotland_yard/travel_log.py:hops_since_surfacing`, `bucket_hops_by_round` — re-deriving
  which logged hops belong to which round, including the surfacing-round double-move case where
  the reveal is the intermediate node (**ADR-0013**)
- `backend/scotland_yard/game_master.py:compute_mrx_zone_from_tickets`, `project_zone_one_hop` —
  the ticket-typed zone walk and the one-hop containment projection
- `backend/scotland_yard/agents.py:annotate_zone_shrink`, `annotate_revisits`, `sort_candidates`
- `backend/scotland_yard/agents.py:MoveChoice`, `TurnResponseChoice` — the two fixed structured-output schemas
- `backend/scotland_yard/agents.py:_invoke`, `_choose_move`, `_enforce_legal_node` — the per-call deadline, the
  one-retry wrapper, and the deterministic enforcement that backs it
- `backend/scotland_yard/agents.py:format_board_block`, `format_options_block` — the prompt blocks every call
  in a turn shares, so all six demonstrably reason about the same board
- `backend/scotland_yard/graph.py:build_detective_graph` (and its `check_turn_status` router), `finalize_round_node`, `build_next_round_state`
- `backend/scotland_yard/game_master.py:compute_valid_moves` — ticket + occupancy legality, server-side
- `backend/scotland_yard/agents.py:fetch_legal_moves` — the single legal-move lookup, and the
  occupancy exclusion that makes duplicate destinations structurally impossible
- `backend/scotland_yard/llm_client.py:get_detective_llm` — cached LLM for the mover's proposal and decision
- `backend/scotland_yard/llm_client.py:get_debate_llm`, `_build_chat_llm` — the responders' cached, non-tool-
  bound LLM instance (see ISSUE-003) and the shared OpenRouter config helper both LLM getters
  build on
- `backend/scotland_yard/game_master.py:compute_mrx_zone`, `compute_distances_to_zone` — the board-topology
  BFS behind the Mr. X Possible-Zone Context described above
- `backend/scotland_yard/agents.py:compute_mrx_zone_context`, `format_mrx_zone_block` — builds and renders that
  context into every prompt
- `backend/scotland_yard/agents.py:annotate_onward_options`, `other_detective_nodes` — the
  anti-stranding annotation and the live occupancy it is computed against
- `backend/scotland_yard/transport.py:pick_transport`, `determine_move_transport` — the transport
  legality/tie-break logic shared by `finalize_round_node`'s Chat-Log preview (here),
  `annotate_onward_options`, and §2's `resolve_round` (which actually applies the move) -
  extracted from `round_resolver.py` into its own module specifically so all callers share one
  implementation

### Design Rationale

- **Turns instead of simultaneous decisions** (ADR-0009): the old design produced good moves but
  was unreadable — fifteen messages arriving at once, up to three times a round, with no natural
  narrative order for the frontend to impose. A turn has an obvious protagonist, so the Chat Log
  can group a round into five legible turns. The cost is latency: nothing in a turn can be
  parallelized, so the critical path goes from 7 sequential call-slots per loop to a flat 30 per
  round. Token cost is roughly unchanged (~31k input tokens/round against 25k–76k before),
  because each call now carries one or two detectives' option lists instead of all five.
- **Sequential movement replaces the majority threshold as the collision guarantee**: the old
  3-of-5 threshold was load-bearing for *correctness*, not just legitimacy — a strict majority
  is what made two detectives' tallies both reaching the threshold on one node arithmetically
  impossible (`known_issues.md` ISSUE-010). Moving each detective as its turn ends achieves the
  same guarantee more directly and with one code path instead of several, which also retires
  ISSUE-025's shared-helper fix and ISSUE-026's fallback de-duplication.
- **The board paces the round, not the other way round** (ADR-0010): a turn ends by waiting for
  its own pawn to arrive. That costs ~6s per round on top of the LLM time and is the price of a
  round being watchable — five pawns jumping at once when the round resolved threw away, on the
  board, exactly the legibility turn-wise play bought in the Chat Log. The wait is bounded in
  every direction so it can never become a way for a round to hang.
- **Fixed turn order, not rotating**: Agent Red always picks from a free board and Agent Purple
  always picks against four reserved nodes, so there is a real first-mover advantage. Accepted
  deliberately — a fixed order is something the player learns once and can then follow without
  effort, and that legibility is the entire point of the change. Revisit if Purple's
  disadvantage shows up as a measurable handicap.
- **Responses are advisory, not binding**: reserving a responder's stated node immediately would
  make turn order overwhelmingly decisive and reduce that detective's own turn to a formality.
  Advisory intent still informs the mover without foreclosing anything.
- **Collaboration tendency as a prompt tier, not an RNG**: an LLM cannot act on a probability,
  only on an instruction. Stating the percentage keeps the tier sharp while leaving rounds
  reproducible and testable.
- **Fixed schemas instead of dynamic per-loop ones**: each call decides exactly one node, so
  there is nothing to generate. Smaller structured responses also mean far less room for the
  reasoning-budget truncation documented in `known_issues.md` ISSUE-006/007.
- **A real wall-clock deadline on every call**: `timeout=45` is an idle-gap timeout, not a
  deadline (ISSUE-009). Concurrency used to mask that; a 30-call sequential chain does not.
- **Server-side legality (in-process; see ADR-0001)**: the LLM cannot be trusted to reliably honor prompt-only
  constraints, so both ticket legality and node-occupancy are computed by `compute_valid_moves` and
  re-validated in code after every LLM response, rather than relied upon as prompted behavior.
  (Current model: `deepseek/deepseek-v4-flash-0731` via OpenRouter — see `CLAUDE.md`.)
- **Reasoning disabled on every detective-facing LLM instance** (full evidence in ADR-0004):
  `llm_client.py:_build_chat_llm` sets `max_tokens=4000` and
  `extra_body={"reasoning": {"enabled": False}}` (OpenRouter's reasoning extension — not part of
  the standard OpenAI schema, hence `extra_body` rather than a typed field), shared by both
  `get_detective_llm()` and `get_debate_llm()`. Every attempt to *bound* the reasoning budget
  was ignored by this route; only disabling it outright worked. See `known_issues.md`
  ISSUE-006/007 for the four-configuration evidence.
- **Responders get their own non-tool-bound LLM instance**: binding tools to an LLM that has no
  tool-execution loop behind it just gives the model an option it shouldn't have — it can return
  a tool call instead of prose, and that response's empty `.content` lands in the transcript as
  a blank pitch. See `known_issues.md` ISSUE-003.
- **An illegal advisory preference is dropped, not reassigned**: unlike a mover's own choice,
  which must resolve to *something* legal, a responder's stated intent has no such obligation.
  Rewriting it to the lowest-numbered legal node would invent an opinion that then feeds into
  the mover's decision prompt. The old vote tally took the same line with a bad ballot entry.
- **One scalar per move, not a full distance matrix, for the Mr. X zone context**: an earlier
  design pass considered giving every candidate move its distance to *every* node Mr. X could
  possibly be at, but a real check against `data/board/map.json` showed the zone can cover up to
  ~84% of the board at 4 hops — a full matrix would run to thousands of numbers per call in the
  worst case, adding real cost and complexity right on top of already-logged latency/reliability
  issues (`known_issues.md` ISSUE-006/007/009). A single "distance to the *nearest* possible
  node" per candidate keeps the added cost to a measured ~304 tokens per call while still
  answering the actual decision-relevant question ("does this move get me closer"). The
  tradeoff, accepted deliberately: this scalar alone can't support true multi-detective
  triangulation (knowing several detectives' distances to *some* plausible node doesn't reveal
  whether they're all closing in on the same spot or covering different ones) — it's a
  "getting warmer/colder" signal per detective, not a coordinated-encirclement one.
- **Per-call streaming, not per-node**: the turn structure only pays off if the client sees each
  message as it lands. A node-level update would deliver all six of a turn's calls at once,
  reproducing the exact problem this change exists to solve.

---

## 2. Round Resolution, Mr. X's Turn, and the API Layer

### Overview

Everything §1 leaves unfinished: judging the win conditions that can only be judged once a whole
round is over, handling Mr. X's own (human-played) turn including double-moves and surfacing
reveals, and a live server a browser frontend can actually talk to.

Note what is **not** here any more: detective moves and their ticket transfers are applied inside
§1, at the end of each detective's own turn (ADR-0010). `resolve_round` used to do that work and
no longer moves anyone.

### Trigger

One full round = Mr. X's human-submitted turn (`backend/scotland_yard/mrx_turn.py`), then the detective
decision cycle (§1, `detective_graph` — five detective turns), then round resolution (`backend/scotland_yard/round_resolver.py`).
Orchestrated per-game by a `GameSession` (`backend/scotland_yard/session.py`) and driven externally via the
Starlette API (`backend/scotland_yard/server.py`).

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
- `run_detective_loop` drives `detective_graph.astream(state, stream_mode=["updates", "values",
  "custom"])`. `"custom"` is the main channel: those are `agents.py`'s own `get_stream_writer()`
  calls, **one per LLM call**, so the client sees a proposal, each response, and the final
  decision land individually as they happen. `"updates"` chunks only arrive once a whole
  six-call turn has finished - too coarse to drive the Chat Log, so they are used for the
  `finalize` node and as a turn-boundary marker. The last `"values"` chunk becomes the new
  `session.state` directly, reusing `state.py`'s own reducers rather than reimplementing them.
- Moves are applied by §1's `agents.py:apply_detective_move`, once per turn, never trusting the
  LLM's choice for legality (re-derived via `game_master.compute_valid_moves` — the same posture
  §1 applies to every LLM response). When a target node is reachable via more than one transport
  type (verified real case: map.json's node 1 ↔ node 46 via both bus and metro),
  `transport.py:determine_move_transport` (which wraps `pick_transport`) deterministically
  prefers whichever type the detective holds the most tickets of, tie-broken taxi > bus > metro —
  a server-side apply-time decision, not something detectives ever choose themselves (see Design
  Rationale). `finalize_round_node` reads the resulting ticket back out of `turn_records` for the
  Chat Log's round recap, so the two can never disagree.
- **Capture is decided inside the turn that causes it**, not here — the instant a detective's new
  node equals Mr. X's real `current_node`, `captured_by` is set and the graph's router skips
  every remaining turn, so the detectives behind it never move *or* deliberate. `resolve_round`
  only reads that verdict, threading it onto `session.winning_detective` (`_game_over`'s own
  `captured_by` argument) so the frontend can credit a specific detective ("Caught by Agent
  Red") rather than only knowing "the detectives won". Stays `None` for the OTHER "detectives
  win" sub-condition below (Mr. X out of legal moves — nobody to credit) and for either "Mr. X
  wins" ending, where it's simply not applicable.
- If no capture: checks whether Mr. X now has any legal move at all (detectives win if not),
  then whether all 5 detectives are simultaneously trapped (Mr. X wins if so), then whether
  `round_number == 24` was just completed (Mr. X wins). Otherwise calls `build_next_round_state`
  (§1) unchanged and sets `status = "awaiting_mr_x_move"` for the next round.

**API layer** (`backend/scotland_yard/server.py`, `backend/scotland_yard/serializers.py`)
- `POST /games`, `GET /games/{id}`, `GET /games/{id}/mrx/legal-moves`,
  `POST /games/{id}/mrx/move`, `GET /games/{id}/round/stream` (SSE via `sse_starlette`, chosen
  over WebSocket since this is one-directional server→client data once opened), plus `/health`
  for platform liveness checks.
- **Every game-scoped route is ownership-checked** (**ADR-0014**): `POST /games` mints an opaque
  token, returns it in an `HttpOnly` cookie and records it on the session, and the rest require a
  `hmac.compare_digest` match. An unknown id 404s *before* that check, so a dead link reads as
  "no such game" rather than hinting one exists. `limits.py` additionally refuses new games past
  a concurrency/per-IP cap (429) and refuses to *start* a round past the daily LLM-call budget
  (503) - before the stream opens, never mid-round, since a refusal halfway would strand a game
  with some detectives moved.
- In a deployment this same app also serves the built SPA, so there is one origin; in dev Vite
  serves it separately and the client sends credentials explicitly so both paths behave alike.
- `serializers.serialize_public_state` is the **single choke point** every route and streamed
  event goes through to build an outward-facing payload. `mr_x.current_node` (his real position)
  IS included as of the board's always-visible Mr. X pawn feature — safe because the only
  human-facing client is the one played BY Mr. X; the detectives are backend-only LangGraph/LLM
  agents with no access to this or any client. If a detective-facing client is ever added,
  `current_node` must be excluded from whatever serialization *that* client receives.
- `POST /games/{game_id}/turn-ack` is the one client→server message in the round: the board
  reporting that a detective's pawn finished animating, which releases the next detective's turn
  (ADR-0010). It deliberately does **not** take `session.lock`, since the round holds that lock
  for its whole duration and this request exists to unblock it from the inside.
- Most SSE events are `agents.py`'s own per-call payloads, relayed verbatim by
  `round_stream_route` with the payload's own `"event"` name as the SSE event name:
  `turn_started`, `turn_proposal`, `turn_response`, `turn_decision`.
  `serializers.serialize_loop_event` handles only the node-level `"updates"` chunks — it
  swallows the `turn` update (its content has already been streamed, call by call) and
  translates `finalize` into `round_finalized`, whose `final_moves` field carries
  `finalize_round_node`'s `final_move_details` (from/to/transport per detective), not the raw
  `final_moves` int map, since that's what the frontend Chat Log actually renders. The API layer
  itself appends a final `round_result` event once `resolve_round` has run (see Flow above).

### State Involved

| Field | Role in this mechanic |
|---|---|
| `mr_x.current_node` | Mr. X's real position — added in this mechanic; never read by any detective-facing code path (§1). Now serialized by `serialize_public_state` for the always-visible Mr. X pawn (see §2's own note above) - still never exposed to anything detective-facing. |
| `mr_x.last_known_node` / `last_known_round` | Set only by `mrx_turn` on a surfacing-round move |
| `mr_x.transport_history` | Appended to by `mrx_turn` on every hop (records the ticket type spent, not necessarily the underlying route type — a black ticket is logged as `"black"`, matching the rules' obfuscation intent). A double-move additionally inserts a `"double"` sentinel immediately before its own two hop entries, matching rules.md's stated broadcast order — never a valid `ticket_type_spent` itself, only ever inserted by `submit_mr_x_move`'s double-move branch. |
| `detectives[*].node_id`, ticket counts | Mutated by `resolve_round`, never by the graph itself |
| `final_moves` | Read (never written) by `resolve_round`; still produced exactly as §1 describes |
| `session.winning_detective` | Set by `resolve_round`'s `_game_over` from `state["captured_by"]`; `None` unless a detective actually landed on Mr. X. Serialized by `serialize_public_state` for the frontend's game-over banner. |

### Implementation References

- `backend/scotland_yard/state.py` — `MrXState.current_node` (additive field)
- `backend/scotland_yard/game_master.py:compute_valid_moves` — pure function extracted from the `get_valid_moves`
  MCP tool so server-side code can call it in-process, without the MCP stdio subprocess round-trip
  that only LLM tool-calling actually needs
- `backend/scotland_yard/session.py:GameSession`, `create_game`
- `backend/scotland_yard/mrx_turn.py:get_mr_x_legal_moves`, `submit_mr_x_move`
- `backend/scotland_yard/round_resolver.py:run_detective_loop`, `resolve_round`
- `backend/scotland_yard/transport.py:pick_transport`, `determine_move_transport` — see §1's own reference to
  the same module
- `backend/scotland_yard/serializers.py:serialize_public_state`, `serialize_loop_event`
- `backend/scotland_yard/server.py` — the Starlette app and its routes
- `backend/tests/test_round_resolution.py`, `backend/tests/test_api.py` — verification (see Known
  Limitations for what these do *not* cover)

### Design Rationale

- **Transport-type ambiguity resolved at apply-time, not in the LLM schema**: extending §1's
  `build_strategy_schema`/`build_ballot_schema` to also require a transport-type field would mean
  rewriting the just-hardened duplicate/legality/contested-node logic for negligible strategic
  value — a detective's target-node choice is what its psychology prompt reasons about, not the
  specific ticket spent to get there. `pick_transport`'s "most remaining tickets, tie-broken
  taxi > bus > metro" rule is a reasonable default that conserves the scarce metro allotment; it
  can be revisited if ticket-economy strategy ever becomes a design priority.
- **`pick_transport`/`determine_move_transport` live in their own `transport.py` module, not
  inline in `round_resolver.py`**: once §1's `finalize_round_node` also needed this exact
  decision (to preview it for the Chat Log one step before `resolve_round` actually applies it),
  keeping two independently-maintained copies would risk them silently drifting apart - a shared
  module guarantees the preview and the real, ticket-deducting application always agree.
- **Starlette, not FastAPI**: see [ADR-0002](../adr/0002-starlette-over-fastapi.md). Note that
  the original "nothing pins dependencies anyway" part of that reasoning no longer applies —
  `backend/pyproject.toml` now pins everything exactly — but the decision stands on the route
  count. Request validation is explicit, in `backend/scotland_yard/requests.py`.
- **A single atomic request for a double-move**: avoids a cross-request "pending double-move"
  state machine entirely. The client (a human, unlike the detectives) decides both hops itself
  before submitting; nothing commits unless the whole thing validates.
- **No persistence**: accepted non-goal — matches "refreshing the page discards the game".
  Sessions ARE now TTL-evicted, though; see
  [ADR-0005](../adr/0005-in-memory-session-store.md) for why those are two separate decisions
  and only the first was deliberate.

