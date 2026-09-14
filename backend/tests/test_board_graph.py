"""
Unit tests for game_master's pure board-graph functions.

These are the functions every other layer trusts for legality and spatial reasoning, and
until now they had no direct coverage at all - they were only ever exercised incidentally
through a full LLM round, which is slow, costs money, and is non-deterministic.
"""
import random

from scotland_yard.game_master import (
    _bfs_from,
    compute_distances_to_zone,
    compute_mrx_zone,
    compute_mrx_zone_from_tickets,
    compute_valid_moves,
    get_node_info,
    map_data,
    node_index,
    project_zone_one_hop,
)


class TestBoardData:
    def test_board_has_199_nodes_numbered_per_the_rules(self):
        # rules.md §1: 199 nodes, numbered 1-107 and 109-200 (node 108 is absent).
        assert len(map_data) == 199
        ids = {node["id"] for node in map_data}
        assert ids == set(range(1, 108)) | set(range(109, 201))
        assert 108 not in ids

    def test_every_connection_points_at_a_real_node(self):
        for node in map_data:
            for connection in node["connections"]:
                assert connection["destination"] in node_index, (
                    f"Node {node['id']} connects to non-existent node {connection['destination']}"
                )

    def test_get_node_info_reports_a_missing_node_rather_than_raising(self):
        assert "error" in get_node_info(108)
        assert "error" in get_node_info(9999)


class TestComputeValidMoves:
    def test_returns_only_moves_the_player_can_pay_for(self):
        # With taxi tickets only, every returned move must be a taxi route.
        moves = compute_valid_moves(13, taxi_tickets=5, bus_tickets=0, metro_tickets=0)
        assert moves
        assert {m["transport_used"] for m in moves} == {"taxi"}

    def test_no_tickets_means_no_legal_moves(self):
        assert compute_valid_moves(13, taxi_tickets=0, bus_tickets=0, metro_tickets=0) == []

    def test_black_ticket_unlocks_every_route_type_including_boat(self):
        # Node 194 is one of the board's boat nodes; boat is reachable only with a black ticket.
        boat_nodes = [
            node["id"] for node in map_data
            if any(c["type"] == "boat" for c in node["connections"])
        ]
        assert boat_nodes, "expected the board to contain boat routes"
        node_id = boat_nodes[0]

        without_black = compute_valid_moves(node_id, 9, 9, 9, black_tickets=0)
        with_black = compute_valid_moves(node_id, 9, 9, 9, black_tickets=1)

        assert "boat" not in {m["transport_used"] for m in without_black}
        assert "boat" in {m["transport_used"] for m in with_black}

    def test_occupied_nodes_are_excluded_as_destinations(self):
        all_moves = compute_valid_moves(13, 9, 9, 9)
        blocked = all_moves[0]["target_node"]
        filtered = compute_valid_moves(13, 9, 9, 9, occupied_nodes=[blocked])
        assert blocked not in {m["target_node"] for m in filtered}

    def test_a_node_pair_can_be_offered_under_more_than_one_transport(self):
        # This is exactly why transport.pick_transport has to exist: the caller asks for a
        # target node, and more than one ticket type can legally pay to reach it.
        moves = compute_valid_moves(1, 9, 9, 9)
        by_target = {}
        for move in moves:
            by_target.setdefault(move["target_node"], []).append(move["transport_used"])
        assert any(len(transports) > 1 for transports in by_target.values())


class TestBfsAndZone:
    def test_bfs_seeds_sources_at_distance_zero(self):
        distances = _bfs_from({13}, max_hops=0)
        assert distances == {13: 0}

    def test_bfs_respects_the_hop_cap(self):
        one_hop = _bfs_from({13}, max_hops=1)
        two_hops = _bfs_from({13}, max_hops=2)
        assert set(one_hop) < set(two_hops)
        assert max(one_hop.values()) == 1
        assert max(two_hops.values()) == 2

    def test_blocked_nodes_are_excluded_as_destination_and_as_pass_through(self):
        # rules.md: Mr. X cannot move to, OR PASS THROUGH, a detective-occupied node.
        unblocked = _bfs_from({13}, max_hops=2)
        neighbour = next(n for n, d in unblocked.items() if d == 1)
        blocked = _bfs_from({13}, max_hops=2, blocked_nodes={neighbour})

        assert neighbour not in blocked
        # Anything only reachable *through* the blocked neighbour is gone too.
        assert set(blocked) <= set(unblocked)

    def test_compute_mrx_zone_grows_with_turns_since_surfacing(self):
        one = compute_mrx_zone(13, max_hops=1)
        three = compute_mrx_zone(13, max_hops=3)
        assert set(one) < set(three)

    def test_compute_distances_to_zone_is_zero_inside_the_zone(self):
        zone = compute_mrx_zone(13, max_hops=2)
        distances = compute_distances_to_zone(zone.keys())
        for node_id in zone:
            assert distances[node_id] == 0

    def test_compute_distances_to_zone_covers_the_whole_board(self):
        # A single multi-source BFS run to exhaustion: the board is connected, so every node
        # should get a distance. If this ever fails, the board data has an isolated component.
        distances = compute_distances_to_zone(compute_mrx_zone(13, max_hops=1).keys())
        assert len(distances) == len(map_data)


class TestTicketNarrowedZone:
    """
    The typed walk that replaced the untyped ball for detectives' possible-zone reasoning.

    The invariant that matters is one-directional: this set may be WIDER than the truth (that
    only makes detectives cautious) but it must never be NARROWER, because it is used to rule
    locations out. test_the_true_position_is_always_inside_the_zone is the test that actually
    guards the game's fairness; the rest describe the mechanism.
    """

    def test_a_typed_hop_only_reaches_that_connection_type(self):
        taxi_only = compute_mrx_zone_from_tickets(13, ["taxi"])
        expected = {c["destination"] for c in get_node_info(13)["connections"] if c["type"] == "taxi"}
        assert taxi_only == expected

    def test_a_black_ticket_reaches_every_connection_type(self):
        black = compute_mrx_zone_from_tickets(13, ["black"])
        expected = {c["destination"] for c in get_node_info(13)["connections"]}
        assert black == expected, "black buys any connection - that is the whole point of it"

    def test_no_tickets_spent_means_he_is_exactly_where_he_was_seen(self):
        assert compute_mrx_zone_from_tickets(13, []) == {13}

    def test_the_typed_zone_is_a_subset_of_the_untyped_ball(self):
        # Same claim the whole change rests on: strictly better information, never different
        # information. Compared at equal hop counts against what the fallback would produce.
        for start in (1, 13, 67, 128, 199):
            for tickets in (["taxi", "taxi"], ["bus", "taxi"], ["taxi", "bus", "taxi"]):
                typed = compute_mrx_zone_from_tickets(start, tickets)
                if typed is None:
                    continue
                ball = set(compute_mrx_zone(start, max_hops=len(tickets)))
                assert typed <= ball

    def test_the_true_position_is_always_inside_the_zone(self):
        # THE fairness test. Walk Mr. X around the real board, record only what the detectives
        # would actually be told (each hop's ticket type), then assert the zone derived from
        # that log still contains where he really ended up. 200 seeded-random routes.
        rng = random.Random(4242)
        for _ in range(200):
            start = rng.choice(list(node_index))
            position = start
            tickets = []
            for _ in range(rng.randint(1, 6)):
                connections = get_node_info(position)["connections"]
                if not connections:
                    break
                hop = rng.choice(connections)
                tickets.append("black" if hop["type"] == "boat" else hop["type"])
                position = hop["destination"]
            if not tickets:
                continue
            zone = compute_mrx_zone_from_tickets(start, tickets)
            assert zone is not None and position in zone, (
                f"walk from {start} via {tickets} ended at {position}, which the zone excluded"
            )

    def test_a_detective_standing_in_the_way_is_not_a_possible_location(self):
        neighbours = {c["destination"] for c in get_node_info(13)["connections"] if c["type"] == "taxi"}
        blocked = next(iter(neighbours))
        zone = compute_mrx_zone_from_tickets(13, ["taxi"], occupied_nodes=[blocked])
        assert blocked not in zone

    def test_a_walk_that_dead_ends_reports_none_rather_than_an_empty_zone(self):
        # A claimed metro hop from a node with no metro connection is impossible, which means
        # an assumption upstream is wrong: the caller must fall back to the wider ball, not be
        # handed an empty set it would read as "he is nowhere". The node is found rather than
        # hardcoded so this cannot quietly stop testing anything if the board data changes.
        metro_less = next(
            node_id for node_id in sorted(node_index)
            if not any(c["type"] == "metro" for c in get_node_info(node_id)["connections"])
        )
        assert compute_mrx_zone_from_tickets(metro_less, ["metro"]) is None


class TestProjectZoneOneHop:
    def test_projection_is_untyped_because_the_next_ticket_is_unknown(self):
        projected = project_zone_one_hop({13})
        assert projected == {c["destination"] for c in get_node_info(13)["connections"]}

    def test_blocking_a_node_shrinks_the_projection(self):
        openly = project_zone_one_hop({13})
        blocker = next(iter(openly))
        closed = project_zone_one_hop({13}, blocked_nodes={blocker})
        assert blocker not in closed
        assert len(closed) < len(openly), "standing on an exit must measurably close space"
