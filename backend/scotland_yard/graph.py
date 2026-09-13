"""
The LangGraph state machine driving one round's detective decision cycle:

    propose -> debate -> vote -> (loop back to propose, up to MAX_DEBATE_LOOPS) -> finalize

plus finalize's fallback handling and the round-boundary state reset.
"""
import logging

from langgraph.graph import END, START, StateGraph

from .agents import debate_node, fetch_legal_moves, propose_node, vote_node
from .rules_constants import DETECTIVE_IDS, MAX_DEBATE_LOOPS, NUM_DETECTIVES
from .state import ScotlandYardState
from .transport import determine_move_transport

logger = logging.getLogger(__name__)


def _resolve_fallback_moves(state: ScotlandYardState, locked: dict) -> dict:
    """
    Assigns a destination to every detective for this round: the locked one where a vote
    passed, otherwise a fallback.

    The fallback is that detective's own self-proposal from the latest loop - but it CANNOT
    be taken at face value. Each proposer's strategy object is internally de-duplicated, yet
    two unlocked detectives draw their fallbacks from two DIFFERENT proposers' strategy
    objects, generated concurrently and independently. Nothing stops Agent Red's proposal
    from saying Red -> 50 while Agent Blue's says Blue -> 50, and this path fires precisely
    when the agents failed to converge - the disorderly case. Previously those duplicates
    flowed straight into final_moves, and resolve_round then silently forfeited whichever
    detective came second in DETECTIVE_IDS order. See known_issues.md ISSUE-014/ISSUE-026.

    So fallbacks are resolved in fixed DETECTIVE_IDS order against a running `claimed` set,
    mirroring propose_node's own deterministic backup enforcement: a detective whose
    self-proposal is illegal or already claimed is reassigned to its own lowest-numbered free
    legal move, and only stays put if it genuinely has none.
    """
    final_moves = dict(locked)
    claimed = set(locked.values())

    pending = [det_id for det_id in DETECTIVE_IDS if det_id not in locked]
    if not pending:
        return final_moves

    proposals = state.get("proposed_strategies", {})
    # Legal moves are re-derived here rather than trusted from the proposal, for the same
    # reason vote_node re-derives them: nothing out of an LLM loop is taken on faith.
    _, legal_move_sets = fetch_legal_moves(state, pending, reserved_nodes=set(locked.values()))

    for det_id in pending:
        current_node = state["detectives"][det_id]["node_id"]
        legal_for_det = legal_move_sets.get(det_id, set())
        proposed = proposals.get(det_id, {}).get("proposed_board_moves", {}).get(det_id)

        if proposed is not None and proposed in legal_for_det and proposed not in claimed:
            final_moves[det_id] = proposed
            logger.info("Fallback for %s: using self-proposed Node %s", det_id, proposed)
        else:
            remaining = sorted(legal_for_det - claimed)
            if remaining:
                final_moves[det_id] = remaining[0]
                if proposed is None:
                    reason = "missing"
                elif proposed in claimed:
                    reason = "already claimed by another detective"
                else:
                    reason = "not a legal move"
                logger.warning(
                    "[VALIDATION] Fallback for %s: self-proposed Node %s was %s. Reassigned to Node %s.",
                    det_id, proposed, reason, final_moves[det_id])
            else:
                final_moves[det_id] = current_node
                logger.warning(
                    "[VALIDATION] Fallback for %s: no free legal move available - staying at Node %s.",
                    det_id, current_node)
        claimed.add(final_moves[det_id])

    return final_moves


def finalize_round_node(state: ScotlandYardState) -> dict:
    """
    PHASE 4: FALLBACK & ROUND CLEANUP.

    Produces `final_moves` (one destination per detective) plus `final_move_details`, the
    from/to/transport preview the frontend's Chat Log renders before resolve_round actually
    runs. The preview uses the same determine_move_transport() resolve_round itself calls,
    so the two cannot disagree about which ticket a move spends.
    """
    logger.info("--- PHASE 4: FINALIZING ROUND MOVES ---")
    locked = state.get("locked_moves", {})
    final_moves = _resolve_fallback_moves(state, locked)

    # The uniqueness invariant every downstream consumer assumes, asserted rather than hoped
    # for. _resolve_fallback_moves de-duplicates fallbacks and fetch_legal_moves reserves
    # already-locked destinations, so a duplicate reaching here means one of those guarantees
    # has regressed - which must fail loudly rather than degrade into a silently forfeited
    # turn. Detectives staying put are excluded: they are not moving anywhere, so they cannot
    # collide with anyone, and two of them sitting on their own distinct nodes is fine.
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


def build_detective_graph(max_debate_loops: int = MAX_DEBATE_LOOPS):
    """
    Compiles the propose/debate/vote/finalize state machine.

    A factory rather than a module-level side effect: importing this module previously
    compiled the graph outright, which made it impossible to construct a variant (a different
    loop cap, stubbed nodes for a fast deterministic test) and meant anything importing
    round_resolver paid the compile as an import side effect.

    `max_debate_loops` is captured by the router closure rather than read from the constant
    directly, so a caller can build a short-loop graph without mutating module state.
    """
    def check_vote_status(state: ScotlandYardState) -> str:
        """ROUTER: loop back for another debate round, or finalize."""
        locked_count = len(state.get("locked_moves", {}))

        if locked_count == NUM_DETECTIVES:
            logger.info(">>> ROUTER: all %d moves locked - ending debate.", NUM_DETECTIVES)
            return "finalize"

        if state.get("debate_loop_count", 0) >= max_debate_loops:
            logger.info(
                ">>> ROUTER: max loops (%d) reached - forcing resolve for remaining %d detective(s).",
                max_debate_loops, NUM_DETECTIVES - locked_count)
            return "finalize"

        logger.info(">>> ROUTER: only %d/%d moves locked - looping back to proposal phase.",
                    locked_count, NUM_DETECTIVES)
        return "propose"

    builder = StateGraph(ScotlandYardState)
    builder.add_node("propose", propose_node)
    builder.add_node("debate", debate_node)
    builder.add_node("vote", vote_node)
    builder.add_node("finalize", finalize_round_node)

    builder.add_edge(START, "propose")
    builder.add_edge("propose", "debate")
    builder.add_edge("debate", "vote")
    builder.add_conditional_edges("vote", check_vote_status, {"propose": "propose", "finalize": "finalize"})
    builder.add_edge("finalize", END)

    return builder.compile()


def build_next_round_state(previous_state: ScotlandYardState) -> ScotlandYardState:
    """
    Builds the initial state for the NEXT round from a completed round's final state.

    locked_moves, proposed_strategies, and debate_loop_count all accumulate WITHIN a single
    round on purpose - that's how the consensus loop tracks who's already locked in. But the
    compiled graph has no memory between separate invocations, so if a caller fed a round's
    raw output straight back in as the next round's initial_state, those fields would still
    show every previously-locked detective as "locked" forever: propose_node and vote_node
    would silently stop running for them from round 2 on, and finalize_round_node would keep
    replaying their old locked node as every future round's move. This resets the
    per-round-only fields so every detective starts each new round fully unlocked.

    mrx_zone_context is deliberately OMITTED rather than set to None: agents.py's memoization
    treats key *absence* as "not yet computed this round", while None is a legitimate
    pre-reveal value that would wrongly read as a cache hit.

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
