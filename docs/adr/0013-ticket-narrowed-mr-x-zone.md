# ADR-0013 — Mr. X's possible zone is narrowed by his own travel log

- **Status:** Accepted (2026-09-14)
- **Area:** `backend/scotland_yard/travel_log.py`, `game_master.py`, `agents.py`
- **Supersedes:** the "Won't Fix" decision recorded as `known_issues.md` ISSUE-015

## Context

`compute_mrx_zone` answered "where could Mr. X be?" with an untyped BFS ball: every node within
`min(turns_since_surfacing, 4)` hops of his last-known position, over any connection type. That
ball is a strict superset of the truth, which is the safe direction — but it is a very loose
one. Measured across all 199 board nodes it averages 18 nodes at 2 hops and 88 at 4, and the
4-hop case covers most of the board, at which point `format_mrx_zone_block` stops listing it at
all and shows only a count.

Three symptoms traced back to that looseness:

- Detectives drifted **away** from Mr. X in the rounds right after he surfaced, when the intel
  was at its most valuable.
- Detectives **shuttled between two nodes**. With a wide zone, many of a nearby detective's
  candidates all score `distance_to_mrx_zone: 0`, so the primary signal carries no
  discriminating information and the choice among them is close to arbitrary.
- The travel log was shown to the model as a raw list explicitly labelled "NOT his location",
  and nothing anywhere used it.

`ISSUE-015` had already considered ticket-awareness and declined it.

## Decision

**Walk the board one ticket-typed layer per logged hop, instead of an untyped ball.**

```
S₀ = {last_known_node}
S₍ᵢ₊₁₎ = { v : u ∈ Sᵢ, edge(u,v) type matches ticket[i], v not detective-occupied }
```

`travel_log.py` re-derives which hops belong to which round (the flat log interleaves a
`"double"` sentinel before a double-move's two hops) and returns the exact ticket sequence spent
since the reveal. `compute_mrx_zone_from_tickets` walks it.

No hop cap. The cap existed because the untyped ball saturated the board; a typed walk does not,
and "the last four tickets" is meaningless anyway without knowing which set they were spent from.

## Why this is not the thing ISSUE-015 declined

That proposal was to model whether Mr. X's remaining **inventory** could have afforded a
hypothetical path — which is genuinely hard, gets harder as his inventory shrinks, and is what
its "materially more complex" rejection was about.

This models something else entirely: **the tickets he demonstrably spent**, which the rules
publish, hop by hop, in order. There is no inference and no hypothesis. A `"bus"` entry means
that hop crossed a bus edge; the nodes not on a bus edge were never reachable, so excluding them
excludes nothing that was ever possible.

## Consequences

Measured on the real board, mean / max zone size:

| Hops since reveal | Untyped ball | Typed walk |
|---|---|---|
| 1 | 5.4 / 14 | 3.4 / 7 |
| 2 | 18.2 / 54 | 9.5 / 19 |
| 3 | 45.2 / 126 | 20.0 / 44 |
| 4 | 87.6 / 186 | 34.9 / 80 |

At 2 hops the maximum now falls under `MRX_ZONE_LIST_THRESHOLD`, so the prompt shows the actual
candidate nodes where it previously showed an unusable count. That is the step change; the
smaller number is secondary.

**Black tickets become genuinely load-bearing.** A `"black"` entry widens back to every
connection type, because that is exactly what a black ticket buys. Mr. X starts with five, and a
sharp human can spend one right after surfacing to restore the old fuzziness deliberately. This
is a feature: it converts an obfuscation ticket that previously changed nothing about detective
reasoning into a real, scarce, tactical resource.

**The safety direction is one-way and must stay that way.** This set is used to rule locations
*out*. Wider than the truth only makes detectives cautious; narrower makes the game unfair. So
every path that cannot be reconciled — a malformed log, a round count that disagrees, a walk
that dead-ends — falls back to the untyped ball and logs at WARNING, rather than narrowing on a
guess. `test_board_graph.py::TestTicketNarrowedZone::test_the_true_position_is_always_inside_the_zone`
walks Mr. X over the real board 200 times and asserts the derived zone still contains him.

**Cost is negligible and adds no LLM calls.** A typed walk is cheaper than the ball it replaces
(smaller frontier). The deliberate alternative — asking the model to reason about the ticket log
itself — would have added ~5 calls per round and is precisely what this codebase already refuses
to do for ticket arithmetic (ISSUE-005, `annotate_onward_options`).

## Alternatives considered

**Narrow only for the first 1-2 rounds after a reveal.** The technique is identical at every hop
count and costs the same, so capping it would have thrown away accuracy for no saving.

**Also model inventory feasibility (prune paths he could not have paid for).** Still declined,
for ISSUE-015's original reasons. Note the one place it leaks in: `project_zone_one_hop` includes
boat edges without checking he still holds a black ticket to pay for one. Deliberate — it keeps
the projection a superset.
