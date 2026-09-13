"""
The LangGraph state machine driving one round's detective decision cycle:

    turn -> (loop back to turn, once per detective) -> finalize

Each `turn` is one detective's whole six-call turn (agents.py:turn_node), after which the
router advances until all five have committed. See ADR-0009 for why this replaced the
propose/debate/vote consensus loop.
"""
import logging

from langgraph.graph import END, START, StateGraph

from .agents import turn_node
from .rules_constants import DETECTIVE_IDS, NUM_DETECTIVES
from .state import ScotlandYardState
from .transport import determine_move_transport

logger = logging.getLogger(__name__)


def finalize_round_node(state: ScotlandYardState) -> dict:
    """
    ROUND CLEANUP.

    Produces `final_moves` (one destination per detective) plus `final_move_details`, the
    from/to/transport preview the frontend's Chat Log renders before resolve_round actually
    runs. The preview uses the same determine_move_transport() resolve_round itself calls,
    so the two cannot disagree about which ticket a move spends.

    There is no fallback resolution left to do here. Under the old simultaneous design this
    node had to assign a destination to every detective whose move never reached the vote
    threshold, drawing on self-proposals that were generated independently and could therefore
    collide (ISSUE-014/ISSUE-026). Turn-wise play has no unlocked detectives: every turn ends
    in a committed move, already validated against a legal-move set that excluded everything
    committed before it.
    """
    logger.info("--- FINALIZING ROUND MOVES ---")
    committed = state.get("committed_moves", {})

    missing = [det_id for det_id in DETECTIVE_IDS if det_id not in committed]
    if missing:
        raise AssertionError(
            f"finalize_round_node reached with no committed move for {missing}. Every detective "
            "takes a turn and every turn commits (agents.py:turn_node)."
        )

    final_moves = {det_id: committed[det_id] for det_id in DETECTIVE_IDS}

    # The uniqueness invariant every downstream consumer assumes, asserted rather than hoped
    # for. Each turn's legal-move set excludes every previously committed destination, so a
    # duplicate reaching here means that exclusion has regressed - which must fail loudly
    # rather than degrade into a silently forfeited turn. Detectives staying put are excluded:
    # they are not moving anywhere, so they cannot collide with anyone, and two of them sitting
    # on their own distinct nodes is fine.
    moving = [
        node for det_id, node in final_moves.items()
        if node != state["detectives"][det_id]["node_id"]
    ]
    if len(moving) != len(set(moving)):
        raise AssertionError(
            f"finalize_round_node produced colliding destinations: {final_moves}. "
            "Two detectives cannot move to the same node (rules.md: Node Occupancy)."
        )

    logger.info("=== FINAL MOVES FOR ROUND %s === %s", state.get("round_number"), final_moves)

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
    the round would finalize without a single detective taking a turn, replaying the previous
    round's committed nodes as every future round's move. This resets the per-round-only fields
    so every round starts at Agent Red's turn with nothing committed.

    mrx_zone_context is deliberately OMITTED rather than set to None: agents.py's memoization
    treats key *absence* as "not yet computed this round", while None is a legitimate
    pre-reveal value that would wrongly read as a cache hit.

    NOTE: This does not apply final_moves to detective positions or deduct tickets - the
    caller is expected to have already updated state["detectives"] / state["mr_x"] to reflect
    the previous round's outcome before calling this.
    """
    return {
        "round_number": previous_state["round_number"] + 1,
        "turn_index": 0,
        "mr_x": previous_state["mr_x"],
        "detectives": previous_state["detectives"],
        "messages": [],
        "committed_moves": {},
        "turn_records": {},
        "final_moves": {},
        "final_move_details": {},
    }
