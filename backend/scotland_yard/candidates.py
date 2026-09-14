"""
A detective's legal destinations, and the deterministic annotations attached to each.

Every number a detective reads about its own options is computed HERE, against the board,
before any prompt is built - never asked of the model. That is the same posture the rest of the
system takes toward LLM output (ADR-0001), applied one step earlier: a small model gets board
arithmetic quietly wrong, so hop distances, onward mobility, containment and revisit history are
all measured rather than described (known_issues.md ISSUE-005/ISSUE-016).

Split out of agents.py, which had grown to carry five separate jobs in one file. This one changes
whenever the *scoring* of a move changes; `prompts.py` changes whenever the *wording* does. They
had been sharing a blast radius for no reason.
"""
from typing import Optional

from .board import compute_valid_moves, project_zone_one_hop
from .state import ScotlandYardState
from .transport import detective_ticket_counts, determine_move_transport


def fetch_legal_moves(state: ScotlandYardState, det_ids: list) -> tuple[dict, dict]:
    """
    The legal target nodes each detective in `det_ids` can reach right now, as both a
    displayable list (for prompt text) and a set (for validating LLM output against).

    Occupancy is simply every OTHER detective's current node. That is the whole collision
    guarantee, and it is exact rather than approximate because a detective's move is applied
    the moment its turn ends (ADR-0010): by the time the next detective asks this question, an
    earlier mover is genuinely standing on its new node and has genuinely vacated its old one.

    This used to need a `reserved_nodes` argument. Moves were committed during the turn but not
    applied until the round finalized, so a committed detective was still physically on its old
    node and its destination had to be excluded by hand - and that node stayed blocked for the
    rest of the round even though nobody was on it any more, which rules.md never asked for.
    Applying per turn removes both the extra argument and that phantom occupancy.

    Board legality is computed in-process via board.compute_valid_moves rather than
    over MCP: these are lookups this code makes on the agents' behalf, not model-initiated
    tool calls, so the stdio round-trip bought nothing. See ADR-0001.
    """
    all_detective_nodes = {d["node_id"] for d in state["detectives"].values()}
    legal_moves_context = {}
    legal_move_sets = {}
    for det_id in det_ids:
        d_info = state["detectives"][det_id]
        occupied = list(all_detective_nodes - {d_info["node_id"]})
        moves = compute_valid_moves(
            d_info["node_id"], d_info["taxi_tickets"], d_info["bus_tickets"],
            d_info["metro_tickets"], black_tickets=0, occupied_nodes=occupied,
        )
        legal_moves_context[det_id] = moves
        legal_move_sets[det_id] = {m["target_node"] for m in moves if "target_node" in m}
    return legal_moves_context, legal_move_sets


def annotate_zone_distances(legal_moves_context: dict, distances_to_zone: dict) -> None:
    """Adds each candidate move's hop-distance to Mr. X's possible zone, in place."""
    for moves in legal_moves_context.values():
        for move in moves:
            move["distance_to_mrx_zone"] = distances_to_zone.get(move["target_node"])


def other_detective_nodes(state: ScotlandYardState, exclude_det_id: str) -> list:
    """
    Where the other four detectives are standing right now.

    Positions are live under per-turn application (ADR-0010), so this needs no projection: a
    detective that has already moved this round is already on its new node. It previously had
    to merge in `committed_moves` by hand, because a committed detective had not physically
    moved yet.
    """
    return [
        info["node_id"]
        for det_id, info in state["detectives"].items()
        if det_id != exclude_det_id
    ]


def annotate_onward_options(
    state: ScotlandYardState, det_id: str, legal_moves_context: dict
) -> None:
    """
    Adds "onward_moves_after" to every candidate: how many distinct nodes `det_id` would still
    be able to reach NEXT round from that destination, with the ticket it would have spent
    getting there already deducted.

    This is what keeps a detective from stranding itself - a candidate whose
    onward_moves_after is 0 is a dead end, and one that costs the last metro ticket to reach a
    metro-only junction shows up as a sharply lower number than its neighbours. Computed
    deterministically here rather than left to the model to reason out, for the same reason
    distance_to_mrx_zone is (ISSUE-005): it is a cheap board lookup, and asking a small model
    to simulate ticket arithmetic is exactly the kind of thing it gets quietly wrong.

    Which ticket a move spends is resolved by transport.determine_move_transport - the same
    helper resolve_round uses to actually deduct it - so this can never disagree with what the
    move will really cost. Costs one integer per candidate in prompt text.
    """
    detective = state["detectives"][det_id]
    occupied_now = other_detective_nodes(state, det_id)
    onward_cache: dict[int, Optional[int]] = {}

    for move in legal_moves_context.get(det_id, []):
        target = move.get("target_node")
        if target is None:
            continue
        if target not in onward_cache:
            transport = determine_move_transport(detective, target, occupied_now)
            if transport is None:
                onward_cache[target] = None
            else:
                remaining = detective_ticket_counts(detective)
                remaining[f"{transport}_tickets"] -= 1
                onward = compute_valid_moves(
                    target, remaining["taxi_tickets"], remaining["bus_tickets"],
                    remaining["metro_tickets"], black_tickets=0,
                    # This detective has vacated its old node by then, so only the other four
                    # constrain it. Detectives later in the turn order will move again before
                    # this lookahead comes true, so it is a snapshot, not a guarantee.
                    occupied_nodes=occupied_now,
                )
                onward_cache[target] = len({m["target_node"] for m in onward if "target_node" in m})
        move["onward_moves_after"] = onward_cache[target]


def annotate_zone_shrink(
    state: ScotlandYardState, det_id: str, legal_moves_context: dict, zone_nodes: list
) -> None:
    """
    Adds "zone_size_after" to every candidate: how many nodes Mr. X could be on NEXT round if
    this detective takes that destination - lower means more of his escape space is closed off.

    This is the metric that actually expresses "converge on him". Distance alone does not:
    five detectives each minimising their own hop-distance all pile onto whichever side of the
    zone faces them, and he simply leaves by the other (the convergence-not-triangulation trap
    already recorded in known_issues.md ISSUE-016). Standing ON an exit is what shrinks the
    zone, and the shrink is directly measurable, so it is measured here rather than described
    to the model in words it would have to apply geometrically.

    Untyped by necessity - his next ticket has not been played yet, so there is nothing to
    filter by. See project_zone_one_hop.

    **Dropped entirely when every candidate scores the same.** That is the common case for a
    detective still several hops out, where nowhere it can stand this round touches his space
    at all, and a column of identical numbers is prompt weight that carries no decision.
    """
    if not zone_nodes:
        return

    moves = legal_moves_context.get(det_id, [])
    others = set(other_detective_nodes(state, det_id))
    shrink_cache: dict[int, int] = {}

    for move in moves:
        target = move.get("target_node")
        if target is None:
            continue
        if target not in shrink_cache:
            shrink_cache[target] = len(
                project_zone_one_hop(zone_nodes, blocked_nodes=others | {target}))
        move["zone_size_after"] = shrink_cache[target]

    if len(set(shrink_cache.values())) <= 1:
        for move in moves:
            move.pop("zone_size_after", None)


def annotate_revisits(
    state: ScotlandYardState, det_id: str, legal_moves_context: dict
) -> bool:
    """
    Flags candidates this detective has recently vacated, and reports whether any exist.

    A detective has no other memory of its own movement (state.py:recent_positions explains
    why), so without this, stepping back to the node it just left looks exactly as attractive
    as it did the first time - which is how two nodes turn into a shuttle. The flag is only set
    on the offending candidates; absence means "not a revisit", and a caller that gets False
    back leaves the whole explanation out of the prompt rather than explaining a rule that
    nothing this turn can break.
    """
    recent = set((state.get("recent_positions") or {}).get(det_id) or [])
    if not recent:
        return False

    flagged = False
    for move in legal_moves_context.get(det_id, []):
        if move.get("target_node") in recent:
            move["you_were_here_recently"] = True
            flagged = True
    return flagged


def sort_candidates(legal_moves_context: dict) -> None:
    """
    Orders every detective's candidate list best-first, in place.

    Costs nothing - no tokens, no latency, no extra computation - and small models weight what
    they read first. The list previously came out in map.json's own connection order, which
    carries no meaning at all. Ties break on node id purely so the ordering is reproducible.
    """
    for moves in legal_moves_context.values():
        moves.sort(key=lambda move: (
            # An unreachable candidate (no path to the zone) sorts last rather than crashing.
            move.get("distance_to_mrx_zone") if move.get("distance_to_mrx_zone") is not None else 1_000,
            move.get("zone_size_after", 0),          # absent (undiscriminating) == neutral
            -(move.get("onward_moves_after") or 0),  # more onward options first
            move.get("target_node", 0),
        ))
