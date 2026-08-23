from langgraph.graph import StateGraph, START, END
from state import ScotlandYardState
from agents import propose_node, debate_node, vote_node

def check_vote_status(state: ScotlandYardState) -> str:
    """
    ROUTER: Decides whether to loop back to debate or finalize the round.
    """
    locked_count = len(state.get("locked_moves", {}))
    
    if locked_count == 5:
        print("\n>>> ROUTER: All 5 moves locked! Ending debate.")
        return "finalize"
        
    if state.get("debate_loop_count", 0) >= 3:
        print(f"\n>>> ROUTER: Max loops (3) reached. Forcing resolve for remaining {5 - locked_count} detectives.")
        return "finalize"
        
    print(f"\n>>> ROUTER: Only {locked_count}/5 moves locked. Looping back to Proposal Phase.")
    return "propose"


def finalize_round_node(state: ScotlandYardState) -> dict:
    """
    PHASE 4: FALLBACK & ROUND CLEANUP
    If the max loops are reached, any detective without a passed vote 
    will default to the move they proposed for themselves in their latest strategy.
    """
    print("\n--- PHASE 4: FINALIZING ROUND MOVES ---")
    locked = state.get("locked_moves", {})
    proposals = state.get("proposed_strategies", {})
    final_moves = {}
    
    detectives = ["detective_1", "detective_2", "detective_3", "detective_4", "detective_5"]
    
    for det_id in detectives:
        if det_id in locked:
            final_moves[det_id] = locked[det_id]
        else:
            # Fallback: What did this detective propose for themselves in the latest loop?
            try:
                fallback_move = proposals[det_id]["proposed_board_moves"][det_id]
                print(f"Fallback triggered for {det_id}: Resorting to self-proposed Node {fallback_move}")
                final_moves[det_id] = fallback_move
            except KeyError:
                # Extreme fallback: Stay put if data is missing/corrupted
                current_node = state["detectives"][det_id]["node_id"]
                print(f"Emergency fallback for {det_id}: Staying put at Node {current_node}")
                final_moves[det_id] = current_node
                
    print(f"\n=== FINAL LOCKED MOVES FOR ROUND {state.get('round_number')} ===")
    print(final_moves)
    
    return {"final_moves": final_moves}


# ==========================================
# BUILD THE STATE GRAPH
# ==========================================
builder = StateGraph(ScotlandYardState)

# 1. Add the Nodes (The "Brain" functions)
builder.add_node("propose", propose_node)
builder.add_node("debate", debate_node)
builder.add_node("vote", vote_node)
builder.add_node("finalize", finalize_round_node)

# 2. Add the Standard Edges (The flow)
builder.add_edge(START, "propose")
builder.add_edge("propose", "debate")
builder.add_edge("debate", "vote")

# 3. Add the Conditional Edges (The loops based on vote results)
builder.add_conditional_edges(
    "vote",              # Starting from the vote node
    check_vote_status,   # Run this router function
    {
        "propose": "propose",   # If router returns "propose", loop back
        "finalize": "finalize"  # If router returns "finalize", move forward
    }
)

# 4. Finish the graph
builder.add_edge("finalize", END)

# 5. Compile into a runnable executable
detective_graph = builder.compile()