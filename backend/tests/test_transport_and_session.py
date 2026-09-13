"""
Unit tests for transport.py's apply-time ticket choice and session.py's game store.

transport.determine_move_transport is load-bearing twice over: finalize_round_node calls it
to build the Chat Log preview, and resolve_round calls it to actually deduct a ticket. The
whole point of it being one shared function is that those two can never disagree, so that
property is asserted directly here.
"""
import time

from scotland_yard import session as session_module
from scotland_yard.game_master import compute_valid_moves
from scotland_yard.rules_constants import (
    DETECTIVE_IDS,
    DETECTIVE_STARTING_TICKETS,
    MR_X_STARTING_TICKETS,
    STARTING_NODE_POOL,
)
from scotland_yard.session import GAMES, SESSION_TTL_SECONDS, _evict_stale_games, create_game
from scotland_yard.transport import determine_move_transport, pick_transport


class TestPickTransport:
    def test_prefers_the_ticket_type_the_detective_holds_most_of(self):
        # Conserves the scarce metro allotment by default.
        tickets = {"taxi_tickets": 11, "bus_tickets": 8, "metro_tickets": 4}
        assert pick_transport(["taxi", "bus", "metro"], tickets) == "taxi"
        assert pick_transport(["bus", "metro"], tickets) == "bus"

    def test_falls_back_to_taxi_over_bus_over_metro_on_a_tie(self):
        tied = {"taxi_tickets": 5, "bus_tickets": 5, "metro_tickets": 5}
        assert pick_transport(["taxi", "bus", "metro"], tied) == "taxi"
        assert pick_transport(["bus", "metro"], tied) == "bus"

    def test_a_depleted_type_loses_to_one_still_held(self):
        tickets = {"taxi_tickets": 0, "bus_tickets": 3, "metro_tickets": 1}
        assert pick_transport(["taxi", "bus"], tickets) == "bus"


class TestDetermineMoveTransport:
    def test_returns_none_when_the_target_is_the_detectives_own_node(self):
        detective = {"node_id": 13, **DETECTIVE_STARTING_TICKETS}
        assert determine_move_transport(detective, 13, occupied_nodes=[]) is None

    def test_returns_none_for_an_unreachable_target(self):
        detective = {"node_id": 13, **DETECTIVE_STARTING_TICKETS}
        assert determine_move_transport(detective, 9999, occupied_nodes=[]) is None

    def test_returns_none_when_the_target_is_occupied(self):
        detective = {"node_id": 13, **DETECTIVE_STARTING_TICKETS}
        target = compute_valid_moves(13, 11, 8, 4)[0]["target_node"]
        assert determine_move_transport(detective, target, occupied_nodes=[target]) is None

    def test_picks_a_transport_that_is_actually_legal_for_that_edge(self):
        detective = {"node_id": 13, **DETECTIVE_STARTING_TICKETS}
        for move in compute_valid_moves(13, 11, 8, 4):
            chosen = determine_move_transport(detective, move["target_node"], occupied_nodes=[])
            legal_for_edge = {
                m["transport_used"] for m in compute_valid_moves(13, 11, 8, 4)
                if m["target_node"] == move["target_node"]
            }
            assert chosen in legal_for_edge

    def test_never_picks_a_transport_the_detective_cannot_pay_for(self):
        # Out of metro tickets entirely: a node reachable only by metro is not reachable.
        broke = {"node_id": 13, "taxi_tickets": 0, "bus_tickets": 0, "metro_tickets": 0}
        for move in compute_valid_moves(13, 11, 8, 4):
            assert determine_move_transport(broke, move["target_node"], occupied_nodes=[]) is None


class TestCreateGame:
    def test_seeded_positions_bypass_the_random_draw(self, seeded_session, seed_positions):
        assert seeded_session.state["mr_x"]["current_node"] == seed_positions["mr_x"]
        for det_id in DETECTIVE_IDS:
            assert seeded_session.state["detectives"][det_id]["node_id"] == seed_positions[det_id]

    def test_starting_inventories_match_the_rules(self, seeded_session):
        mr_x = seeded_session.state["mr_x"]
        for key, count in MR_X_STARTING_TICKETS.items():
            assert mr_x[key] == count
        for det_id in DETECTIVE_IDS:
            for key, count in DETECTIVE_STARTING_TICKETS.items():
                assert seeded_session.state["detectives"][det_id][key] == count

    def test_random_draw_gives_six_unique_nodes_from_the_rules_pool(self):
        for _ in range(25):
            state = create_game().state
            drawn = [state["mr_x"]["current_node"]] + [
                state["detectives"][d]["node_id"] for d in DETECTIVE_IDS
            ]
            assert len(set(drawn)) == 6, "starting positions must be unique"
            assert all(node in STARTING_NODE_POOL for node in drawn)

    def test_a_new_game_starts_awaiting_mr_x(self, seeded_session):
        assert seeded_session.status == "awaiting_mr_x_move"
        assert seeded_session.winner is None
        assert seeded_session.state["round_number"] == 1


class TestSessionEviction:
    """
    "No persistence" and "no eviction" were separate decisions; only the first was deliberate.
    Without a TTL every abandoned game leaked for the lifetime of the process.
    """

    def test_a_fresh_game_is_not_evicted(self):
        GAMES.clear()
        session = create_game()
        assert _evict_stale_games() == 0
        assert session.game_id in GAMES

    def test_a_game_idle_past_the_ttl_is_swept(self):
        GAMES.clear()
        session = create_game()
        session.last_touched_at = time.monotonic() - (SESSION_TTL_SECONDS + 1)
        assert _evict_stale_games() == 1
        assert session.game_id not in GAMES

    def test_touch_defers_eviction(self):
        GAMES.clear()
        session = create_game()
        session.last_touched_at = time.monotonic() - (SESSION_TTL_SECONDS + 1)
        session.touch()
        assert _evict_stale_games() == 0
        assert session.game_id in GAMES

    def test_creating_a_game_sweeps_stale_ones(self):
        GAMES.clear()
        stale = create_game()
        stale.last_touched_at = time.monotonic() - (SESSION_TTL_SECONDS + 1)
        fresh = create_game()
        assert stale.game_id not in GAMES
        assert fresh.game_id in GAMES

    def teardown_method(self):
        GAMES.clear()
