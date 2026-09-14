"""
Tests for the Starlette API layer, driven in-process via starlette.testclient.TestClient -
no real network port needed.

Uses `with TestClient(...) as client:` (not a bare `TestClient(...)`) so every request in a
test shares ONE persistent event loop/portal, rather than each call spinning up its own.

The full round/stream event sequence requires a live OPENROUTER_API_KEY and real LLM calls,
so it lives in test_full_round_e2e_llm.py behind the `llm` marker. What IS covered here is
every path that rejects a request before any model is involved - which is where the
structural-validation bugs lived.
"""
import logging

import pytest
from starlette.testclient import TestClient

from scotland_yard import limits, server
from scotland_yard import session as session_module
from scotland_yard.logging_config import GameIdFilter, game_log_context
from scotland_yard.rules_constants import DETECTIVE_IDS


@pytest.fixture
def client():
    # https, not http: the game-ownership cookie is marked Secure, and a client on a plain-http
    # origin that is not localhost would silently drop it - every game-scoped request would then
    # arrive anonymous and 403, testing the rejection path instead of the real one.
    with TestClient(server.app, base_url="https://testserver") as c:
        yield c


@pytest.fixture
def game(client):
    """A created game, returned as (game_id, initial public state)."""
    response = client.post("/games")
    assert response.status_code == 201, response.text
    body = response.json()
    return body["game_id"], body


class TestGameLifecycle:
    def test_create_and_fetch_expose_mr_x_current_node(self, client, game):
        # current_node is deliberately public (see serializers.py): the only human-facing
        # client is played BY Mr. X, and detectives are backend-only agents with no client.
        game_id, body = game
        assert isinstance(body["mr_x"]["current_node"], int)
        assert body["status"] == "awaiting_mr_x_move"
        assert set(body["detectives"]) == set(DETECTIVE_IDS)

        again = client.get(f"/games/{game_id}")
        assert again.status_code == 200
        assert again.json() == body

    def test_unknown_game_is_404_on_every_game_scoped_route(self, client):
        for path in ("", "/map", "/mrx/legal-moves", "/round/stream"):
            response = client.get(f"/games/does-not-exist{path}")
            assert response.status_code == 404, path
            assert response.json()["error"] == "Game not found."

    def test_map_route_serves_the_whole_board(self, client, game):
        game_id, _ = game
        body = client.get(f"/games/{game_id}/map").json()
        assert len(body["nodes"]) == 199
        assert len(body["positions"]) == 199
        assert "x" in body["positions"]["1"] and "y" in body["positions"]["1"]


class TestMrXMoveFlow:
    def test_a_legal_move_advances_the_game_into_the_detective_loop(self, client, game):
        game_id, _ = game
        legal = client.get(f"/games/{game_id}/mrx/legal-moves").json()["legal_moves"]
        assert legal
        move = legal[0]

        response = client.post(f"/games/{game_id}/mrx/move", json={
            "move_type": "single",
            "target_node": move["target_node"],
            "ticket_type_spent": move["ticket_options"][0],
        })
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "detective_loop_running"
        assert body["mr_x"]["current_node"] == move["target_node"]

    def test_moving_twice_in_one_round_is_rejected(self, client, game):
        game_id, _ = game
        legal = client.get(f"/games/{game_id}/mrx/legal-moves").json()["legal_moves"]
        move = legal[0]
        client.post(f"/games/{game_id}/mrx/move", json={
            "move_type": "single", "target_node": move["target_node"],
            "ticket_type_spent": move["ticket_options"][0],
        })
        # Legal-moves is now the wrong turn...
        assert client.get(f"/games/{game_id}/mrx/legal-moves").status_code == 409
        # ...and so is another move.
        second = client.post(f"/games/{game_id}/mrx/move", json={
            "move_type": "single", "target_node": move["target_node"],
            "ticket_type_spent": move["ticket_options"][0],
        })
        assert second.status_code == 400

    def test_an_illegal_target_is_rejected_with_a_readable_message(self, client, game):
        game_id, _ = game
        response = client.post(f"/games/{game_id}/mrx/move", json={
            "move_type": "single", "target_node": 199, "ticket_type_spent": "taxi",
        })
        assert response.status_code == 400
        assert "error" in response.json()

    def test_round_stream_is_rejected_before_mr_x_has_moved(self, client, game):
        game_id, _ = game
        response = client.get(f"/games/{game_id}/round/stream")
        assert response.status_code == 409
        assert "Submit Mr. X's move first" in response.json()["error"]


class TestMalformedRequests:
    """
    Structurally malformed requests used to raise KeyError/ValueError/JSONDecodeError out of
    the route with no handler, producing an opaque 500 with an HTML stack trace the frontend's
    ApiError could not read a message out of. Every one of these must now be a 400.
    """

    @pytest.mark.parametrize("body,reason", [
        ({}, "no move_type at all"),
        ({"move_type": "single"}, "single move missing target_node and ticket"),
        ({"move_type": "single", "target_node": 5}, "single move missing ticket"),
        ({"move_type": "single", "ticket_type_spent": "taxi"}, "single move missing target"),
        ({"move_type": "teleport", "target_node": 5, "ticket_type_spent": "taxi"}, "unknown move_type"),
        ({"move_type": "single", "target_node": "five", "ticket_type_spent": "taxi"}, "non-numeric node"),
        ({"move_type": "single", "target_node": 5, "ticket_type_spent": "rocket"}, "invalid ticket type"),
        ({"move_type": "single", "target_node": 5, "ticket_type_spent": "double"}, "double is a log sentinel, not a ticket"),
        ({"move_type": "double", "hop1": {"target_node": 5, "ticket_type_spent": "taxi"}}, "double missing hop2"),
        ({"move_type": "double", "hop1": {}, "hop2": {}}, "double with empty hops"),
    ])
    def test_malformed_move_bodies_return_400(self, client, game, body, reason):
        game_id, _ = game
        response = client.post(f"/games/{game_id}/mrx/move", json=body)
        assert response.status_code == 400, f"{reason}: got {response.status_code}"
        assert response.json()["error"].startswith("Malformed request"), reason

    def test_a_non_json_body_returns_400_not_500(self, client, game):
        game_id, _ = game
        response = client.post(
            f"/games/{game_id}/mrx/move",
            content=b"this is not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert "not valid JSON" in response.json()["error"]

    @pytest.mark.parametrize("query", [
        "?from_node=abc&ticket_type_spent=taxi",
        "?from_node=5&ticket_type_spent=rocket",
        "?from_node=5",                      # incomplete pair
        "?ticket_type_spent=taxi",           # incomplete pair
        "?from_node=99999&ticket_type_spent=taxi",
    ])
    def test_malformed_hop2_preview_params_return_400(self, client, game, query):
        game_id, _ = game
        response = client.get(f"/games/{game_id}/mrx/legal-moves{query}")
        assert response.status_code == 400, response.text
        assert "error" in response.json()


class TestDoubleMovePreview:
    def test_hop2_preview_requires_a_double_ticket(self, client, game):
        game_id, _ = game
        legal = client.get(f"/games/{game_id}/mrx/legal-moves").json()["legal_moves"]
        hop1 = legal[0]

        server.GAMES[game_id].state["mr_x"]["double_tickets"] = 0
        response = client.get(
            f"/games/{game_id}/mrx/legal-moves"
            f"?from_node={hop1['target_node']}&ticket_type_spent={hop1['ticket_options'][0]}"
        )
        assert response.status_code == 400
        assert "double-move" in response.json()["error"].lower()

    def test_hop2_preview_returns_options_from_the_intermediate_node(self, client, game):
        game_id, _ = game
        legal = client.get(f"/games/{game_id}/mrx/legal-moves").json()["legal_moves"]
        hop1 = legal[0]

        response = client.get(
            f"/games/{game_id}/mrx/legal-moves"
            f"?from_node={hop1['target_node']}&ticket_type_spent={hop1['ticket_options'][0]}"
        )
        assert response.status_code == 200, response.text
        options = response.json()["legal_moves"]
        assert isinstance(options, list)
        assert all("target_node" in m and "ticket_options" in m for m in options)


class TestCorsPolicy:
    def test_the_configured_frontend_origin_is_allowed(self, client, game):
        game_id, _ = game
        response = client.get(
            f"/games/{game_id}", headers={"Origin": "http://localhost:5173"}
        )
        assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"

    def test_an_arbitrary_origin_is_not_granted_access(self, client, game):
        # The previous wildcard policy would have echoed "*" here.
        game_id, _ = game
        response = client.get(
            f"/games/{game_id}", headers={"Origin": "http://evil.example.com"}
        )
        assert response.headers.get("access-control-allow-origin") not in ("*", "http://evil.example.com")


class TestTurnAck:
    """
    POST /games/{id}/turn-ack - the client reporting that a detective's pawn finished animating
    (ADR-0010). Malformed acks are rejected at the boundary like every other request body; a
    well-formed one that simply does not match what the turn loop is waiting on is NOT an error,
    since that is an ordinary race the client can do nothing about.
    """

    def test_an_unmatched_ack_is_accepted_but_reports_it_did_nothing(self, client, game):
        game_id, _ = game
        response = client.post(
            f"/games/{game_id}/turn-ack",
            json={"round_number": 1, "detective": "agent_red"},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"applied": False}

    def test_a_matching_ack_reports_that_it_applied(self, client, game):
        game_id, _ = game
        server.GAMES[game_id].expect_pawn_ack(1, "agent_blue")

        response = client.post(
            f"/games/{game_id}/turn-ack",
            json={"round_number": 1, "detective": "agent_blue"},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"applied": True}

    def test_an_unknown_game_is_404(self, client):
        response = client.post(
            "/games/does-not-exist/turn-ack",
            json={"round_number": 1, "detective": "agent_red"},
        )
        assert response.status_code == 404

    @pytest.mark.parametrize(
        "body",
        [
            {"round_number": 1},                                   # missing detective
            {"detective": "agent_red"},                            # missing round_number
            {"round_number": 1, "detective": "agent_pink"},        # not a real detective
            {"round_number": 0, "detective": "agent_red"},         # rounds start at 1
            {"round_number": 99, "detective": "agent_red"},        # past MAX_ROUND
            {"round_number": "one", "detective": "agent_red"},     # wrong type
            {"round_number": 1, "detective": "agent_red", "x": 1},  # extra field
        ],
    )
    def test_malformed_acks_are_rejected_with_a_field_level_message(self, client, game, body):
        game_id, _ = game
        response = client.post(f"/games/{game_id}/turn-ack", json=body)
        assert response.status_code == 400, response.text
        assert "Malformed request" in response.json()["error"]


class TestGameOwnership:
    """
    The access control that replaces having no accounts at all: a game belongs to the browser
    that created it, proved by an opaque token in an HttpOnly cookie. This is what makes a
    shared URL useless to the recipient - they get the game id, but not the cookie.
    """

    def test_creating_a_game_sets_an_httponly_cookie(self, client):
        response = client.post("/games")
        assert response.status_code == 201
        cookie = response.headers["set-cookie"]
        assert server.PLAYER_COOKIE in cookie
        assert "HttpOnly" in cookie, "the token must not be readable from page JavaScript"
        assert "SameSite=lax" in cookie.replace("samesite", "SameSite")

    def test_someone_following_a_shared_link_is_refused(self, client, game):
        game_id, _ = game
        # A second browser: same URL, no cookie. This is exactly the link-sharing case.
        with TestClient(server.app, base_url="https://testserver") as stranger:
            for method, path in [
                ("get", f"/games/{game_id}"),
                ("get", f"/games/{game_id}/map"),
                ("get", f"/games/{game_id}/mrx/legal-moves"),
                ("get", f"/games/{game_id}/round/stream"),
            ]:
                response = getattr(stranger, method)(path)
                assert response.status_code == 403, f"{path} leaked to a non-owner"
            assert stranger.post(
                f"/games/{game_id}/mrx/move",
                json={"move_type": "single", "target_node": 1, "ticket_type_spent": "taxi"},
            ).status_code == 403
            assert stranger.post(
                f"/games/{game_id}/turn-ack",
                json={"round_number": 1, "detective": "agent_red"},
            ).status_code == 403

    def test_a_forged_token_is_refused(self, client, game):
        game_id, _ = game
        with TestClient(server.app, base_url="https://testserver") as forger:
            forger.cookies.set(server.PLAYER_COOKIE, "not-the-real-token")
            assert forger.get(f"/games/{game_id}").status_code == 403

    def test_the_owner_keeps_access_across_requests(self, client, game):
        game_id, _ = game
        assert client.get(f"/games/{game_id}").status_code == 200
        assert client.get(f"/games/{game_id}/map").status_code == 200

    def test_one_browser_can_hold_several_games_at_once(self, client):
        first = client.post("/games").json()["game_id"]
        second = client.post("/games").json()["game_id"]
        assert first != second
        # The second creation reuses the existing token rather than rotating it, so the first
        # game does not become inaccessible the moment a player starts another.
        assert client.get(f"/games/{first}").status_code == 200
        assert client.get(f"/games/{second}").status_code == 200

    def test_an_unknown_game_still_404s_for_a_stranger(self, client):
        # 404 is checked BEFORE ownership, so a dead link reads as "no such game" rather than
        # hinting that one exists and is simply not yours.
        with TestClient(server.app, base_url="https://testserver") as stranger:
            assert stranger.get("/games/does-not-exist").status_code == 404


class TestSpaAndHealth:
    def test_health_is_ok(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.text == "ok"


class TestLoggingConfiguration:
    """
    Regression: logging was configured only inside server.main().

    `app` is a module-level object, so every ASGI launcher - including the
    `uvicorn scotland_yard.server:app --reload` that README.md documents for development -
    builds it without ever calling main(). Root logging was left unconfigured, so the round
    loop's whole INFO narrative vanished and the [VALIDATION] warnings that did survive came
    out through logging.lastResort, with no [game_id] attached. Importing the module has to be
    enough.
    """

    def test_importing_the_app_configures_root_logging(self):
        root = logging.getLogger()
        assert root.handlers, "importing scotland_yard.server must configure root logging"
        assert any(
            isinstance(f, GameIdFilter) for handler in root.handlers for f in handler.filters
        ), "the game-id filter must be attached, or every log line loses its game attribution"

    def test_a_log_record_renders_with_a_game_id(self, caplog):
        """The formatter references %(game_id)s, so a record without it would raise on format."""
        handler = next(h for h in logging.getLogger().handlers if h.filters)
        record = logging.LogRecord(
            "scotland_yard.test", logging.WARNING, __file__, 1, "[VALIDATION] x", None, None
        )
        for f in handler.filters:
            f.filter(record)
        assert handler.format(record).count("[-]") == 1

        with game_log_context("game-123"):
            record = logging.LogRecord(
                "scotland_yard.test", logging.WARNING, __file__, 1, "[VALIDATION] x", None, None
            )
            for f in handler.filters:
                f.filter(record)
            assert "[game-123]" in handler.format(record)


class TestDeploymentLimits:
    """
    The caps that make a public, loginless link survivable: a completed game is roughly 720 LLM
    calls, so without these "very low usage" would be a hope rather than a property. None of
    them replaces a hard credit limit on the OpenRouter key - they exist so the service refuses
    politely long before the key starts erroring mid-game.
    """

    def test_a_burst_of_new_games_from_one_client_is_throttled(self, client, monkeypatch):
        monkeypatch.setattr(limits, "MAX_GAMES_PER_IP_PER_HOUR", 2)
        assert client.post("/games").status_code == 201
        assert client.post("/games").status_code == 201

        refused = client.post("/games")
        assert refused.status_code == 429
        assert "started a lot of games" in refused.json()["error"]

    def test_a_refused_creation_does_not_count_against_the_caller(self, client, monkeypatch):
        monkeypatch.setattr(limits, "MAX_ACTIVE_GAMES", 0)
        assert client.post("/games").status_code == 429
        # The concurrent-game wall, not the per-IP one - so once there is room again the same
        # client is served immediately rather than serving out a penalty it never earned.
        monkeypatch.setattr(limits, "MAX_ACTIVE_GAMES", 5)
        assert client.post("/games").status_code == 201

    def test_too_many_live_games_refuses_a_new_one(self, client, monkeypatch):
        monkeypatch.setattr(limits, "MAX_ACTIVE_GAMES", 1)
        assert client.post("/games").status_code == 201
        refused = client.post("/games")
        assert refused.status_code == 429
        assert "Too many games" in refused.json()["error"]

    def test_the_daily_budget_refuses_a_round_before_it_starts(self, client, game, monkeypatch):
        """
        Checked before the stream opens, never mid-round: refusing halfway would leave a game
        with some detectives moved and no way to finish the round.
        """
        game_id, _ = game
        server.GAMES[game_id].status = "detective_loop_running"
        monkeypatch.setattr(limits, "DAILY_LLM_CALL_BUDGET", 0)

        refused = client.get(f"/games/{game_id}/round/stream")
        assert refused.status_code == 503
        assert "thinking limit" in refused.json()["error"]

    def test_stale_games_are_swept_before_the_concurrent_cap_is_applied(self, client, monkeypatch):
        """
        Regression: the cap used to be checked against a store nothing had swept.

        Eviction lived only inside create_game, which sits BELOW the cap check, so once enough
        abandoned games accumulated every POST /games returned 429 and returned before the only
        code that could have evicted them ever ran. The service stayed wedged until a restart -
        which would also have destroyed every genuinely live game. The cap has to bound games
        that are actually alive, so the sweep must happen before the count is taken.
        """
        monkeypatch.setattr(limits, "MAX_ACTIVE_GAMES", 2)
        first = client.post("/games")
        second = client.post("/games")
        assert first.status_code == 201
        assert second.status_code == 201
        # At the cap, with both games live: correctly refused.
        assert client.post("/games").status_code == 429

        # Now age both of them past the TTL without touching them - the abandoned-tab case.
        for session in server.GAMES.values():
            session.last_touched_at -= session_module.SESSION_TTL_SECONDS + 1

        allowed = client.post("/games")
        assert allowed.status_code == 201, (
            "A store holding only TTL-expired games must not refuse a new one - "
            "the stale games should have been swept before the cap was evaluated."
        )
        assert len(server.GAMES) == 1, "the two expired games should be gone, not merely ignored"

    def test_a_round_is_refused_outright_when_no_llm_api_key_is_configured(
        self, client, game, monkeypatch
    ):
        """
        The key is read lazily, so a deployment missing it looks healthy until a round starts -
        at which point the failure would land INSIDE the SSE body, where no status code can be
        sent and the player sees nothing but a dropped connection. Checked before the stream
        opens so it is a readable 503 instead.

        Overrides conftest's autouse _assume_llm_key_configured, which pins this true for every
        other route test.
        """
        monkeypatch.setattr(server, "api_key_configured", lambda: False)
        game_id, _ = game
        server.GAMES[game_id].status = "detective_loop_running"

        refused = client.get(f"/games/{game_id}/round/stream")
        assert refused.status_code == 503
        assert "no LLM API key" in refused.json()["error"]

    def test_every_llm_attempt_counts_including_failures(self):
        # _invoke records before it calls, so a timeout or an auth error still counts - it has
        # been paid for either way.
        limits.reset_for_tests()
        assert limits.llm_calls_today() == 0
        limits.record_llm_call()
        limits.record_llm_call()
        assert limits.llm_calls_today() == 2

    def test_a_malformed_limit_env_var_falls_back_instead_of_crashing(self, monkeypatch):
        monkeypatch.setenv("MAX_ACTIVE_GAMES", "not-a-number")
        assert limits._int_env("MAX_ACTIVE_GAMES", 20) == 20
