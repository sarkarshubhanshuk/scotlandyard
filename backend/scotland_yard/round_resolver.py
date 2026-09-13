"""
Drives one full round of the detective decision cycle and applies its outcome to the board.

`run_detective_loop` streams the LangGraph run for an API layer to relay; `resolve_round`
then applies the resulting moves, transfers tickets, and evaluates every win condition.
"""
import logging
from typing import AsyncIterator, Optional, TypedDict

from .game_master import compute_valid_moves
from .graph import build_detective_graph, build_next_round_state
from .rules_constants import DETECTIVE_IDS, MAX_ROUND
from .session import GameSession
from .state import ScotlandYardState
from .transport import determine_move_transport

logger = logging.getLogger(__name__)

# Built once at import, from the factory rather than as a module-level graph construction -
# see graph.build_detective_graph. Tests that need a variant build their own.
detective_graph = build_detective_graph()


class RoundResult(TypedDict):
    status: str
    winner: Optional[str]
    round_number: int


def _other_detective_nodes(state: ScotlandYardState, exclude_det_id: str) -> list:
    return [d["node_id"] for det_id, d in state["detectives"].items() if det_id != exclude_det_id]


def _game_over(session: GameSession, winner: str) -> RoundResult:
    session.status = "game_over"
    session.winner = winner
    logger.info("Game %s over at round %s: %s win.",
                session.game_id, session.state["round_number"], winner)
    return {
        "status": session.status,
        "winner": session.winner,
        "round_number": session.state["round_number"],
    }


async def run_detective_loop(session: GameSession) -> AsyncIterator[dict]:
    """
    Drives detective_graph for the current round, yielding one labeled event per LangGraph step
    so an API layer can stream each detective's turn to a client in real time.

    Uses stream_mode=["updates", "values", "custom"] together, and "custom" is now the main
    channel: those are agents.py's own get_stream_writer() calls, one per LLM call, so the
    client sees a proposal, each response, and the final decision land individually as they
    happen. An "updates" chunk only arrives once a whole six-call turn has finished, which is
    too coarse to drive the Chat Log - it is used for the finalize node and as a turn-boundary
    marker (see serializers.serialize_loop_event). "values" chunks are full cumulative state
    snapshots after each step; the LAST one becomes the new session.state directly, reusing
    state.py's own reducers (update_dict, operator.add) rather than hand-reimplementing them.
    """
    session.status = "detective_loop_running"
    last_values = session.state

    async for mode, chunk in detective_graph.astream(
        session.state, stream_mode=["updates", "values", "custom"]
    ):
        if mode == "values":
            last_values = chunk
            continue
        if mode == "custom":
            # agents.py owns these payloads entirely - each already carries its own "event"
            # name and fields, so they pass straight through rather than being re-shaped here.
            yield {"type": "turn_event", "payload": chunk}
            continue
        # mode == "updates": chunk is {node_name: partial_state_update}
        for node_name, update in chunk.items():
            yield {"type": "node_finished", "node": node_name, "update": update}

    session.state = last_values


def resolve_round(session: GameSession) -> RoundResult:
    """
    Applies session.state["final_moves"] (produced by detective_graph) to the board: moves each
    detective in fixed DETECTIVE_IDS order, deducting/transferring tickets, and checking for
    capture after EVERY individual move - not just once at the end - since the rules end the
    game the instant any detective lands on Mr. X's real node; the remaining detectives never
    get to move once that happens.

    Never trusts detective_graph's final_moves for legality (same "trust nothing coming out of
    an LLM loop" posture agents.py already applies to every LLM response) - re-derives the
    detective's actual legal moves via compute_valid_moves before applying anything.
    """
    state = session.state
    final_moves = state.get("final_moves", {})
    mr_x = state["mr_x"]

    # Defence in depth for ISSUE-014/ISSUE-026. finalize_round_node already asserts this, so
    # reaching here with a duplicate means final_moves came from somewhere else entirely.
    # Log it rather than silently forfeiting a turn deep inside the apply loop below, which is
    # exactly how this class of bug used to hide.
    moving_destinations = [
        node for det_id, node in final_moves.items()
        if node != state["detectives"][det_id]["node_id"]
    ]
    if len(moving_destinations) != len(set(moving_destinations)):
        logger.error(
            "Colliding destinations in final_moves for game %s round %s: %s. "
            "Applying in DETECTIVE_IDS order; later detectives will forfeit.",
            session.game_id, state["round_number"], final_moves)

    for det_id in DETECTIVE_IDS:
        detective = state["detectives"][det_id]
        target_node = final_moves.get(det_id, detective["node_id"])

        if target_node != detective["node_id"]:
            occupied = _other_detective_nodes(state, det_id)
            transport = determine_move_transport(detective, target_node, occupied)

            if transport is not None:
                ticket_key = f"{transport}_tickets"
                detective[ticket_key] -= 1
                mr_x[ticket_key] += 1  # Rules: a detective's spent ticket transfers to Mr. X.
                detective["node_id"] = target_node
            else:
                # final_moves named a destination that isn't actually legal right now. Should
                # be unreachable given propose/vote/finalize's own enforcement, but it is not
                # trusted blindly - treated as a forfeit rather than crashing the round, and
                # logged at WARNING because it always indicates a real upstream bug.
                logger.warning(
                    "%s could not legally move from Node %s to Node %s - forfeiting the turn.",
                    det_id, detective["node_id"], target_node)

        if detective["node_id"] == mr_x["current_node"]:
            return _game_over(session, "detectives")

    # No capture this round - check the two remaining win conditions before continuing.
    new_detective_nodes = [d["node_id"] for d in state["detectives"].values()]

    mr_x_legal_moves = compute_valid_moves(
        mr_x["current_node"], mr_x["taxi_tickets"], mr_x["bus_tickets"], mr_x["metro_tickets"],
        black_tickets=mr_x["black_tickets"], occupied_nodes=new_detective_nodes
    )
    if not mr_x_legal_moves:
        return _game_over(session, "detectives")

    all_detectives_trapped = True
    for det_id in DETECTIVE_IDS:
        detective = state["detectives"][det_id]
        occupied = _other_detective_nodes(state, det_id)
        if compute_valid_moves(
            detective["node_id"], detective["taxi_tickets"], detective["bus_tickets"],
            detective["metro_tickets"], black_tickets=0, occupied_nodes=occupied
        ):
            all_detectives_trapped = False
            break

    if all_detectives_trapped:
        return _game_over(session, "mr_x")

    if state["round_number"] >= MAX_ROUND:
        return _game_over(session, "mr_x")

    session.state = build_next_round_state(state)
    session.status = "awaiting_mr_x_move"
    return {"status": session.status, "winner": None, "round_number": session.state["round_number"]}
