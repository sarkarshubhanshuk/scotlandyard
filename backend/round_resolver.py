from typing import AsyncIterator, Optional, TypedDict

from game_master import compute_valid_moves
from state import ScotlandYardState
from agents import DETECTIVE_IDS
from graph import detective_graph, build_next_round_state
from session import GameSession
from transport import determine_move_transport

MAX_ROUND = 24
SURFACING_ROUNDS = {3, 8, 13, 18, 24}


class RoundResult(TypedDict):
    status: str
    winner: Optional[str]
    round_number: int


def _other_detective_nodes(state: ScotlandYardState, exclude_det_id: str) -> list:
    return [d["node_id"] for det_id, d in state["detectives"].items() if det_id != exclude_det_id]


async def run_detective_loop(session: GameSession) -> AsyncIterator[dict]:
    """
    Drives detective_graph for the current round, yielding one labeled event per LangGraph step
    so an API layer can stream propose/debate/vote progress to a client in real time.

    Uses stream_mode=["updates", "values", "custom"] together: "updates" chunks are
    {node_name: partial} and identify which node just ran (propose/debate/vote/finalize) for
    event labeling; "values" chunks are full cumulative state snapshots after each step; "custom"
    chunks are agents.py's own get_stream_writer() calls, fired the moment a node's first LLM
    call actually goes out (not when the node merely starts, which could still be doing MCP/
    zone-context prep) - this is what lets the frontend show "Detectives are debating..." etc.
    as each stage BEGINS rather than only once "updates" reports the whole node finished. The
    LAST "values" snapshot becomes the new session.state directly - this reuses state.py's own
    reducers (update_dict, operator.add) rather than hand-reimplementing them here.
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
            yield {"type": "stage_started", "stage": chunk["stage"]}
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
            # else: final_moves named a destination that isn't actually legal right now (should
            # never happen given propose/vote's own legality enforcement, but not trusted
            # blindly here either) - treat as a forfeit rather than crash the round.

        if detective["node_id"] == mr_x["current_node"]:
            session.status = "game_over"
            session.winner = "detectives"
            return {"status": session.status, "winner": session.winner, "round_number": state["round_number"]}

    # No capture this round - check the two remaining win conditions before continuing.
    new_detective_nodes = [d["node_id"] for d in state["detectives"].values()]

    mr_x_legal_moves = compute_valid_moves(
        mr_x["current_node"], mr_x["taxi_tickets"], mr_x["bus_tickets"], mr_x["metro_tickets"],
        black_tickets=mr_x["black_tickets"], occupied_nodes=new_detective_nodes
    )
    if not mr_x_legal_moves:
        session.status = "game_over"
        session.winner = "detectives"
        return {"status": session.status, "winner": session.winner, "round_number": state["round_number"]}

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
        session.status = "game_over"
        session.winner = "mr_x"
        return {"status": session.status, "winner": session.winner, "round_number": state["round_number"]}

    if state["round_number"] >= MAX_ROUND:
        session.status = "game_over"
        session.winner = "mr_x"
        return {"status": session.status, "winner": session.winner, "round_number": state["round_number"]}

    session.state = build_next_round_state(state)
    session.status = "awaiting_mr_x_move"
    return {"status": session.status, "winner": None, "round_number": session.state["round_number"]}
