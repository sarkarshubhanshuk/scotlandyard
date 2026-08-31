import asyncio
from graph import detective_graph, build_next_round_state

async def run_test_round():
    print("\n=== STARTING PHASE 3 END-TO-END TEST ===")
    print("Initializing Game State (Round 3 - Mr. X Revealed at Node 13)...")

    # Mock starting state for Round 3
    initial_state = {
        "round_number": 3,
        "debate_loop_count": 0,
        "mr_x": {
            "last_known_node": 13,
            "last_known_round": 3,
            "transport_history": ["taxi", "taxi", "bus"],
            "taxi_tickets": 10,
            "bus_tickets": 8,
            "metro_tickets": 4,
            "black_tickets": 5,
            "double_tickets": 2
        },
        # Spread the detectives out across the map
        "detectives": {
            "detective_1": {"node_id": 29, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
            "detective_2": {"node_id": 50, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
            "detective_3": {"node_id": 91, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
            "detective_4": {"node_id": 117, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
            "detective_5": {"node_id": 123, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
        },
        "messages": [],
        "proposed_strategies": {},
        "locked_moves": {},
        "final_moves": {}
    }

    print("\nFiring up the LangGraph Multi-Agent Engine...")
    
    # We use astream() to process the graph asynchronously and watch the steps unfold
    async for output in detective_graph.astream(initial_state):
        # Output is a dict showing which node just completed
        for node_name, state_update in output.items():
            print(f"\n[SYSTEM] --> Node '{node_name.upper()}' finished execution.")
            
    print("\n=== ROUND COMPLETE ===")


async def run_multi_round_reset_test():
    """
    Verifies the fix for Critical Issue 3: locked_moves/debate_loop_count/proposed_strategies
    must reset between rounds, or detectives locked in round 1 stay permanently locked and
    stop proposing/voting for the rest of the game.
    """
    print("\n=== MULTI-ROUND RESET TEST ===")

    initial_state = {
        "round_number": 3,
        "debate_loop_count": 0,
        "mr_x": {
            "last_known_node": 13,
            "last_known_round": 3,
            "transport_history": ["taxi", "taxi", "bus"],
            "taxi_tickets": 10,
            "bus_tickets": 8,
            "metro_tickets": 4,
            "black_tickets": 5,
            "double_tickets": 2
        },
        "detectives": {
            "detective_1": {"node_id": 29, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
            "detective_2": {"node_id": 50, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
            "detective_3": {"node_id": 91, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
            "detective_4": {"node_id": 117, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
            "detective_5": {"node_id": 123, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
        },
        "messages": [],
        "proposed_strategies": {},
        "locked_moves": {},
        "final_moves": {}
    }

    print("\n--- Running ROUND 1 ---")
    round_1_result = await detective_graph.ainvoke(initial_state)
    print(f"\nRound 1 finished. locked_moves={round_1_result['locked_moves']}, "
          f"debate_loop_count={round_1_result['debate_loop_count']}, "
          f"final_moves={round_1_result['final_moves']}")

    round_2_initial_state = build_next_round_state(round_1_result)
    assert round_2_initial_state["locked_moves"] == {}, "Round 2 must start with no locked moves"
    assert round_2_initial_state["debate_loop_count"] == 0, "Round 2 must start at loop 0"
    assert round_2_initial_state["proposed_strategies"] == {}, "Round 2 must start with no stale proposals"
    print(f"\nBuilt Round 2 initial state: round_number={round_2_initial_state['round_number']}, "
          f"locked_moves={round_2_initial_state['locked_moves']} (reset), "
          f"debate_loop_count={round_2_initial_state['debate_loop_count']} (reset)")

    print("\n--- Running ROUND 2 ---")
    round_2_result = await detective_graph.ainvoke(round_2_initial_state)
    print(f"\nRound 2 finished. final_moves={round_2_result['final_moves']}")

    print("\n=== MULTI-ROUND RESET TEST PASSED ===")


if __name__ == "__main__":
    asyncio.run(run_test_round())
    asyncio.run(run_multi_round_reset_test())