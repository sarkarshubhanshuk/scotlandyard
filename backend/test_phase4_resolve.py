"""
Sync test script (mirrors test_phase3.py's style) covering session/mrx_turn/round_resolver -
everything in the "apply a round" pipeline that doesn't require a live LLM call. run_detective_loop
itself (which does call the LLM) is intentionally NOT exercised here, same as test_phase3.py's own
LLM-dependent paths - run it manually/opt-in once a valid GROQ_API_KEY is configured.

Run: python test_phase4_resolve.py
"""
from game_master import compute_valid_moves
from agents import DETECTIVE_IDS
from session import create_game
from mrx_turn import get_mr_x_legal_moves, submit_mr_x_move, IllegalMoveError
from round_resolver import resolve_round

SEED_POSITIONS = {
    "mr_x": 13, "agent_red": 26, "agent_blue": 29,
    "agent_green": 34, "agent_yellow": 50, "agent_purple": 53,
}


def total_tickets(state) -> dict:
    total = {"taxi": 0, "bus": 0, "metro": 0}
    for kind in total:
        total[kind] += state["mr_x"][f"{kind}_tickets"]
        for detective in state["detectives"].values():
            total[kind] += detective[f"{kind}_tickets"]
    return total


def test_create_game_seeded():
    print("\n=== TEST: create_game (seeded) ===")
    session = create_game(seed_positions=SEED_POSITIONS)
    assert session.state["mr_x"]["current_node"] == 13
    assert session.state["detectives"]["agent_red"]["node_id"] == 26
    assert session.state["mr_x"]["taxi_tickets"] == 2 and session.state["mr_x"]["black_tickets"] == 5
    assert session.state["detectives"]["agent_red"]["taxi_tickets"] == 11
    assert session.status == "awaiting_mr_x_move"
    print("PASSED")


def test_mrx_single_move_and_illegal_move():
    print("\n=== TEST: Mr. X single move + illegal move rejection ===")
    session = create_game(seed_positions=SEED_POSITIONS)
    legal = get_mr_x_legal_moves(session)
    assert len(legal) > 0
    move = legal[0]

    ticket_key = f"{move['ticket_options'][0]}_tickets"
    before_ticket_count = session.state["mr_x"][ticket_key]
    submit_mr_x_move(session, {
        "move_type": "single", "target_node": move["target_node"], "ticket_type_spent": move["ticket_options"][0]
    })
    assert session.state["mr_x"][ticket_key] == before_ticket_count - 1
    assert session.state["mr_x"]["current_node"] == move["target_node"]
    assert session.state["mr_x"]["transport_history"] == [move["ticket_options"][0]]
    assert session.status == "detective_loop_running"

    # Not Mr. X's turn anymore - any submission now must fail.
    try:
        submit_mr_x_move(session, {"move_type": "single", "target_node": 1, "ticket_type_spent": "taxi"})
        assert False, "expected IllegalMoveError (not Mr. X's turn)"
    except IllegalMoveError:
        pass

    # A fresh game, illegal target node.
    session2 = create_game(seed_positions=SEED_POSITIONS)
    try:
        submit_mr_x_move(session2, {"move_type": "single", "target_node": 999999, "ticket_type_spent": "taxi"})
        assert False, "expected IllegalMoveError (not connected)"
    except IllegalMoveError:
        pass
    print("PASSED")


def test_double_move_cannot_double_spend_scarce_ticket():
    print("\n=== TEST: double-move validates hop2 against POST-hop1 ticket counts ===")
    session = create_game(seed_positions=SEED_POSITIONS)
    mr_x = session.state["mr_x"]
    mr_x["metro_tickets"] = 1  # exactly one metro ticket

    metro_moves = compute_valid_moves(mr_x["current_node"], 0, 0, 1, black_tickets=0)
    if not metro_moves:
        print("SKIPPED (seed position has no metro connection to build this scenario from)")
        return
    hop1_target = metro_moves[0]["target_node"]
    hop2_candidates = compute_valid_moves(hop1_target, 0, 0, 1, black_tickets=0)
    if not hop2_candidates:
        print("SKIPPED (no second metro hop available from the intermediate node)")
        return
    hop2_target = hop2_candidates[0]["target_node"]

    try:
        submit_mr_x_move(session, {
            "move_type": "double",
            "hop1": {"target_node": hop1_target, "ticket_type_spent": "metro"},
            "hop2": {"target_node": hop2_target, "ticket_type_spent": "metro"},
        })
        assert False, "expected IllegalMoveError - only one metro ticket available for two metro hops"
    except IllegalMoveError:
        pass
    assert mr_x["current_node"] == 13, "failed double-move must not have moved Mr. X at all"
    assert mr_x["metro_tickets"] == 1, "failed double-move must not have deducted anything"
    print("PASSED")


def test_resolve_round_non_capturing_conserves_tickets_and_advances_round():
    print("\n=== TEST: resolve_round (non-capturing) - ticket conservation + round advance ===")
    session = create_game(seed_positions=SEED_POSITIONS)
    before = total_tickets(session.state)

    final_moves = {}
    for det_id, detective in session.state["detectives"].items():
        occupied = [d["node_id"] for k, d in session.state["detectives"].items() if k != det_id]
        legal = compute_valid_moves(detective["node_id"], detective["taxi_tickets"], detective["bus_tickets"],
                                     detective["metro_tickets"], 0, occupied)
        choice = next((m for m in legal if m["target_node"] != session.state["mr_x"]["current_node"]), None)
        if choice:
            final_moves[det_id] = choice["target_node"]
    session.state["final_moves"] = final_moves

    result = resolve_round(session)
    after = total_tickets(session.state)

    assert before == after, f"ticket conservation violated: {before} != {after}"
    assert result["status"] == "awaiting_mr_x_move"
    assert result["winner"] is None
    assert session.state["round_number"] == 2
    assert session.state["locked_moves"] == {}
    assert session.state["proposed_strategies"] == {}
    print("PASSED")


def test_resolve_round_capture_short_circuits_remaining_detectives():
    print("\n=== TEST: resolve_round - capture stops remaining detectives from moving ===")
    session = create_game(seed_positions=SEED_POSITIONS)
    d1 = session.state["detectives"]["agent_red"]
    occupied = [d["node_id"] for k, d in session.state["detectives"].items() if k != "agent_red"]
    legal = compute_valid_moves(d1["node_id"], d1["taxi_tickets"], d1["bus_tickets"], d1["metro_tickets"], 0, occupied)
    target = legal[0]["target_node"]

    session.state["mr_x"]["current_node"] = target  # force a guaranteed capture
    d2_original_node = session.state["detectives"]["agent_blue"]["node_id"]
    session.state["final_moves"] = {"agent_red": target, "agent_blue": d2_original_node + 0}
    # Give agent_blue a real (but irrelevant, since the round must stop before they move) move too.
    d2 = session.state["detectives"]["agent_blue"]
    occupied_for_d2 = [d["node_id"] for k, d in session.state["detectives"].items() if k != "agent_blue"]
    d2_legal = compute_valid_moves(d2["node_id"], d2["taxi_tickets"], d2["bus_tickets"], d2["metro_tickets"], 0, occupied_for_d2)
    if d2_legal:
        session.state["final_moves"]["agent_blue"] = d2_legal[0]["target_node"]

    result = resolve_round(session)

    assert result["winner"] == "detectives"
    assert result["status"] == "game_over"
    assert session.state["detectives"]["agent_red"]["node_id"] == target
    assert session.state["detectives"]["agent_blue"]["node_id"] == d2_original_node, \
        "agent_blue must NOT have moved - the round must stop the instant agent_red captures Mr. X"
    print("PASSED")


def test_resolve_round_mr_x_trapped_detectives_win():
    print("\n=== TEST: resolve_round - Mr. X with zero tickets everywhere -> detectives win ===")
    session = create_game(seed_positions=SEED_POSITIONS)
    for ticket_type in ("taxi_tickets", "bus_tickets", "metro_tickets", "black_tickets"):
        session.state["mr_x"][ticket_type] = 0
    session.state["final_moves"] = {}  # detectives all forfeit/stay - irrelevant to this check

    result = resolve_round(session)
    assert result["winner"] == "detectives"
    assert result["status"] == "game_over"
    print("PASSED")


def test_resolve_round_all_detectives_trapped_mr_x_wins():
    print("\n=== TEST: resolve_round - all 5 detectives with zero tickets -> Mr. X wins ===")
    session = create_game(seed_positions=SEED_POSITIONS)
    for detective in session.state["detectives"].values():
        detective["taxi_tickets"] = 0
        detective["bus_tickets"] = 0
        detective["metro_tickets"] = 0
    session.state["final_moves"] = {}

    result = resolve_round(session)
    assert result["winner"] == "mr_x"
    assert result["status"] == "game_over"
    print("PASSED")


def test_resolve_round_24_ends_in_mr_x_win():
    print("\n=== TEST: resolve_round - surviving round 24 -> Mr. X wins ===")
    session = create_game(seed_positions=SEED_POSITIONS)
    session.state["round_number"] = 24
    session.state["final_moves"] = {}

    result = resolve_round(session)
    assert result["winner"] == "mr_x"
    assert result["status"] == "game_over"
    print("PASSED")


if __name__ == "__main__":
    test_create_game_seeded()
    test_mrx_single_move_and_illegal_move()
    test_double_move_cannot_double_spend_scarce_ticket()
    test_resolve_round_non_capturing_conserves_tickets_and_advances_round()
    test_resolve_round_capture_short_circuits_remaining_detectives()
    test_resolve_round_mr_x_trapped_detectives_win()
    test_resolve_round_all_detectives_trapped_mr_x_wins()
    test_resolve_round_24_ends_in_mr_x_win()
    print("\n=== ALL PHASE 4 RESOLVE TESTS PASSED ===")
