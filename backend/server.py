import json

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from sse_starlette.sse import EventSourceResponse

from session import GAMES, create_game
from mrx_turn import get_mr_x_legal_moves, submit_mr_x_move, IllegalMoveError
from round_resolver import run_detective_loop, resolve_round
from serializers import serialize_public_state, serialize_loop_event
from game_master import map_data, node_positions


def _get_session_or_404(game_id: str):
    session = GAMES.get(game_id)
    if session is None:
        return None
    return session


async def create_game_route(request: Request) -> JSONResponse:
    session = create_game()
    return JSONResponse(serialize_public_state(session), status_code=201)


async def get_game_route(request: Request) -> JSONResponse:
    session = _get_session_or_404(request.path_params["game_id"])
    if session is None:
        return JSONResponse({"error": "Game not found."}, status_code=404)
    return JSONResponse(serialize_public_state(session))


async def map_route(request: Request) -> JSONResponse:
    session = _get_session_or_404(request.path_params["game_id"])
    if session is None:
        return JSONResponse({"error": "Game not found."}, status_code=404)
    # Board topology/positions are game-independent (immutable per .cursorrules), but this route
    # is scoped under /games/{game_id}/... for consistency with the rest of the API.
    return JSONResponse({"nodes": map_data, "positions": node_positions})


async def mrx_legal_moves_route(request: Request) -> JSONResponse:
    session = _get_session_or_404(request.path_params["game_id"])
    if session is None:
        return JSONResponse({"error": "Game not found."}, status_code=404)
    if session.status != "awaiting_mr_x_move":
        return JSONResponse({"error": f"Not Mr. X's turn (status={session.status})."}, status_code=409)

    # Optional hop-2 preview for a double-move: both params must be given together.
    from_node = request.query_params.get("from_node")
    ticket_type_spent = request.query_params.get("ticket_type_spent")
    after_hop = None
    if from_node is not None and ticket_type_spent is not None:
        after_hop = {"target_node": int(from_node), "ticket_type_spent": ticket_type_spent}

    try:
        return JSONResponse({"legal_moves": get_mr_x_legal_moves(session, after_hop=after_hop)})
    except IllegalMoveError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def mrx_move_route(request: Request) -> JSONResponse:
    session = _get_session_or_404(request.path_params["game_id"])
    if session is None:
        return JSONResponse({"error": "Game not found."}, status_code=404)

    move_request = await request.json()
    async with session.lock:
        try:
            submit_mr_x_move(session, move_request)
        except IllegalMoveError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    # Returns immediately once Mr. X's turn is validated+applied - does NOT block on the
    # detective loop. The client opens /round/stream next to watch that loop run.
    return JSONResponse(serialize_public_state(session))


async def round_stream_route(request: Request) -> EventSourceResponse:
    session = _get_session_or_404(request.path_params["game_id"])
    if session is None:
        return JSONResponse({"error": "Game not found."}, status_code=404)
    if session.status != "detective_loop_running":
        return JSONResponse(
            {"error": f"Detective loop is not running (status={session.status}). Submit Mr. X's move first."},
            status_code=409,
        )

    async def event_generator():
        async with session.lock:
            async for event in run_detective_loop(session):
                if event["type"] == "stage_started":
                    payload = {"type": "stage_started", "stage": event["stage"]}
                else:
                    payload = serialize_loop_event(event["node"], event["update"])
                yield {"event": payload["type"], "data": json.dumps(payload)}

            round_result = resolve_round(session)
            final_payload = {**round_result, "state": serialize_public_state(session)}
            yield {"event": "round_result", "data": json.dumps(final_payload)}

    return EventSourceResponse(event_generator())


app = Starlette(
    routes=[
        Route("/games", create_game_route, methods=["POST"]),
        Route("/games/{game_id}", get_game_route, methods=["GET"]),
        Route("/games/{game_id}/map", map_route, methods=["GET"]),
        Route("/games/{game_id}/mrx/legal-moves", mrx_legal_moves_route, methods=["GET"]),
        Route("/games/{game_id}/mrx/move", mrx_move_route, methods=["POST"]),
        Route("/games/{game_id}/round/stream", round_stream_route, methods=["GET"]),
    ],
    middleware=[
        # A browser frontend is a known near-term consumer of this API - open now rather than
        # revisiting CORS config once that phase starts.
        Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]),
    ],
)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
