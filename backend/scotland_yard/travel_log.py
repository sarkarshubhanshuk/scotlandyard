"""
Reading Mr. X's public travel log (`mr_x["transport_history"]`) back out, round by round.

The log is a flat list of the ticket type spent on every hop, in order, with a `"double"`
sentinel inserted immediately before a double-move's own two hop entries (rules.md §4's stated
broadcast order; written by `mrx_turn.submit_mr_x_move`). Flat is the right wire format - it is
exactly what the rules say is broadcast - but every consumer that wants to reason about it has
to re-derive which hops belong to which round, so that derivation lives here once rather than
in each of them.

This module deliberately imports nothing else in the package: `mrx_turn` writes the log,
`agents` reads it to narrow Mr. X's possible zone, and neither should have to import the other
to agree on its format.
"""
from typing import List, Optional, Sequence

# Never a valid ticket type - only ever a marker that the NEXT TWO entries are the two hops of
# one double-move, played in a single round. `rules_constants.VALID_TICKET_TYPES` excludes it
# for exactly that reason.
DOUBLE_SENTINEL = "double"


def bucket_hops_by_round(transport_history: Sequence[str]) -> Optional[List[List[str]]]:
    """
    The flat log re-grouped into one list of ticket types per round Mr. X has played, oldest
    first - `[["taxi"], ["bus", "metro"], ["black"]]` for a taxi, then a double-move of
    bus+metro, then a black.

    Returns None if the log cannot be read cleanly (a trailing `"double"` with fewer than two
    hops after it, or a `"double"` where a hop should be). Callers are expected to treat that
    as "I do not know" and fall back to something that assumes less - never to guess, since
    every consumer of this uses it to RULE OUT places Mr. X could be.
    """
    rounds: List[List[str]] = []
    index = 0
    total = len(transport_history)

    while index < total:
        if transport_history[index] == DOUBLE_SENTINEL:
            hops = list(transport_history[index + 1:index + 3])
            if len(hops) != 2 or DOUBLE_SENTINEL in hops:
                return None
            rounds.append(hops)
            index += 3
        else:
            rounds.append([transport_history[index]])
            index += 1

    return rounds


def hops_since_surfacing(
    transport_history: Sequence[str],
    last_known_round: Optional[int],
    round_number: int,
) -> Optional[List[str]]:
    """
    The ticket types Mr. X has spent since the position the detectives last SAW him at, in
    order - the exact sequence of connection types his hops since then must have used.

    Empty list means he has not moved since the reveal (the surfacing round itself, where the
    detectives move after him), which is the strongest possible case: his last-known node IS
    his current node.

    **The surfacing-round double-move case**: on a surfacing round a double-move reveals only
    the INTERMEDIATE node (game_mechanics.md §2), so the second hop of that same round already
    happened after the reveal and counts here - the first does not, since it is the hop that
    produced the revealed node.

    Returns None whenever the log cannot be reconciled with the round number (a malformed log,
    or a round count that does not match the number of moves recorded). Mr. X moves first in
    every round, so by the time detectives deliberate the log must contain exactly
    `round_number` rounds' worth of hops; anything else means an assumption here is wrong, and
    the caller must fall back rather than narrow the zone on a guess.
    """
    if last_known_round is None or last_known_round < 1:
        return None

    rounds = bucket_hops_by_round(transport_history)
    if rounds is None or len(rounds) != round_number or last_known_round > len(rounds):
        return None

    surfacing_round_hops = rounds[last_known_round - 1]
    hops: List[str] = surfacing_round_hops[1:]  # the double-move case above; empty otherwise
    for later_round_hops in rounds[last_known_round:]:
        hops.extend(later_round_hops)
    return hops
