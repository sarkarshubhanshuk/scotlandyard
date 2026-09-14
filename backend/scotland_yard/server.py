"""
The Starlette HTTP + SSE API.

Deliberately Starlette rather than FastAPI (ADR-0002), so request-body validation is explicit
here via the models in requests.py rather than inferred from route signatures.
"""
import hmac
import json
import logging
import os
import secrets
from pathlib import Path

from pydantic import ValidationError
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .game_master import BASE_DIR, map_data, node_positions
from .limits import (
    check_new_game,
    check_round_budget,
    client_ip,
    record_new_game,
)
from .llm_client import api_key_configured
from .logging_config import configure_logging, game_log_context
from .mrx_turn import IllegalMoveError, get_mr_x_legal_moves, submit_mr_x_move
from .requests import MR_X_MOVE_ADAPTER, Hop2PreviewQuery, TurnAckRequest
from .round_resolver import resolve_round, run_detective_loop
from .serializers import serialize_loop_event, serialize_public_state
from .session import GAMES, SESSION_TTL_SECONDS, active_game_count, create_game

logger = logging.getLogger(__name__)

# Configured HERE, at import, rather than only in main(). `app` is a module-level object, so
# every ASGI launcher - `uvicorn scotland_yard.server:app --reload` (which README.md documents
# for development), gunicorn, a platform's own runner - builds it WITHOUT ever calling main().
# Root logging was then left unconfigured, falling back to logging.lastResort: every logger.info
# in the round loop vanished, and the [VALIDATION] warnings that survived printed through a
# handler with no [game_id] field, which is the one thing logging_config exists to attach.
# Doing it at import also captures the module-level lines below (e.g. the "no SPA build" notice),
# which an on_startup hook would run too late to see. configure_logging clears root handlers
# first, so calling it once here is idempotent and safe to repeat.
configure_logging()

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

# The built SPA, when one has been built into the repo layout (the deployed image puts it here;
# a dev checkout only has it after `npm run build`). Serving it from this same app is what makes
# the frontend and the API ONE origin, which in turn is what lets the ownership cookie ride along
# on the EventSource round stream - EventSource cannot set headers, so a cookie is the only
# practical way to authenticate it. In dev the SPA is served by Vite instead and this is absent,
# which is why every route below is conditional rather than assumed.
FRONTEND_DIST = Path(os.getenv("FRONTEND_DIST", BASE_DIR / "frontend" / "dist"))
SPA_INDEX = FRONTEND_DIST / "index.html"

# Game ownership, in the absence of any accounts: creating a game mints an opaque token, returns
# it in an HttpOnly cookie, and records it on the session. Every game-scoped route then requires
# the cookie to match. A shared URL therefore conveys the game id but NOT the cookie, so the
# recipient cannot see or act on someone else's game.
#
# The token is the secret itself rather than a signed claim - it is 32 random bytes, so forging
# one is as hard as guessing it, and signing would add a key to manage for no extra strength.
#
# Secure is on by default (the deployment is HTTPS, and browsers treat http://localhost as a
# secure context anyway); COOKIE_SECURE=false exists only for a plain-HTTP host on some other
# name. SameSite=Lax means a cross-site POST cannot carry it, so another page cannot move your
# pawns on your behalf.
PLAYER_COOKIE = "sy_player"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").strip().lower() not in {"false", "0", "no"}


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


def _load_owned_game(request: Request):
    """
    The game named in the path, but only for the browser that created it.

    Returns `(session, None)` or `(None, error_response)`. An unknown id 404s BEFORE the
    ownership check, so a stale or mistyped link reads as "no such game" rather than implying
    one exists; a real game belonging to someone else 403s with a message that tells the reader
    what to do instead, since following a friend's link is the expected way to hit this.
    """
    session = _get_session(request)
    if session is None:
        return None, _error("Game not found.", 404)
    presented = request.cookies.get(PLAYER_COOKIE) or ""
    if not session.owner_token or not hmac.compare_digest(presented, session.owner_token):
        return None, _error(
            "This game belongs to another player. Start your own from the main menu.", 403)
    return session, None


async def create_game_route(request: Request) -> JSONResponse:
    """
    Starts a game and binds it to the calling browser.

    A browser that already has a token keeps it, so one person can hold several games at once
    (an abandoned tab, a fresh start) without the newest invalidating the others. Re-setting the
    cookie on every creation also refreshes its Max-Age, so an active player's token does not
    expire out from under them mid-session.
    """
    ip = client_ip(request)
    # active_game_count(), never len(GAMES): it sweeps TTL-expired games before counting. The
    # cap is meant to bound how many games are RUNNING, and eviction used to happen only inside
    # create_game - below this check - so a store full of abandoned games refused every new game
    # and never swept the games doing the refusing. See session.active_game_count.
    refusal = check_new_game(ip, active_games=active_game_count())
    if refusal is not None:
        logger.info("Refused a new game for %s: %s", ip, refusal)
        return _error(refusal, 429)

    token = request.cookies.get(PLAYER_COOKIE) or secrets.token_urlsafe(32)
    session = create_game()
    record_new_game(ip)
    session.owner_token = token
    with game_log_context(session.game_id):
        response = JSONResponse(serialize_public_state(session), status_code=201)
        response.set_cookie(
            PLAYER_COOKIE,
            token,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            secure=COOKIE_SECURE,
            samesite="lax",
            path="/",
        )
        return response


async def get_game_route(request: Request) -> JSONResponse:
    session, denied = _load_owned_game(request)
    if denied is not None:
        return denied
    return JSONResponse(serialize_public_state(session))


async def map_route(request: Request) -> JSONResponse:
    session, denied = _load_owned_game(request)
    if denied is not None:
        return denied
    # Board topology/positions are game-independent (immutable per .cursorrules), but this route
    # is scoped under /games/{game_id}/... for consistency with the rest of the API.
    return JSONResponse({"nodes": map_data, "positions": node_positions})


async def mrx_legal_moves_route(request: Request) -> JSONResponse:
    session, denied = _load_owned_game(request)
    if denied is not None:
        return denied
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
    session, denied = _load_owned_game(request)
    if denied is not None:
        return denied

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
    session, denied = _load_owned_game(request)
    if denied is not None:
        return denied

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
    session, denied = _load_owned_game(request)
    if denied is not None:
        return denied

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

    # Checked HERE, before the stream opens, rather than per call inside the round: refusing
    # mid-round would leave a game with half its detectives moved and no way to finish.
    refusal = check_round_budget()
    if refusal is not None:
        logger.warning("Refused a round for game %s: %s", session.game_id, refusal)
        return _error(refusal, 503)

    # Checked before the stream opens, for the same reason the budget is: once the SSE response
    # has started there is no status code left to send, and a missing key would otherwise surface
    # to the player as nothing but a dropped connection. This is a deployment misconfiguration
    # (the key is supplied as a platform secret), so it is logged at ERROR and named plainly.
    if not api_key_configured():
        logger.error(
            "Refused a round for game %s: OPENROUTER_API_KEY is not set on this deployment.",
            session.game_id)
        return _error(
            "The detectives are unavailable - this deployment has no LLM API key configured.",
            503)

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

                # Every failure inside the round has to become an EVENT, because by the time
                # anything here runs the response has already started and no status code can be
                # sent any more. Without this, an exception escaping the loop just ended the
                # stream: the browser reported a generic "lost connection", and the player had
                # no way to tell a network blip apart from a broken deployment.
                #
                # asyncio.CancelledError inherits from BaseException, so a client that simply
                # disconnects is NOT caught here - it stays an ordinary cancellation rather than
                # being reported as a round failure.
                try:
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
                except Exception:
                    # session.status is deliberately LEFT at "detective_loop_running", which is
                    # what makes the client's retry correct rather than merely permitted:
                    # run_detective_loop only assigns session.state once the graph has run to
                    # completion, so a mid-round failure leaves the session exactly as the round
                    # started. Retrying replays the whole round from that untouched state; it
                    # cannot resume into a half-moved board, because no half-moved board was ever
                    # committed.
                    logger.exception("Round failed for game %s", session.game_id)
                    yield {
                        "event": "round_error",
                        "data": json.dumps({
                            "type": "round_error",
                            # Deliberately generic: this endpoint is public and unauthenticated,
                            # and the exception text is in the server log for whoever can read it.
                            "message": ("The detectives hit an unexpected problem this round. "
                                        "The round was not applied - you can try again."),
                            "state": serialize_public_state(session),
                        }),
                    }

    return EventSourceResponse(event_generator())


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Last-resort handler so an unexpected failure returns structured JSON the client can parse,
    rather than Starlette's HTML stack-trace page that api/client.ts cannot read an error out of.
    """
    logger.exception("Unhandled error serving %s %s", request.method, request.url.path)
    return _error("Internal server error.", 500)


async def health_route(request: Request) -> PlainTextResponse:
    """
    A cheap liveness probe. Exists because every other path here is game-scoped, so a platform
    health check against "/" on a deployment without the SPA would see a 404 and call the service
    unhealthy.
    """
    return PlainTextResponse("ok")


async def spa_route(request: Request) -> FileResponse:
    """
    Hands index.html to any non-API path so the client router owns deep links.

    /game/<id> is a React Router route, not a file on disk: a plain static mount would 404 it,
    which is exactly what a player following a shared link (or refreshing mid-game) would hit.
    Registered LAST, so it can never shadow a real API route above it.
    """
    return FileResponse(SPA_INDEX)


routes = [
    Route("/health", health_route, methods=["GET"]),
    Route("/games", create_game_route, methods=["POST"]),
    Route("/games/{game_id}", get_game_route, methods=["GET"]),
    Route("/games/{game_id}/map", map_route, methods=["GET"]),
    Route("/games/{game_id}/mrx/legal-moves", mrx_legal_moves_route, methods=["GET"]),
    Route("/games/{game_id}/mrx/move", mrx_move_route, methods=["POST"]),
    Route("/games/{game_id}/round/stream", round_stream_route, methods=["GET"]),
    Route("/games/{game_id}/turn-ack", turn_ack_route, methods=["POST"]),
]

if SPA_INDEX.is_file():
    routes += [
        # Hashed build output: safe to cache hard, and never collides with an API path.
        Mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets")),
        # The art sync-assets.mjs copies out of data/ (board, pawns, tickets, how-to, logo).
        Mount("/board", StaticFiles(directory=FRONTEND_DIST / "board")),
        Mount("/pawn", StaticFiles(directory=FRONTEND_DIST / "pawn")),
        Mount("/tickets", StaticFiles(directory=FRONTEND_DIST / "tickets")),
        Mount("/howto", StaticFiles(directory=FRONTEND_DIST / "howto")),
        Route("/game_logo.jpg", lambda request: FileResponse(FRONTEND_DIST / "game_logo.jpg")),
        Route("/", spa_route, methods=["GET"]),
        Route("/{path:path}", spa_route, methods=["GET"]),
    ]
else:
    logger.info("No SPA build at %s - serving the API only (Vite serves the frontend in dev).",
                FRONTEND_DIST)

app = Starlette(
    routes=routes,
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=ALLOWED_ORIGINS,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
            # Needed for the ownership cookie to survive the cross-origin dev setup (:5173 SPA
            # calling the :8000 API). Safe only because allow_origins is an explicit list and
            # never "*" - the two are mutually exclusive in the CORS spec for exactly this
            # reason. Deployed, the SPA is same-origin and this has nothing to do.
            allow_credentials=True,
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

    # Logging is already configured at import (see the configure_logging call at the top of this
    # module), which covers this entrypoint and every ASGI launcher alike.
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    logger.info("Starting Scotland Yard API on http://%s:%d (CORS: %s)",
                host, port, ", ".join(ALLOWED_ORIGINS))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
