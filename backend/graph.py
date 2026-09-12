from langgraph.graph import StateGraph, START, END
from state import ScotlandYardState
from agents import propose_node, debate_node, vote_node, DETECTIVE_IDS
from transport import determine_move_transport

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
    
    for det_id in DETECTIVE_IDS:
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

    # A preview of what round_resolver.py:resolve_round is about to apply (from-node, to-node,
    # and which transport it'll spend) - computed here, not there, so the Chat Log's "Final
    # Moves" line can show it via this node's own SSE event, before resolve_round actually runs
    # (it's only called once the whole detective_graph finishes, from server.py). Uses the exact
    # same determine_move_transport() resolve_round itself calls, so the preview can never
    # disagree with what's actually deducted.
    final_move_details = {}
    for det_id in DETECTIVE_IDS:
        detective = state["detectives"][det_id]
        to_node = final_moves[det_id]
        occupied = [d["node_id"] for other_id, d in state["detectives"].items() if other_id != det_id]
        final_move_details[det_id] = {
            "from_node": detective["node_id"],
            "to_node": to_node,
            "transport": determine_move_transport(detective, to_node, occupied),
        }

    return {"final_moves": final_moves, "final_move_details": final_move_details}


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


def build_next_round_state(previous_state: ScotlandYardState) -> ScotlandYardState:
    """
    Builds the initial state for the NEXT round from a completed round's final state.

    locked_moves, proposed_strategies, and debate_loop_count all accumulate WITHIN a single
    round on purpose - that's how the 3-loop consensus tracks who's already locked in. But
    detective_graph has no memory between separate invocations, so if a caller just fed a
    round's raw output straight back in as the next round's initial_state, those fields would
    still show every previously-locked detective as "locked" forever: propose_node and
    vote_node would silently stop running for them starting round 2, and finalize_round_node
    would keep replaying their old locked node as every future round's move. This resets the
    per-round-only fields so every detective starts each new round fully unlocked.

    NOTE: This does not apply final_moves to detective positions or deduct tickets - the
    caller is expected to have already updated state["detectives"] / state["mr_x"] to reflect
    the previous round's outcome before calling this.
    """
    return {
        "round_number": previous_state["round_number"] + 1,
        "debate_loop_count": 0,
        "mr_x": previous_state["mr_x"],
        "detectives": previous_state["detectives"],
        "messages": [],
        "proposed_strategies": {},
        "locked_moves": {},
        "final_moves": {},
        "final_move_details": {},
    }