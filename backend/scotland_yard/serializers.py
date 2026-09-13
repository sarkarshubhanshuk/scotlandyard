from typing import Optional

from .session import GameSession

# Ticket counts are always visible to everyone (rules.md: "Inventory Visibility"). current_node
# (Mr. X's real position) IS included here, deliberately - see serialize_public_state's own
# docstring for why this is no longer the "never leaves the server" field it used to be.
_MR_X_PUBLIC_FIELDS = (
    "current_node", "taxi_tickets", "bus_tickets", "metro_tickets", "black_tickets",
    "double_tickets", "last_known_node", "last_known_round", "transport_history",
)


def serialize_public_state(session: GameSession) -> dict:
    """
    The single choke point every route/streamed event uses to build an outward-facing payload.

    mr_x.current_node IS included here (as of the board's always-visible Mr. X pawn feature).
    That is safe because this project has exactly one human-facing client and it is played BY
    Mr. X - the detectives are backend-only LangGraph agents that never see any HTTP response,
    only prompt text built server-side. Full reasoning in ADR-0007.

    THE CONDITION THAT MAKES THIS SAFE: if a detective-facing client is ever added (a spectator
    mode, or detective-controlled play), current_node must be excluded from whatever
    serialization THAT client receives. This warning lives here, at the point of risk, rather
    than only in the ADR.
    """
    state = session.state
    mr_x_public = {field: state["mr_x"][field] for field in _MR_X_PUBLIC_FIELDS}

    return {
        "game_id": session.game_id,
        "status": session.status,
        "winner": session.winner,
        "round_number": state["round_number"],
        "mr_x": mr_x_public,
        "detectives": {
            det_id: dict(detective) for det_id, detective in state["detectives"].items()
        },
    }


def serialize_loop_event(node_name: str, update: dict) -> dict:
    """
    Translates one raw LangGraph "updates"-mode chunk (from round_resolver.run_detective_loop)
    into a JSON-safe event for the SSE stream.

    Only "finalize" is translated here. Everything a turn produces reaches the client through
    agents.py's per-call custom stream events instead (see run_detective_loop): a node-level
    update only arrives once the whole six-call turn has finished, which is exactly the
    all-at-once delivery turn-wise play exists to get rid of. A "turn" update is therefore
    swallowed rather than re-sent - its content has already been streamed, call by call.
    """
    if node_name == "finalize":
        # final_move_details (not the plain final_moves int map) is what the client actually
        # renders - {det_id: {"from_node", "to_node", "transport"}} - see
        # graph.py:finalize_round_node for why it's computed there rather than here.
        return {"type": "round_finalized", "final_moves": update.get("final_move_details", {})}
    return {"type": "turn_finished", "detective": _committed_detective(update)}


def _committed_detective(update: dict) -> Optional[str]:
    """Which detective the just-finished turn belonged to, from its committed_moves update."""
    committed = update.get("committed_moves") or {}
    return next(iter(committed), None)
