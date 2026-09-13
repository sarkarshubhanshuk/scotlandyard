# ADR-0009 — Turn-wise detective play: propose, respond, commit

- **Status:** Accepted — supersedes [ADR-0006](0006-sequential-debate-and-majority-vote.md),
  refined by [ADR-0010](0010-per-turn-move-application-and-pawn-handshake.md)
- **Area:** `backend/scotland_yard/agents.py`, `graph.py`, `state.py`, `rules_constants.py`,
  `serializers.py`, `server.py`, `frontend/src/hooks/useRoundStream.ts`,
  `frontend/src/components/ChatLog.tsx`

## Context

ADR-0006 had all five detectives decide **simultaneously**: propose concurrently, debate
sequentially, vote concurrently, 3-of-5 to lock, looping up to three times. It produced good
moves, and the strict-majority threshold was load-bearing for correctness (ISSUE-010).

The problem was never move quality — it was **legibility**. A round delivered three bursts of
five messages each, up to three times over, and the human player could not follow which
detective was arguing for what, or why any particular move happened. Fifteen messages landing
at once is not a debate anyone can read. Worse, each burst only reached the client once the
*entire* stage had finished, so the Chat Log alternated between frozen and flooded.

The underlying tension: a simultaneous decision has no natural narrative order, so no amount
of frontend work could impose one on it.

## Decision

**Detectives take turns, one at a time, in fixed `DETECTIVE_IDS` order. Each turn is a
proposal, four responses, and a binding decision — six LLM calls.**

For the detective whose turn it is (the *mover*):

1. **Propose (1 call).** The mover picks a destination for itself and broadcasts it, given
   Mr. X's last known node, his possible zone, its own legal destinations annotated with both
   `distance_to_mrx_zone` and `onward_moves_after`, and its own ticket inventory.
2. **Respond (4 calls, sequential).** Every other detective answers once, in cyclic
   `DETECTIVE_IDS` order starting from the mover's immediate successor — so Agent Green's turn
   is answered by Orange, Purple, Red, Blue. Each responder sees the mover's options and
   proposal, its own options, and the responses given before it this turn.
3. **Decide (1 call).** The mover, having heard all four, commits. **The commitment is
   final** and every later detective this round must work around it.

Supporting decisions:

- **Responses are advisory.** A responder states the node it would take on its own turn, but
  that reserves nothing and it may decide otherwise when its turn arrives — by then the board
  has changed. An illegal stated preference is *dropped* from the transcript rather than
  rewritten, because rewriting it would manufacture an opinion that then feeds into the
  mover's decision prompt.
- **Fixed turn order**, not rotating. Predictability for the player was judged worth more than
  evening out the first-mover advantage. See Consequences.
- **Collaboration tendency replaces desperation.** `get_psychology_prompt` keeps its three
  motivations verbatim; the ladder underneath them now scales how much weight a detective
  gives a teammate's argument, stated as a literal percentage: rounds ≤4 → 1%, 5–8 → 25%,
  9–12 → 50%, 13–16 → 75%, ≥17 → 99%. It is a prompt tier, not a random draw — the percentage
  steers the model rather than gating it, which keeps rounds deterministic and testable.
- **Per-turn transcripts.** Each turn's conversation starts fresh. What earlier turns decided
  is still visible as committed moves on the board, which is the part that actually binds.
- **Moves commit during the turn but apply at the end of the round.** `resolve_round` is
  untouched: ticket transfer, the capture short-circuit, and every win condition work exactly
  as before. **(Reversed by ADR-0010** — each move now takes effect at the end of its own turn,
  which is what `rules.md` §2 describes and what lets the board animate one pawn at a time.
  `reserved_nodes` went with it.)

### What this deletes

Sequential commitment makes duplicate destinations **structurally impossible**: the mover's
legal-move set already excludes every destination committed earlier this round
(`fetch_legal_moves`'s `reserved_nodes`). A whole layer of after-the-fact defence is therefore
obsolete rather than merely unused:

| Removed | Why it is no longer needed |
|---|---|
| `VOTE_THRESHOLD`, `MAX_DEBATE_LOOPS`, `locked_moves`, `debate_loop_count`, the vote router | There is no vote |
| `VOTE_THRESHOLD * 2 > NUM_DETECTIVES` assert (ISSUE-010) | No cross-tally exists to collide |
| ISSUE-025's shared-helper fix | One call site, so no two paths can drift apart |
| `_resolve_fallback_moves`'s de-duplication (ISSUE-026) | Every turn commits; there are no unlocked detectives to fall back for |
| `find_proposal_conflicts` | Collapses to "is this node in my own legal set" |
| The dynamic 5-field pydantic schemas | Each call decides exactly one node |

`finalize_round_node`'s collision assert is **kept** as defence in depth even though the paths
it was written for can no longer fire.

## Alternatives considered

**Keep simultaneous decisions, fix only the streaming.** Emitting each of the fifteen calls as
it completed would have helped, and is in fact part of this change — but it does not give the
messages an order that means anything. Five proposals streamed one by one are still five
independent monologues.

**Rotate turn order each round** (start from `round_number % 5`). Genuinely better on fairness
grounds: Agent Red currently always picks from a free board and Agent Purple always picks
against four reserved nodes. Rejected because a fixed order is something the player learns
once and can then follow without effort, and that legibility is the entire point of this
change. Revisit if Purple's disadvantage shows up as a measurable handicap.

**Binding responses** — reserve a responder's stated node immediately. Rejected: it would make
turn order overwhelmingly decisive and reduce a detective's own turn to a formality.

**A literal random draw for collaboration probability.** Rejected: an LLM cannot act on a
probability, only on an instruction, and a per-call RNG would make rounds irreproducible for
no behavioural gain.

**Fewer responders** (only the two nearest detectives answer). Rejected for now — it is the
obvious latency lever if 30 calls per round proves too slow, and it stays available.

## Consequences

- **Latency roughly doubles to quadruples, and this is the real cost.** Nothing in a turn can
  be parallelised: responses must see each other, and each turn must see the previous turn's
  commitment. The critical path goes from 7 sequential call-slots per loop (7–21 per round) to
  a flat 30. At the ~4.3 s/call this route averages, a round runs ~2.2 min against 30–90 s
  before — roughly 52 min of detective thinking across a full 24-round game.
- **Perceived latency improves anyway**, which is the point. Thirty messages arriving ~4 s
  apart in narrative order is close to reading pace; the player is never staring at a frozen
  panel. This only holds because streaming granularity changed with it — `agents.py` emits a
  custom stream event **per LLM call**, and `serialize_loop_event` no longer re-sends the
  node-level update.
- **Token cost is roughly flat and now predictable:** ~31k input tokens per round, against
  25k–76k before. Doubling the call count is offset by each call carrying one or two
  detectives' option lists (~82 tokens each) instead of all five (~639), and by the vote
  phase — previously the most expensive stage at ~2,400 tokens/call — disappearing entirely.
- **Structured-output failures should drop.** Each call now returns one integer and a short
  string rather than five integers, which is far less room for the reasoning-budget truncation
  documented in ISSUE-006/007.
- **Tail latency is no longer masked.** A straggler used to overlap four concurrent siblings;
  now it adds its full duration to the round. `llm_client.py`'s `timeout=45` is an *idle-gap*
  timeout, not a deadline (ISSUE-009), so `agents.py` wraps every call in
  `asyncio.wait_for(LLM_CALL_DEADLINE_SECONDS)` and resolves a timed-out detective
  deterministically.
- **First-mover advantage is real and accepted.** Agent Red chooses from an unconstrained
  board; Agent Purple chooses last against four reserved nodes and is the most likely to hit
  the "no free legal move, stay put" path.
- Every LLM output is still treated as untrusted: `_enforce_legal_node` re-checks each named
  node against a legal-move set this code computed itself, illegal advisory preferences are
  dropped, `finalize_round_node` asserts destination uniqueness, and `resolve_round`
  re-derives legality independently (ADR-0001).
