"""
The Starlette HTTP + SSE API.

Deliberately Starlette rather than FastAPI (ADR-0002), so request-body validation is explicit
here via the models in requests.py rather than inferred from route signatures.
"""
import json
import logging
import os

from pydantic import ValidationError
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .game_master import map_data, node_positions
from .logging_config import configure_logging, game_log_context
from .mrx_turn import IllegalMoveError, get_mr_x_legal_moves, submit_mr_x_move
from .requests import MR_X_MOVE_ADAPTER, Hop2PreviewQuery, TurnAckRequest
from .round_resolver import resolve_round, run_detective_loop
from .serializers import serialize_loop_event, serialize_public_state
from .session import GAMES, create_game

logger = logging.getLogger(__name__)

# Origins allowed to call this API. Defaults to the Vite dev server only - the previous
# wildcard was opened before the frontend existed and was never narrowed once it did. Every
# endpoint here is unauthenticated and /round/stream triggers real, billable LLM calls, so a
# wildcard plus a 0.0.0.0 bind would let any page the user visits spend their API budget.
# Override with a comma-separated ALLOWED_ORIGINS for a non-default frontend origin.
DEFAULT_ALLOWED_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", ",".join(DEFAULT_ALLOWED_ORIGINS)).split(",")
    if origin.strip()
]


def _error(message: str, status: int) -> JSONResponse:
    """Every error response in this API has the same {"error": ...} shape the client parses."""
    return JSONResponse({"error": message}, status_code=status)


def _validation_error(exc: ValidationError) -> JSONResponse:
    """
    Renders a pydantic ValidationError as a single readable client-facing string.

    Kept terse on purpose: the client surfaces this verbatim in the UI, so it needs to read
    like a sentence, not like a serialized exception.
    """
    problems = "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or 'body'}: {err['msg']}"
        for err in exc.errors()
    )
    return _error(f"Malformed request - {problems}", 400)


def _get_session(request: Request):
    """Looks up the game named in the path, marking it as still in use if found."""
    session = GAMES.get(request.path_params["game_id"])
    if session is not None:
        session.touch()
    return session


async def create_game_route(request: Request) -> JSONResponse:
    session = create_game()
    with game_log_context(session.game_id):
        return JSONResponse(serialize_public_state(session), status_code=201)


async def get_game_route(request: Request) -> JSONResponse:
    session = _get_session(request)
    if session is None:
        return _error("Game not found.", 404)
    return JSONResponse(serialize_public_state(session))


async def map_route(request: Request) -> JSONResponse:
    session = _get_session(request)
    if session is None:
        return _error("Game not found.", 404)
    # Board topology/positions are game-independent (immutable per .cursorrules), but this route
    # is scoped under /games/{game_id}/... for consistency with the rest of the API.
    return JSONResponse({"nodes": map_data, "positions": node_positions})


async def mrx_legal_moves_route(request: Request) -> JSONResponse:
    session = _get_session(request)
    if session is None:
        return _error("Game not found.", 404)
    if session.status != "awaiting_mr_x_move":
        return _error(f"Not Mr. X's turn (status={session.status}).", 409)

    # Optional hop-2 preview for a double-move: both params must be given together.
    raw = {
        key: request.query_params[key]
        for key in ("from_node", "ticket_type_spent")
        if key in request.query_params
    }
    after_hop = None
    if raw:
        try:
            preview = Hop2PreviewQuery(**raw)
        except ValidationError as e:
            return _validation_error(e)
        after_hop = {
            "target_node": preview.from_node,
            "ticket_type_spent": preview.ticket_type_spent,
        }

    with game_log_context(session.game_id):
        try:
            return JSONResponse({"legal_moves": get_mr_x_legal_moves(session, after_hop=after_hop)})
        except IllegalMoveError as e:
            return _error(str(e), 400)


async def mrx_move_route(request: Request) -> JSONResponse:
    session = _get_session(request)
    if session is None:
        return _error("Game not found.", 404)

    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error("Malformed request - body is not valid JSON.", 400)

    try:
        move = MR_X_MOVE_ADAPTER.validate_python(body)
    except ValidationError as e:
        return _validation_error(e)

    with game_log_context(session.game_id):
        async with session.lock:
            try:
                submit_mr_x_move(session, move.to_move_request())
            except IllegalMoveError as e:
                return _error(str(e), 400)

        # Returns immediately once Mr. X's turn is validated+applied - does NOT block on the
        # detective loop. The client opens /round/stream next to watch that loop run.
        return JSONResponse(serialize_public_state(session))


async def turn_ack_route(request: Request) -> JSONResponse:
    """
    The client reporting that a detective's pawn has finished moving (ADR-0010), which is what
    releases the next detective's turn.

    Deliberately does NOT take `session.lock`: the lock is held for the entire duration of the
    detective loop by round_stream_route, and this request exists precisely to unblock that
    loop from the inside. Waiting on the lock would deadlock the round against itself.

    Always returns 200 with whether the ack was actually applied. A mismatched or late ack is a
    normal race (the loop may already have timed out and moved on), not a client error - there
    is nothing useful for the client to do about it, and nothing it should retry.
    """
    session = _get_session(request)
    if session is None:
        return _error("Game not found.", 404)

    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error("Malformed request - body is not valid JSON.", 400)

    try:
        ack = TurnAckRequest(**body) if isinstance(body, dict) else TurnAckRequest()
    except ValidationError as e:
        return _validation_error(e)
    except TypeError:
        return _error("Malformed request - body must be a JSON object.", 400)

    applied = session.acknowledge_pawn_settled(ack.round_number, ack.detective)
    return JSONResponse({"applied": applied})


async def round_stream_route(request: Request):
    session = _get_session(request)
    if session is None:
        return _error("Game not found.", 404)

    # The status check below is repeated INSIDE the lock further down, and that is the one
    # that actually decides. This early check exists only to reject an obviously-wrong request
    # (Mr. X hasn't moved yet) without opening a stream, and it is NOT sufficient on its own:
    # two concurrent requests both pass it, and the second would then run a whole second
    # detective loop once the first released the lock - against the NEXT round's state, with
    # Mr. X never having moved. React StrictMode's dev-mode double-invoke of effects makes
    # that a routine occurrence, not a theoretical one, and each spurious loop is ~15 billable
    # LLM calls. See docs/issues/known_issues.md ISSUE-027.
    if session.status != "detective_loop_running":
        return _error(
            f"Detective loop is not running (status={session.status}). Submit Mr. X's move first.",
            409,
        )

    async def event_generator():
        with game_log_context(session.game_id):
            async with session.lock:
                # Re-check now that we hold the lock. If another subscriber already ran this
                # round while we were queued, the round is over - say so and stop, rather than
                # re-running it.
                if session.status != "detective_loop_running":
                    yield {
                        "event": "round_already_resolved",
                        "data": json.dumps({
                            "type": "round_already_resolved",
                            "state": serialize_public_state(session),
                        }),
                    }
                    return

                async for event in run_detective_loop(session):
                    if event["type"] == "turn_event":
                        # One LLM call's worth of a detective's turn, streamed the moment it
                        # completed. agents.py names the event ("turn_started",
                        # "turn_proposal", "turn_response", "turn_decision"); the SSE event
                        # name is taken from that so the client can register one listener per
                        # kind, exactly as it does for the node-level events below.
                        turn_event = dict(event["payload"])
                        payload = {"type": turn_event.pop("event"), **turn_event}
                    else:
                        payload = serialize_loop_event(event["node"], event["update"])
                    yield {"event": payload["type"], "data": json.dumps(payload)}

                round_result = resolve_round(session)
                # "type" is included for consistency with every other event's payload (and
                # with frontend/src/types.ts's RoundResultEvent, which declares it) - this
                # one payload was assembled by spreading RoundResult and never carried it.
                final_payload = {
                    "type": "round_result",
                    **round_result,
                    "state": serialize_public_state(session),
                }
                yield {"event": "round_result", "data": json.dumps(final_payload)}

    return EventSourceResponse(event_generator())


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Last-resort handler so an unexpected failure returns structured JSON the client can parse,
    rather than Starlette's HTML stack-trace page that api/client.ts cannot read an error out of.
    """
    logger.exception("Unhandled error serving %s %s", request.method, request.url.path)
    return _error("Internal server error.", 500)


app = Starlette(
    routes=[
        Route("/games", create_game_route, methods=["POST"]),
        Route("/games/{game_id}", get_game_route, methods=["GET"]),
        Route("/games/{game_id}/map", map_route, methods=["GET"]),
        Route("/games/{game_id}/mrx/legal-moves", mrx_legal_moves_route, methods=["GET"]),
        Route("/games/{game_id}/mrx/move", mrx_move_route, methods=["POST"]),
        Route("/games/{game_id}/round/stream", round_stream_route, methods=["GET"]),
        Route("/games/{game_id}/turn-ack", turn_ack_route, methods=["POST"]),
    ],
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=ALLOWED_ORIGINS,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
        ),
    ],
    exception_handlers={Exception: unhandled_exception_handler},
)


def main() -> None:
    """
    Development entrypoint: `python -m scotland_yard.server`.

    Binds to 127.0.0.1, not 0.0.0.0. Every endpoint here is unauthenticated and the stream
    endpoint spends real money per request, so exposing it on every interface by default was
    a larger grant than this app's threat model warrants. Set HOST to override deliberately.
    """
    import uvicorn

    configure_logging()
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    logger.info("Starting Scotland Yard API on http://%s:%d (CORS: %s)",
                host, port, ", ".join(ALLOWED_ORIGINS))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
