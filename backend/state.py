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
    Schema for Mr. X's public information visible to all detectives.
    """
    last_known_node: Optional[int]    # None during rounds 1-2 before the 1st reveal
    last_known_round: Optional[int]   # Round number when he last revealed his location
    transport_history: List[str]      # Complete travel log, e.g., ["taxi", "bus", "black"]
    
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
    
    # Mr. X Status & Travel Log
    mr_x: MrXState
    
    # Detective Statuses (Keys: "detective_1", "detective_2", ..., "detective_5")
    detectives: Dict[str, Detective]
    
    # Shared Debate History
    # Using Annotated with operator.add appends new messages rather than replacing history
    messages: Annotated[List[BaseMessage], operator.add]
    
    # Each detective proposes moves for ALL detectives
    proposed_strategies: Annotated[Dict[str, DetectiveStrategy], update_dict]

    # Moves that have passed the 3-vote threshold
    locked_moves: Annotated[Dict[str, int], update_dict]
    
    # Final Locked Moves agreed upon after debate consensus
    final_moves: Annotated[Dict[str, int], update_dict]