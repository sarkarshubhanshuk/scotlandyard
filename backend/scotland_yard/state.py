from typing import Annotated, Dict, List, TypedDict, Optional
import operator
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
    committed_node: int
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

    # Per-round memoization of agents.py:compute_mrx_zone_context(). Its inputs
    # (last_known_node/round, round_number, and the detectives' occupied nodes) are identical
    # for every one of the round's 30 LLM calls: detectives commit destinations during their
    # turns but do not physically move until resolve_round, so state["detectives"] is frozen
    # for the whole round. Computed once by the first turn and reused by the other four. Key
    # absence (not just None, which is a legitimate pre-reveal value) means "not yet computed
    # this round" - build_next_round_state omits this field so each new round starts fresh.
    mrx_zone_context: Optional[dict]

    # Mr. X Status & Travel Log
    mr_x: MrXState
    
    # Detective Statuses (Keys: rules_constants.py's DETECTIVE_IDS - "agent_red",
    # "agent_blue", "agent_green", "agent_yellow", "agent_purple")
    detectives: Dict[str, Detective]
    
    # Append-only log of each turn's rendered transcript, one AIMessage per completed turn.
    # Using Annotated with operator.add appends rather than replacing.
    messages: Annotated[List[BaseMessage], operator.add]
    
    # {detective_id: node_id} for every detective that has already finished its turn this
    # round. A committed destination is final: the next mover's legal-move set excludes it
    # (agents.py:fetch_legal_moves' reserved_nodes), which is what makes two detectives sharing
    # a destination structurally impossible under turn-wise play rather than something a vote
    # threshold has to rule out after the fact. See ADR-0009.
    #
    # Committing is NOT the same as moving: a detective physically stays on its old node until
    # round_resolver.resolve_round applies final_moves at the end of the round, so
    # state["detectives"] is unchanged for the whole round and a committed destination never
    # shows up in the occupied-node set on its own.
    committed_moves: Annotated[Dict[str, int], update_dict]

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