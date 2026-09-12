from session import GameSession

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

    mr_x.current_node IS included here (as of the board's always-visible Mr. X pawn feature) -
    this is safe specifically because this project has exactly one human-facing client, and that
    client is played BY Mr. X: the detectives are backend-only LangGraph/LLM agents (agents.py)
    that never see this frontend or any HTTP response at all, only prompt text built server-side.
    "Never leak to a client" was never actually about hiding this from the OPPONENT (impossible -
    the opponent has no client), it was about not hiding it from the one client that exists
    unnecessarily - now revisited so the Mr. X player can always see their own pawn on their own
    board. If a detective-facing client (e.g. a spectator/detective-controlled mode) is ever
    added, current_node must be excluded from whatever serialization THAT client receives.
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
    into a JSON-safe event for the SSE stream. Only "debate" updates need real translation - its
    `messages` list holds LangChain message objects, not plain data; every other node's update
    is already JSON-safe as-is.
    """
    if node_name == "propose":
        return {"type": "proposal", "proposed_strategies": update.get("proposed_strategies", {})}
    if node_name == "debate":
        messages = update.get("messages", [])
        return {"type": "debate", "transcript": messages[-1].content if messages else ""}
    if node_name == "vote":
        return {
            "type": "vote_tally",
            "locked_moves": update.get("locked_moves", {}),
            "loop_number": update.get("debate_loop_count"),
        }
    if node_name == "finalize":
        # final_move_details (not the plain final_moves int map) is what the client actually
        # renders - {det_id: {"from_node", "to_node", "transport"}} - see
        # graph.py:finalize_round_node for why it's computed there rather than here.
        return {"type": "round_finalized", "final_moves": update.get("final_move_details", {})}
    return {"type": "unknown", "node": node_name}
