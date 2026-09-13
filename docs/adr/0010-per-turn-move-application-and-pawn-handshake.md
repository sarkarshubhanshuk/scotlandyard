# ADR-0010 — Moves apply per turn, and the board paces the round

- **Status:** Accepted — refines [ADR-0009](0009-turn-wise-detective-play.md)
- **Area:** `backend/scotland_yard/agents.py`, `graph.py`, `round_resolver.py`, `state.py`,
  `session.py`, `server.py`, `requests.py`, `rules_constants.py`,
  `frontend/src/board/BoardScene.ts`, `hooks/useRoundStream.ts`

## Context

ADR-0009 made detectives take turns, but a turn only *committed* a destination — all five moves
were applied together at the end of the round by `resolve_round`. Two things followed from that,
both wrong:

**The board lied about the order.** Five pawns jumped to their new nodes at once when the round
resolved, so a round that was deliberated one detective at a time was still *shown* as a
simultaneous scramble. The legibility ADR-0009 bought in the Chat Log was thrown away on the
board.

**It was also a rules deviation.** `rules.md` §2 says a round is "Mr. X moving first, followed by
Detectives 1 through 5 moving in sequential order", and that "at the start of each turn, the
active AI agent receives the current board state". Committing-without-moving needed a
`reserved_nodes` set to stop two detectives choosing one node — and that set blocked each
mover's **origin** for the rest of the round as well as its destination. A node Agent Red had
walked away from stayed unusable by everyone until the round ended, a constraint the rules never
impose.

## Decision

**Apply each detective's move at the end of its own turn, and hold the next turn until the board
has finished animating it.**

### Per-turn application

`agents.py:apply_detective_move` moves the detective, transfers its spent ticket to Mr. X, and
checks capture, immediately — before the next detective is asked anything. So:

- `fetch_legal_moves` loses its `reserved_nodes` argument. Occupancy is just "where the other
  four are standing", which is now exact rather than a reconstruction. A vacated node is
  available to whoever moves next, as the rules describe.
- **The Mr. X possible-zone BFS is recomputed per turn** instead of memoized per round. Its
  occupancy input genuinely changes five times a round now, so a round-start snapshot would show
  later detectives paths blocked through nodes their teammates had already left. Five BFS pairs
  per round instead of one is nothing beside 30 LLM calls; `state["mrx_zone_context"]` is gone.
- **Capture ends the round where it happens.** `turn_node` records `captured_by`, the graph's
  router skips straight to finalize, and the detectives behind the captor never take a turn —
  which is both the rules-correct outcome and ~24 billable LLM calls not spent on a decided game.
- `resolve_round` no longer moves anyone. It reads the capture verdict and evaluates the three
  win conditions that can only be judged once a whole round is over (Mr. X trapped, all
  detectives trapped, round 24).
- `finalize_round_node` summarises the round from `turn_records` rather than recomputing it —
  by then the detectives have left the nodes they started from, so `from_node` has to be
  recorded when it is still true.

### The pawn-animation handshake

Every pawn — Mr. X's and all five detectives' — now animates with the same 1000 ms
`Sine.easeInOut` tween. Detective pawns used to be destroyed and recreated on every render,
which is why they teleported; they are reused and tweened like Mr. X's already was.

The next detective's **first LLM call** waits for the previous pawn to arrive:

- **Detective → detective:** the client POSTs `/games/{id}/turn-ack` when the tween completes;
  `turn_node` awaits it before returning. The ack names `(round_number, detective)` and is
  ignored unless it matches what the loop is actually waiting on, so a late ack for an earlier
  turn cannot release the current one.
- **Mr. X → Agent Red:** no ack. The client simply holds the round stream closed for the
  animation's duration. His tween and that timer share one trigger and one clock, and the
  backend does not start the round until the stream is opened, so there is nothing to
  synchronise across.

**The wait is always bounded** (`TURN_ACK_TIMEOUT_SECONDS = 3.0`). Nobody may be watching — the
round runs whether or not a client is listening (ISSUE-027) — the tab may be backgrounded with
its tweens throttled, or the connection may have dropped. Timing out is a normal outcome that
logs at INFO and continues, never a stall.

## Alternatives considered

**Client-side pacing only, with the backend running ahead.** The client queues events and
releases them behind each animation. Visually near-identical, needs no new endpoint, and cannot
stall a round. Rejected because it does not do what was actually asked: the next detective's LLM
call still fires while the previous pawn is mid-flight, so the agent is reasoning about a board
the human is still watching rearrange. Worth remembering as the fallback if the handshake ever
proves fragile.

**A fixed backend sleep matching the animation duration.** Simpler than an ack and impossible to
stall. Rejected as the primary mechanism because it is open-loop — a backgrounded tab throttles
Phaser's tweens and the two clocks drift apart. It survives as the *timeout* behaviour, which is
the same thing in the case where no ack is coming.

**Keeping the round-start zone snapshot** so all five detectives reason about an identical zone.
Rejected: `rules.md` §2 asks for "the current board state" at the start of each turn, and a stale
occupancy picture is exactly the kind of quiet inaccuracy that is hard to notice and hard to
debug.

**Applying moves but keeping `reserved_nodes`** so a vacated node stays blocked. Rejected as
deliberately preserving a constraint the rules do not impose, on a board where node scarcity
already bites.

## Consequences

- **Detective turn order now carries a real, visible advantage.** It always did, but with moves
  applied per turn it also decides who gets first refusal on a contested node in a way the board
  makes obvious. Still fixed Red → Purple, per ADR-0009's own reasoning.
- **A round takes ~6 s longer** — five 1 s animations plus ack round-trips — on top of ADR-0009's
  ~2.2 min. That is the cost of the round being watchable.
- **Two clocks must stay in step:** `TURN_ACK_TIMEOUT_SECONDS` (backend) must stay comfortably
  above `PAWN_MOVE_DURATION_MS` (frontend). Both carry a comment pointing at the other.
  `PAWN_MOVE_DURATION_MS` lives in `boardDimensions.ts`, the deliberately Phaser-free module, so
  `useRoundStream` can read it without dragging Phaser into the main bundle (ISSUE-019).
- **The client mirrors each applied move locally** so the pawn animates and ticket counts stay
  live mid-round. It is a mirror, not a decision — the server already applied it, and
  `round_result` re-syncs authoritatively at the end of every round, so any drift is bounded.
- **`turn_ack` deliberately does not take `session.lock`.** The lock is held for the whole round
  by `round_stream_route`, and this request exists to unblock that loop from the inside; waiting
  on the lock would deadlock the round against itself.
- **A detective that forfeits acks immediately** rather than waiting out the timeout — it never
  animates, so nothing would otherwise report it as settled.
- **`turn_node`'s `config` parameter must stay annotated `RunnableConfig`.** LangGraph decides
  whether to pass a node its config by inspecting that annotation; a plain `dict` hint silently
  passes nothing rather than failing, which is how the handshake first appeared to work while
  doing nothing at all.
- Testing note: httpx's `ASGITransport` buffers a streaming response instead of delivering it
  incrementally, so an in-process test of this handshake sees every event arrive at once, after
  the round. It needs a real server over a real socket; `test_round_stream_concurrency.py` stubs
  the loop and is unaffected.
