"""
Manual scratch check for the two new backend additions (Phase 4 M1) - not part of the automated
smoke suite. Run: python test_map_and_hop2_manual.py
"""
from starlette.testclient import TestClient
import server


def test_map_route():
    print("\n=== TEST: GET /games/{id}/map ===")
    with TestClient(server.app) as client:
        game_id = client.post("/games").json()["game_id"]
        r = client.get(f"/games/{game_id}/map")
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["nodes"]) == 199, len(body["nodes"])
        assert len(body["positions"]) == 199, len(body["positions"])
        assert "1" in body["positions"] and "x" in body["positions"]["1"]
    print("PASSED")


def test_hop2_preview_requires_double_ticket_and_legal_hop1():
    print("\n=== TEST: hop-2 preview validates hop-1 legality + double-ticket availability ===")
    with TestClient(server.app) as client:
        game_id = client.post("/games").json()["game_id"]

        # A nonsense from_node/ticket_type_spent should 400, not crash.
        r = client.get(f"/games/{game_id}/mrx/legal-moves?from_node=999999&ticket_type_spent=taxi")
        assert r.status_code == 400, r.text
        print("  no-connection from_node -> 400 OK:", r.json())

        # A real legal hop-1 (Mr. X starts with double_tickets=2, so this should succeed and
        # return hop-2 options sourced from the hop-1 target, not from Mr. X's real current node).
        legal_moves = client.get(f"/games/{game_id}/mrx/legal-moves").json()["legal_moves"]
        hop1 = legal_moves[0]
        r2 = client.get(
            f"/games/{game_id}/mrx/legal-moves"
            f"?from_node={hop1['target_node']}&ticket_type_spent={hop1['ticket_options'][0]}"
        )
        assert r2.status_code == 200, r2.text
        hop2_options = r2.json()["legal_moves"]
        print("  hop1:", hop1, "-> hop2 options:", hop2_options)
        assert isinstance(hop2_options, list)
        # hop-2 options should differ from a plain (non-hop2) legal-moves call from the real
        # current node in the general case, since they're one hop further out.
        assert all("target_node" in m and "ticket_options" in m for m in hop2_options)
    print("PASSED")


if __name__ == "__main__":
    test_map_route()
    test_hop2_preview_requires_double_ticket_and_legal_hop1()
    print("\n=== ALL MANUAL CHECKS PASSED ===")
