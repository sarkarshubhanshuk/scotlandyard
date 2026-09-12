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
    Schema for Mr. X's state. Only last_known_node/last_known_round/transport_history are
    ever shown to detectives (see agents.py's prompts) - current_node is his real, secret
    position and must never be read by any detective-facing code path.
    """
    current_node: int                 # Mr. X's true position - NEVER exposed to detectives
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

class DetectiveStrategy(TypedDict):
    """Stores a detective's global strategy (moves for everyone + rationale)"""
    proposed_board_moves: Dict[str, int]
    rationale: str

class ScotlandYardState(TypedDict):
    """
    The master memory object passed through all LangGraph agent nodes.
    """
    # Game Progress Tracking
    round_number: int

    # Tracks the 1-3 voting loops
    debate_loop_count: int

    # Per-round memoization of agents.py:compute_mrx_zone_context() - identical inputs across
    # propose/debate/vote and every debate-loop iteration within one round, so it's computed
    # once (by whichever of those nodes runs first) and reused for the rest of the round. Key
    # absence (not just None, which is a legitimate pre-reveal value) means "not yet computed
    # this round" - build_next_round_state omits this field so each new round starts fresh.
    mrx_zone_context: Optional[dict]

    # Mr. X Status & Travel Log
    mr_x: MrXState
    
    # Detective Statuses (Keys: agents.py's DETECTIVE_IDS - "agent_red", "agent_blue",
    # "agent_green", "agent_yellow", "agent_purple")
    detectives: Dict[str, Detective]
    
    # Shared Debate History
    # Using Annotated with operator.add appends new messages rather than replacing history
    messages: Annotated[List[BaseMessage], operator.add]
    
    # Each detective proposes moves for ALL detectives
    proposed_strategies: Annotated[Dict[str, DetectiveStrategy], update_dict]

    # Each debate speaker's structured post-debate stance (ISSUE-004):
    # {speaker_id: {target_id: preferred_node}}, pending targets only. Plain overwrite field -
    # no memoization needed like mrx_zone_context, since debate_node always runs immediately
    # before vote_node reads it, every loop and every round, so it's never stale when read.
    debate_positions: Optional[Dict[str, Dict[str, int]]]

    # Moves that have passed the 3-vote threshold
    locked_moves: Annotated[Dict[str, int], update_dict]
    
    # Final Locked Moves agreed upon after debate consensus
    final_moves: Annotated[Dict[str, int], update_dict]