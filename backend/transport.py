from typing import List, Optional

from game_master import compute_valid_moves

# Tie-break when a target node is reachable via more than one transport type: prefer whichever
# type the detective currently holds the most tickets of (conserves the scarce metro allotment
# by default), tied-broken taxi > bus > metro.
TRANSPORT_PRIORITY = {"taxi": 2, "bus": 1, "metro": 0}


def pick_transport(available_transports: List[str], ticket_counts: dict) -> str:
    """
    Deterministic apply-time transport choice for a detective's move. Detectives never choose
    this themselves - agents.py's build_strategy_schema/build_ballot_schema only ever ask for a
    target node, never a transport type, since a node pair can legally be connected by more than
    one transport simultaneously (e.g. map.json's node 1 <-> node 46 via both bus and metro).
    """
    def sort_key(transport):
        return (ticket_counts.get(f"{transport}_tickets", 0), TRANSPORT_PRIORITY[transport])
    return max(available_transports, key=sort_key)


def determine_move_transport(detective: dict, target_node: int, occupied_nodes: List[int]) -> Optional[str]:
    """
    Which transport type a detective's move from their current node to target_node would spend,
    using the exact same legality + tie-break logic round_resolver.py:resolve_round uses to
    actually apply moves - shared here so graph.py:finalize_round_node's Chat-Log preview (shown
    to the client before the round is actually resolved) can never disagree with what
    resolve_round later deducts. Returns None if target_node == the detective's current node, or
    if no legal transport reaches it (should never happen given propose/vote's own legality
    enforcement, but not trusted blindly here either).
    """
    if target_node == detective["node_id"]:
        return None
    legal_moves = compute_valid_moves(
        detective["node_id"], detective["taxi_tickets"], detective["bus_tickets"],
        detective["metro_tickets"], black_tickets=0, occupied_nodes=occupied_nodes
    )
    available_transports = [m["transport_used"] for m in legal_moves if m.get("target_node") == target_node]
    if not available_transports:
        return None
    return pick_transport(available_transports, detective)
