"""
Drives one full round of the detective decision cycle and applies its outcome to the board.

`run_detective_loop` streams the LangGraph run for an API layer to relay; `resolve_round`
then evaluates the round's win conditions. Moves and ticket transfers are NOT applied here any
more - each happens at the end of its own detective's turn (ADR-0010).
"""
import logging
from typing import AsyncIterator, Optional, TypedDict

from .board import compute_valid_moves
from .graph import build_detective_graph, build_next_round_state
from .rules_constants import DETECTIVE_IDS, MAX_ROUND
from .session import GameSession
from .state import ScotlandYardState

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


def _game_over(session: GameSession, winner: str, captured_by: Optional[str] = None) -> RoundResult:
    session.status = "game_over"
    session.winner = winner
    session.winning_detective = captured_by
    logger.info("Game %s over at round %s: %s win.%s",
                session.game_id, session.state["round_number"], winner,
                f" ({captured_by} caught Mr. X)" if captured_by else "")
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

    # The session travels with the run so turn_node can arm and await the pawn-animation
    # handshake (ADR-0010) - LangGraph passes `config` through to any node that accepts it,
    # which keeps a live orchestration object out of the serializable graph state.
    async for mode, chunk in detective_graph.astream(
        session.state,
        stream_mode=["updates", "values", "custom"],
        config={"configurable": {"session": session}},
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
    Evaluates the round's outcome. It no longer MOVES anyone: each detective's move was applied
    the moment its own turn ended (agents.py:apply_detective_move, ADR-0010), so positions and
    ticket inventories are already current by the time this runs. What is left is the set of
    win conditions that can only be judged once the whole round is over.

    Capture is the exception, and it is already decided: a detective landing on Mr. X ends the
    game at that instant, so turn_node records it on state["captured_by"] and the graph's router
    skips every remaining turn. This only has to read the verdict.
    """
    state = session.state
    mr_x = state["mr_x"]

    captured_by = state.get("captured_by")
    if captured_by:
        logger.info("%s landed on Mr. X at Node %s.", captured_by, mr_x["current_node"])
        return _game_over(session, "detectives", captured_by=captured_by)

    # No capture this round - check the three remaining win conditions before continuing.
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
