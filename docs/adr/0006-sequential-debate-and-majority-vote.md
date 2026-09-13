# ADR-0006 — Sequential debate, 3/5 majority vote, 3-loop cap

- **Status:** Superseded by [ADR-0009](0009-turn-wise-detective-play.md)
- **Area:** `backend/scotland_yard/agents.py`, `graph.py`, `rules_constants.py`

## Superseded

Detectives no longer decide simultaneously. ADR-0009 replaced the propose/debate/vote
consensus loop with turn-wise play — one detective at a time, each committing its own move —
because fifteen messages arriving at once, three times a round, was not something the human
player could follow. The record below stands as written; nothing in it was wrong at the time.

Worth keeping in mind if this is ever revisited: the strict-majority threshold argued for
below was load-bearing for *correctness*, not just legitimacy. Turn-wise play does not need it
because sequential commitment makes the collision it ruled out structurally impossible — but
any return to simultaneous decisions would need that argument back.

## Context

The detectives must agree on five simultaneous moves each round. The interesting design
question is not *how to pick good moves* — it is how to make five agents with deliberately
conflicting incentives negotiate, since `get_psychology_prompt` gives each of them a selfish
secondary goal ("YOU must be the one who lands on him") alongside the shared team goal.

These parameters are this project's own game-theoretic design. They are **not** from
`docs/rules/rules.md`, which says nothing about how detectives decide — hence their separate
section in `rules_constants.py`.

## Decision

**Propose concurrently, debate sequentially, vote concurrently, requiring 3 of 5, looping at
most 3 times.**

- **Propose (concurrent).** All five agents propose a full board of moves simultaneously.
  Proposals are blind — no agent sees another's before making its own — so they run in one
  `asyncio.gather` rather than as five sequential calls.
- **Debate (sequential).** Agents speak once each, in fixed `DETECTIVE_IDS` order. The first
  speaker pitches proactively; every later speaker is explicitly told *not* to just agree, and
  sees the transcript so far. Sequence is the whole point: a concurrent debate is five
  monologues.
- **Vote (concurrent).** All five cast ballots simultaneously against the same finished
  transcript, so no voter is influenced by another's ballot.
- **Threshold: `VOTE_THRESHOLD = 3` of 5** — a strict majority.
- **Cap: `MAX_DEBATE_LOOPS = 3`**, after which unlocked detectives fall back to their own
  self-proposals (de-duplicated — see ISSUE-026).

Each loop only asks about *still-undecided* detectives: the schemas are rebuilt from the
pending set, so a locked detective is never re-proposed or re-voted on.

## Alternatives considered

**Simple majority of those voting, or plurality.** Rejected — and this is the one that
matters, because the strict majority is load-bearing for *correctness*, not just legitimacy.
Each ballot is de-duplicated internally, so two *different* detectives' tallies both reaching
the threshold on the *same* node would require `VOTE_THRESHOLD * 2 = 6` distinct ballots from
5 voters: impossible. A plurality rule reintroduces cross-tally collisions (ISSUE-010).
`rules_constants.py` now asserts `VOTE_THRESHOLD * 2 > NUM_DETECTIVES` at import, so retuning
either value fails loudly instead of silently reopening that bug.

**Unanimity.** Rejected: with adversarial selfish goals it would almost never converge, making
the fallback path the normal path.

**Unbounded loops until consensus.** Rejected: each loop is ~15 billable LLM calls with no
guaranteed convergence.

**Letting agents choose the transport type too.** Rejected — see `game_mechanics.md` §2. A node
pair can be connected by more than one transport, so the ticket is chosen deterministically at
apply time by `transport.pick_transport`, and the agents only ever name a target node.

## Consequences

- Desperation scaling (`get_psychology_prompt`) is what keeps the cap from being hit
  constantly: agents are told to be stubborn before round 12 and to compromise after round 18,
  so late-game rounds converge faster by design.
- **The fallback path is a normal outcome, not an error path.** It fires whenever the agents
  genuinely disagree — which is exactly when their independently-generated self-proposals are
  most likely to collide. That is why it needs its own de-duplication rather than trusting the
  proposals (ISSUE-026).
- Debate being sequential means it cannot be parallelised: it is the slowest of the three
  stages by construction, five calls deep rather than one.
- Every LLM output is still treated as untrusted: proposals are checked by
  `find_proposal_conflicts` plus deterministic reassignment, ballots are discarded if illegal
  or self-duplicating, and `resolve_round` re-derives legality independently (ADR-0001).
