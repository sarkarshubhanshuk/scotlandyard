import operator
from typing import Annotated, Dict, List, Optional, TypedDict

from langchain_core.messages import BaseMessage


# Reducer function to merge dictionary updates cleanly across nodes
def update_dict(existing: dict, new: dict) -> dict:
    """Merges new dictionary updates into the existing state dictionary."""
    if not existing:
        return new
    return {**existing, **new}

class Detective(TypedDict):
    """Schema for individual detective position and ticket inventory."""
    node_id: int
    taxi_tickets: int
    bus_tickets: int
    metro_tickets: int

class MrXState(TypedDict):
    """
    Schema for Mr. X's state.

    Only last_known_node/last_known_round/transport_history are ever put in front of the
    DETECTIVES (see agents.py's prompts) - current_node is his real position and must never
    appear in any prompt text the agents receive.

    It IS serialized to the human client, though, so the board can always draw Mr. X's own
    pawn - which is safe precisely because that client is played BY Mr. X and the detectives
    have no client at all. See ADR-0007 and serializers.py, which own that decision; this
    comment used to assert a blanket "never exposed" that the serializer had already stopped
    honoring.
    """
    current_node: int                 # Mr. X's true position - never goes into a detective prompt
    last_known_node: Optional[int]    # None during rounds 1-2 before the 1st reveal
    last_known_round: Optional[int]   # Round number when he last revealed his location
    transport_history: List[str]      # Complete travel log, e.g., ["taxi", "bus", "black"] - a
                                       # double-move inserts a "double" entry immediately before
                                       # its own two hop entries (see mrx_turn.py)
    
    # Mr. X Ticket Inventory (Crucial for detective deduction algorithms)
    taxi_tickets: int
    bus_tickets: int
    metro_tickets: int
    black_tickets: int
    double_tickets: int

class TurnResponse(TypedDict):
    """One non-moving detective's advisory response to the current mover's proposal."""
    responder: str
    response: str
    preferred_node: int      # The responder's own stated preference for ITSELF - advisory only
                             # (ADR-0009): it is never reserved, and the responder is free to
                             # decide differently when its own turn comes round.


class TurnRecord(TypedDict):
    """
    Everything one detective's turn produced, in the order it happened: that detective's
    opening proposal, the four responses to it, and the final move it committed to.

    This is the round's whole narrative - it is what the frontend Chat Log renders, and (for
    the current turn only) what gets fed back into the mover's final-decision prompt.
    """
    proposed_node: int
    proposal_rationale: str
    responses: List[TurnResponse]
    from_node: int           # Where the detective stood before this turn. Recorded because the
                             # move is applied immediately (ADR-0010), so by the time anything
                             # downstream reads this, detectives[det]["node_id"] is already the
                             # destination and the origin is no longer derivable from state.
    committed_node: int
    transport: Optional[str]  # The ticket actually spent; None if the detective could not move.
    decision_rationale: str

class ScotlandYardState(TypedDict):
    """
    The master memory object passed through all LangGraph agent nodes.
    """
    # Game Progress Tracking
    round_number: int

    # Which detective is taking its turn right now, as an index into DETECTIVE_IDS. Starts at
    # 0 (Agent Red) and is advanced by graph.py's router until it reaches NUM_DETECTIVES, at
    # which point every detective has committed and the round finalizes.
    turn_index: int

    # Mr. X Status & Travel Log
    mr_x: MrXState
    
    # Detective Statuses (Keys: rules_constants.py's DETECTIVE_IDS - "agent_red",
    # "agent_blue", "agent_green", "agent_orange", "agent_purple"). Updated DURING a round, as
    # each detective's turn ends and its move is applied (ADR-0010) - not only at round
    # resolution, which is what it used to be.
    detectives: Dict[str, Detective]
    
    # Append-only log of each turn's rendered transcript, one AIMessage per completed turn.
    # Using Annotated with operator.add appends rather than replacing.
    messages: Annotated[List[BaseMessage], operator.add]
    
    # {detective_id: node_id} for every detective that has already taken its turn this round.
    # Its entry is where that detective is now actually standing - the move was applied the
    # moment its turn ended (ADR-0010), so this is a record of who has moved, not a set of
    # reservations waiting to be honoured. Two detectives sharing a destination is impossible
    # because the second one is simply never offered a node the first is standing on.
    committed_moves: Annotated[Dict[str, int], update_dict]

    # Set to the detective that landed on Mr. X, the instant it happens. The rules end the game
    # at that moment, so the router reads this to skip every remaining turn in the round rather
    # than letting four more detectives deliberate over a finished game.
    captured_by: Optional[str]

    # {detective_id: TurnRecord} for every turn taken so far this round - see TurnRecord.
    turn_records: Annotated[Dict[str, TurnRecord], update_dict]
    
    # The round's destination for every detective, assembled by finalize_round_node from
    # committed_moves once all five turns are done.
    final_moves: Annotated[Dict[str, int], update_dict]

    # graph.py:finalize_round_node's Chat-Log preview of final_moves - per detective,
    # {"from_node": int, "to_node": int, "transport": Optional[str]} - computed via
    # transport.py:determine_move_transport, the same helper round_resolver.py:resolve_round
    # itself uses to actually apply the move, so this can never disagree with what's deducted.
    final_move_details: Dict[str, dict]

    # {detective_id: [node, node]} - the last two nodes each detective VACATED, oldest first,
    # and the only thing in this state that survives a round boundary besides positions and
    # tickets (graph.py:build_next_round_state carries it forward).
    #
    # It exists because detectives are otherwise completely stateless across rounds: `messages`
    # is written once per turn but never read back into a prompt, so nothing a detective did
    # last round is visible to it this round. That is what lets two detectives shuffle back and
    # forth between the same pair of nodes indefinitely - each round in isolation, returning
    # looks as good as it did the first time. agents.py:annotate_revisits turns this into a
    # per-candidate flag so the mover can at least see that it is about to double back.
    recent_positions: Annotated[Dict[str, List[int]], update_dict]