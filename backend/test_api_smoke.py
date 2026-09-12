"""
Sync test script (mirrors test_phase3.py / test_phase4_resolve.py's style) smoke-testing the
Starlette API layer (server.py) in-process via starlette.testclient.TestClient - no real network
port needed.

Uses `with TestClient(...) as client:` (not a bare TestClient(...)) so every request in this file
shares ONE persistent event loop/portal - otherwise each call would spin up its own loop and
re-trigger mcp_client.py's module-level MCP-subprocess caching from scratch per request.

The full round/stream event sequence (proposal/debate/vote_tally/round_finalized/round_result)
requires a live OPENROUTER_API_KEY and real LLM calls, so it is NOT exercised automatically here
- see test_full_round_stream_manual() at the bottom, which is intentionally excluded from
__main__.

Run: python test_api_smoke.py
"""
import json

from starlette.testclient import TestClient
import server
from agents import DETECTIVE_IDS


def test_create_get_and_public_state_includes_mr_x_current_node():
    print("\n=== TEST: create + get game - public state includes mr_x.current_node ===")
    # current_node is deliberately public (see serializers.py's own note): the only human-facing
    # client is played BY Mr. X, and detectives are backend-only agents with no client access at
    # all, so this isn't a leak to an opponent - it's what lets the board always show his pawn.
    with TestClient(server.app) as client:
        r = client.post("/games")
        assert r.status_code == 201, r.text
        body = r.json()
        assert isinstance(body["mr_x"]["current_node"], int)
        assert body["status"] == "awaiting_mr_x_move"
        assert set(body["detectives"].keys()) == set(DETECTIVE_IDS)
        game_id = body["game_id"]

        r2 = client.get(f"/games/{game_id}")
        assert r2.status_code == 200
        assert r2.json()["mr_x"]["current_node"] == body["mr_x"]["current_node"]
        assert r2.json() == body
    print("PASSED")


def test_get_unknown_game_returns_404():
    print("\n=== TEST: unknown game id -> 404 ===")
    with TestClient(server.app) as client:
        r = client.get("/games/does-not-exist")
        assert r.status_code == 404
    print("PASSED")


def test_mrx_move_flow_and_turn_guards():
    print("\n=== TEST: mrx legal-moves -> move -> guards flip correctly ===")
    with TestClient(server.app) as client:
        game_id = client.post("/games").json()["game_id"]

        r = client.get(f"/games/{game_id}/mrx/legal-moves")
        assert r.status_code == 200
        legal_moves = r.json()["legal_moves"]
        assert len(legal_moves) > 0
        move = legal_moves[0]

        r2 = client.post(f"/games/{game_id}/mrx/move", json={
            "move_type": "single", "target_node": move["target_node"],
            "ticket_type_spent": move["ticket_options"][0],
        })
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["status"] == "detective_loop_running"
        assert body["mr_x"]["current_node"] == move["target_node"]

        # It's no longer Mr. X's turn - legal-moves must now 409.
        r3 = client.get(f"/games/{game_id}/mrx/legal-moves")
        assert r3.status_code == 409

        # A second move submission must also be rejected (already detective_loop_running).
        r4 = client.post(f"/games/{game_id}/mrx/move", json={
            "move_type": "single", "target_node": move["target_node"],
            "ticket_type_spent": move["ticket_options"][0],
        })
        assert r4.status_code == 400
    print("PASSED")


def test_mrx_move_rejects_illegal_target():
    print("\n=== TEST: illegal move submission -> 400 ===")
    with TestClient(server.app) as client:
        game_id = client.post("/games").json()["game_id"]
        r = client.post(f"/games/{game_id}/mrx/move", json={
            "move_type": "single", "target_node": 999999, "ticket_type_spent": "taxi",
        })
        assert r.status_code == 400
        assert "error" in r.json()
    print("PASSED")


def test_round_stream_guarded_before_mrx_moves():
    print("\n=== TEST: round/stream before Mr. X has moved -> 409 (no LLM call made) ===")
    with TestClient(server.app) as client:
        game_id = client.post("/games").json()["game_id"]
        r = client.get(f"/games/{game_id}/round/stream")
        assert r.status_code == 409, r.text
    print("PASSED")


def test_full_round_stream_manual():
    """
    NOT run automatically (excluded from __main__) - requires a valid OPENROUTER_API_KEY, since
    opening this endpoint after a legal Mr. X move actually runs detective_graph for real.
    Run this function by hand once .env has a working key.
    """
    print("\n=== MANUAL TEST: full round/stream event sequence ===")
    with TestClient(server.app) as client:
        game_id = client.post("/games").json()["game_id"]
        legal_moves = client.get(f"/games/{game_id}/mrx/legal-moves").json()["legal_moves"]
        move = legal_moves[0]
        client.post(f"/games/{game_id}/mrx/move", json={
            "move_type": "single", "target_node": move["target_node"],
            "ticket_type_spent": move["ticket_options"][0],
        })

        with client.stream("GET", f"/games/{game_id}/round/stream") as response:
            assert response.status_code == 200
            event_types = []
            for line in response.iter_lines():
                if line.startswith("event:"):
                    event_types.append(line.split(":", 1)[1].strip())
            assert event_types[-1] == "round_result"
            print("Event sequence:", event_types)

        polled = client.get(f"/games/{game_id}").json()
        assert polled["round_number"] in (1, 2)  # depends on whether the round ended in capture
    print("PASSED")


if __name__ == "__main__":
    test_create_get_and_public_state_includes_mr_x_current_node()
    test_get_unknown_game_returns_404()
    test_mrx_move_flow_and_turn_guards()
    test_mrx_move_rejects_illegal_target()
    test_round_stream_guarded_before_mrx_moves()
    print("\n=== ALL API SMOKE TESTS PASSED ===")
    print("(test_full_round_stream_manual() was NOT run - needs a live OPENROUTER_API_KEY; run it by hand)")
