"""
The LangGraph state machine driving one round's detective decision cycle:

    turn -> (loop back to turn, once per detective) -> finalize

Each `turn` is one detective's whole six-call turn (agents.py:turn_node), which also applies
that detective's move and waits for the board to finish animating it (ADR-0010). The router
advances until all five have moved, or stops early if one of them caught Mr. X. See ADR-0009
for why this replaced the propose/debate/vote consensus loop.
"""
import logging

from langgraph.graph import END, START, StateGraph

from .agents import turn_node
from .rules_constants import DETECTIVE_IDS, NUM_DETECTIVES
from .state import ScotlandYardState

logger = logging.getLogger(__name__)


def finalize_round_node(state: ScotlandYardState) -> dict:
    """
    ROUND CLEANUP.

    Produces `final_moves` (where each detective ended up) plus `final_move_details`, the
    from/to/transport summary the frontend's Chat Log renders as the round's recap. Both are a
    record of moves that have already happened, not a preview of moves about to happen - which
    is what they were before ADR-0010 made each move take effect at the end of its own turn.

    There is no fallback resolution and no move application left to do here. Every turn ends
    in a move that agents.py:apply_detective_move has already made real, so this node only
    summarises what happened - both values are read straight out of turn_records rather than
    recomputed, since the detectives have already left the nodes they started from.

    A round cut short by a capture finalizes with only the turns that actually happened. That
    is the rules-correct outcome, not a gap: once a detective lands on Mr. X the game is over
    and the detectives behind it in the turn order never move.
    """
    logger.info("--- FINALIZING ROUND MOVES ---")
    records = state.get("turn_records", {})
    captured_by = state.get("captured_by")

    if captured_by is None:
        missing = [det_id for det_id in DETECTIVE_IDS if det_id not in records]
        if missing:
            raise AssertionError(
                f"finalize_round_node reached with no turn record for {missing}. Every detective "
                "takes a turn unless the round ended in a capture (agents.py:turn_node)."
            )

    moved = [det_id for det_id in DETECTIVE_IDS if det_id in records]
    final_moves = {det_id: records[det_id]["committed_node"] for det_id in moved}
    final_move_details = {
        det_id: {
            "from_node": records[det_id]["from_node"],
            "to_node": records[det_id]["committed_node"],
            "transport": records[det_id]["transport"],
        }
        for det_id in moved
    }

    # The uniqueness invariant every downstream consumer assumes, asserted rather than hoped
    # for. A detective is only ever offered nodes no other detective is standing on, so a
    # duplicate reaching here means that exclusion has regressed - which must fail loudly
    # rather than degrade into two pawns sharing a square. Detectives that forfeited are
    # excluded: they did not move, so they cannot have collided with anyone.
    destinations = [
        detail["to_node"] for detail in final_move_details.values()
        if detail["to_node"] != detail["from_node"]
    ]
    if len(destinations) != len(set(destinations)):
        raise AssertionError(
            f"finalize_round_node produced colliding destinations: {final_move_details}. "
            "Two detectives cannot occupy the same node (rules.md: Node Occupancy)."
        )

    logger.info("=== FINAL MOVES FOR ROUND %s === %s", state.get("round_number"), final_moves)

    return {"final_moves": final_moves, "final_move_details": final_move_details}


def build_detective_graph(num_turns: int = NUM_DETECTIVES):
    """
    Compiles the turn/finalize state machine.

    A factory rather than a module-level side effect: importing this module previously
    compiled the graph outright, which made it impossible to construct a variant (a shorter
    round, stubbed nodes for a fast deterministic test) and meant anything importing
    round_resolver paid the compile as an import side effect.

    `num_turns` is captured by the router closure rather than read from the constant directly,
    so a caller can build a short-round graph without mutating module state.
    """
    def check_turn_status(state: ScotlandYardState) -> str:
        """ROUTER: hand the turn to the next detective, or finalize."""
        turn_index = state.get("turn_index", 0)

        # A capture ends the game the instant it happens, so the detectives still behind this
        # one in the turn order never move - and, just as importantly, never make ~6 billable
        # LLM calls each deliberating over a game that is already decided.
        captured_by = state.get("captured_by")
        if captured_by:
            logger.info(">>> ROUTER: %s caught Mr. X - ending the round immediately.", captured_by)
            return "finalize"

        if turn_index >= num_turns:
            logger.info(">>> ROUTER: all %d detectives have committed - finalizing.", num_turns)
            return "finalize"

        logger.info(">>> ROUTER: %d/%d turns taken - next up is %s.",
                    turn_index, num_turns, DETECTIVE_IDS[turn_index])
        return "turn"

    builder = StateGraph(ScotlandYardState)
    builder.add_node("turn", turn_node)
    builder.add_node("finalize", finalize_round_node)

    builder.add_edge(START, "turn")
    builder.add_conditional_edges("turn", check_turn_status, {"turn": "turn", "finalize": "finalize"})
    builder.add_edge("finalize", END)

    return builder.compile()


def build_next_round_state(previous_state: ScotlandYardState) -> ScotlandYardState:
    """
    Builds the initial state for the NEXT round from a completed round's final state.

    committed_moves, turn_records and turn_index all accumulate WITHIN a single round on
    purpose - that's how the turn sequence tracks who has already moved. But the compiled graph
    has no memory between separate invocations, so if a caller fed a round's raw output
    straight back in as the next round's initial_state, turn_index would already be at 5 and
    the round would finalize without a single detective taking a turn.

    NOTE: detective positions and ticket inventories are NOT touched here - each move was
    already applied the moment its own turn ended (ADR-0010), so mr_x and detectives carry
    forward exactly as they stand.
    """
    return {
        "round_number": previous_state["round_number"] + 1,
        "turn_index": 0,
        "mr_x": previous_state["mr_x"],
        "detectives": previous_state["detectives"],
        "messages": [],
        "committed_moves": {},
        "turn_records": {},
        "captured_by": None,
        "final_moves": {},
        "final_move_details": {},
    }
