from typing import Dict, List, Optional

from game_master import get_node_info
from round_resolver import SURFACING_ROUNDS
from session import GameSession

VALID_TICKET_TYPES = {"taxi", "bus", "metro", "black"}


class IllegalMoveError(ValueError):
    """Raised when a submitted Mr. X move/hop fails validation. Message is client-facing."""


def _occupied_by_detectives(session: GameSession) -> List[int]:
    return [d["node_id"] for d in session.state["detectives"].values()]


def _legal_moves_from(node: int, ticket_counts: dict, occupied: set) -> List[Dict]:
    """
    Shared core of get_mr_x_legal_moves: Mr. X's legal single-hop destinations from `node`,
    each annotated with which ticket type(s) (from `ticket_counts`) could pay for it - native
    transport ticket if held, plus "black" if a black ticket is held (covers ANY route type,
    including boat).
    """
    node_info = get_node_info(node)

    options_by_target: Dict[int, set] = {}
    for connection in node_info["connections"]:
        target = connection["destination"]
        if target in occupied:
            continue
        req_type = connection["type"]

        options = options_by_target.setdefault(target, set())
        if req_type in ("taxi", "bus", "metro") and ticket_counts.get(f"{req_type}_tickets", 0) > 0:
            options.add(req_type)
        if ticket_counts.get("black_tickets", 0) > 0:
            options.add("black")  # Black covers any route type, including boat.

    return [
        {"target_node": target, "ticket_options": sorted(options)}
        for target, options in options_by_target.items()
        if options
    ]


def get_mr_x_legal_moves(session: GameSession, after_hop: Optional[Dict] = None) -> List[Dict]:
    """
    Returns Mr. X's legal single-hop destinations, each annotated with which ticket type(s)
    could be spent to reach it. An empty result (when `after_hop` is None) means Mr. X has no
    legal move at all this round - per the rules he can never forfeit, so this is the
    immediate-loss condition, checked by the caller before offering a move menu rather than
    discovered via a rejected submission.

    after_hop, when given ({"target_node": int, "ticket_type_spent": str}), previews hop-2
    options for a double-move: hop-1 must already be a legal current single hop (validated here
    the same way submit_mr_x_move would, including requiring a double-move ticket in hand, so
    the preview never offers a double-move the eventual submission would reject), and options are
    computed from hop-1's target using tickets remaining after hop-1's spend - never from Mr. X's
    live inventory, or spending the same scarce ticket type on both hops would be missed.
    """
    mr_x = session.state["mr_x"]
    occupied = set(_occupied_by_detectives(session))

    if after_hop is None:
        return _legal_moves_from(mr_x["current_node"], mr_x, occupied)

    if mr_x.get("double_tickets", 0) <= 0:
        raise IllegalMoveError("No double-move tickets remaining.")
    _validate_hop(mr_x["current_node"], after_hop["target_node"], after_hop["ticket_type_spent"], mr_x, occupied)

    post_hop1_tickets = dict(mr_x)
    spent_key = f"{after_hop['ticket_type_spent']}_tickets"
    post_hop1_tickets[spent_key] = post_hop1_tickets.get(spent_key, 0) - 1
    return _legal_moves_from(after_hop["target_node"], post_hop1_tickets, occupied)


def _validate_hop(current_node: int, target_node: int, ticket_type_spent: str, ticket_counts: dict, occupied: set) -> None:
    """
    ticket_counts is whatever tickets are actually available at the moment of THIS hop - for a
    double-move's second hop, the caller must pass a post-hop1 snapshot, not Mr. X's live
    inventory, or spending the same scarce ticket type on both hops would be missed.
    """
    if ticket_type_spent not in VALID_TICKET_TYPES:
        raise IllegalMoveError(f"'{ticket_type_spent}' is not a valid ticket type.")
    if target_node in occupied:
        raise IllegalMoveError(f"Node {target_node} is occupied by a detective.")

    node_info = get_node_info(current_node)
    connection_types = {c["type"] for c in node_info["connections"] if c["destination"] == target_node}
    if not connection_types:
        raise IllegalMoveError(f"Node {target_node} is not connected to node {current_node}.")

    if ticket_type_spent == "black":
        if ticket_counts.get("black_tickets", 0) <= 0:
            raise IllegalMoveError("No black tickets remaining.")
    else:
        if ticket_type_spent not in connection_types:
            raise IllegalMoveError(
                f"No '{ticket_type_spent}' connection between node {current_node} and node {target_node}."
            )
        if ticket_counts.get(f"{ticket_type_spent}_tickets", 0) <= 0:
            raise IllegalMoveError(f"No '{ticket_type_spent}' tickets remaining.")


def _apply_hop(mr_x: dict, target_node: int, ticket_type_spent: str) -> None:
    # Mr. X's own spent tickets are removed from the game entirely - never transferred anywhere.
    mr_x[f"{ticket_type_spent}_tickets"] -= 1
    mr_x["transport_history"].append(ticket_type_spent)
    mr_x["current_node"] = target_node


def submit_mr_x_move(session: GameSession, move_request: dict) -> dict:
    """
    Validates and atomically applies a human-submitted Mr. X move for the current round, then
    flips session.status to "detective_loop_running" - the signal an API layer uses to know the
    detective propose/debate/vote cycle (round_resolver.run_detective_loop) should start next.

    move_request shapes:
      {"move_type": "single", "target_node": int, "ticket_type_spent": "taxi"|"bus"|"metro"|"black"}
      {"move_type": "double", "hop1": {...same two keys...}, "hop2": {...same two keys...}}

    A double-move commits nothing unless BOTH hops validate - there is no cross-request
    "pending double-move" state, the client decides both hops itself before submitting.
    """
    if session.status != "awaiting_mr_x_move":
        raise IllegalMoveError(f"It is not Mr. X's turn (status={session.status}).")

    state = session.state
    mr_x = state["mr_x"]
    occupied = set(_occupied_by_detectives(session))
    move_type = move_request.get("move_type")
    is_surfacing_round = state["round_number"] in SURFACING_ROUNDS

    if move_type == "single":
        hop = move_request["target_node"], move_request["ticket_type_spent"]
        _validate_hop(mr_x["current_node"], hop[0], hop[1], mr_x, occupied)
        _apply_hop(mr_x, hop[0], hop[1])
        if is_surfacing_round:
            mr_x["last_known_node"] = mr_x["current_node"]
            mr_x["last_known_round"] = state["round_number"]

    elif move_type == "double":
        if mr_x.get("double_tickets", 0) <= 0:
            raise IllegalMoveError("No double-move tickets remaining.")
        hop1, hop2 = move_request["hop1"], move_request["hop2"]

        # Validate both hops BEFORE applying either - nothing commits on a partial failure.
        _validate_hop(mr_x["current_node"], hop1["target_node"], hop1["ticket_type_spent"], mr_x, occupied)
        # The intermediate node can never be detective-occupied (rules' "cannot pass through a
        # detective" constraint) - already guaranteed by `occupied` above being reused for hop2,
        # since detectives don't move during Mr. X's own turn.
        # hop2 must be validated against tickets remaining AFTER hop1 spends one, not Mr. X's
        # live inventory - otherwise spending the same scarce ticket type on both hops (e.g. a
        # lone metro ticket) would be missed since hop1 hasn't actually been applied yet.
        post_hop1_tickets = dict(mr_x)
        spent_key = f"{hop1['ticket_type_spent']}_tickets"
        post_hop1_tickets[spent_key] = post_hop1_tickets.get(spent_key, 0) - 1
        _validate_hop(hop1["target_node"], hop2["target_node"], hop2["ticket_type_spent"], post_hop1_tickets, occupied)

        mr_x["double_tickets"] -= 1
        # rules.md: "a Double-Move is broadcast as the Double-Move ticket being played, followed
        # by the two transport tickets sequentially" - "double" is a sentinel never valid as an
        # actual ticket_type_spent (see VALID_TICKET_TYPES), only ever inserted here, so the
        # frontend's travel log can render it as its own icon ahead of the two hop tickets.
        mr_x["transport_history"].append("double")
        _apply_hop(mr_x, hop1["target_node"], hop1["ticket_type_spent"])
        intermediate_node = mr_x["current_node"]
        _apply_hop(mr_x, hop2["target_node"], hop2["ticket_type_spent"])

        # Confirmed with the project owner: on a surfacing round, a double-move reveals only the
        # INTERMEDIATE node - the final destination stays hidden, same as a non-surfacing round.
        if is_surfacing_round:
            mr_x["last_known_node"] = intermediate_node
            mr_x["last_known_round"] = state["round_number"]

    else:
        raise IllegalMoveError(f"Unknown move_type '{move_type}'.")

    session.status = "detective_loop_running"
    return {"status": session.status, "mr_x_current_node": mr_x["current_node"]}
