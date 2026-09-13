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
import pytest
from starlette.testclient import TestClient

from scotland_yard import server
from scotland_yard.rules_constants import DETECTIVE_IDS


@pytest.fixture
def client():
    with TestClient(server.app) as c:
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
