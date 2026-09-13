"""
Regression tests for the move-uniqueness guarantees around turn-wise play.

Every test here targets a specific way two detectives could end up assigned the same
destination - the failure mode that used to manifest as a detective silently forfeiting a
turn, with a Chat Log entry claiming they moved. Under turn-wise play (ADR-0009) that is
prevented structurally rather than by a vote tally: each mover's legal-move set excludes every
destination committed earlier in the round, so a collision cannot be constructed in the first
place. These tests pin that exclusion, the deterministic enforcement that backs it when the LLM
names something illegal, and the turn sequencing that drives it.

None of these paths require an LLM: they drive the deterministic helpers directly with
hand-built state.
"""
import pytest

from scotland_yard.agents import (
    _enforce_legal_node,
    annotate_onward_options,
    fetch_legal_moves,
    get_collaboration_tier,
    projected_occupied_nodes,
    responders_for,
)
from scotland_yard.graph import build_next_round_state, finalize_round_node
from scotland_yard.rules_constants import (
    COLLABORATION_TIERS,
    DETECTIVE_IDS,
    DETECTIVE_STARTING_TICKETS,
    MAX_ROUND,
    NUM_DETECTIVES,
)


def build_state(positions: dict, **overrides) -> dict:
    """A minimal ScotlandYardState good enough for the deterministic helpers under test."""
    state = {
        "round_number": 1,
        "turn_index": 0,
        "mr_x": {
            "current_node": 1, "last_known_node": None, "last_known_round": None,
            "transport_history": [], "taxi_tickets": 2, "bus_tickets": 5,
            "metro_tickets": 3, "black_tickets": 5, "double_tickets": 2,
        },
        "detectives": {
            det_id: {"node_id": positions[det_id], **DETECTIVE_STARTING_TICKETS}
            for det_id in DETECTIVE_IDS
        },
        "messages": [],
        "committed_moves": {},
        "turn_records": {},
        "final_moves": {},
        "final_move_details": {},
    }
    state.update(overrides)
    return state


@pytest.fixture
def state(seed_positions):
    return build_state({d: seed_positions[d] for d in DETECTIVE_IDS})


class TestResponderOrder:
    """
    Responses run in cyclic DETECTIVE_IDS order from the mover's immediate successor, so every
    detective hears from all four others exactly once per turn.
    """

    def test_every_non_mover_responds_exactly_once(self):
        for mover in DETECTIVE_IDS:
            responders = responders_for(mover)
            assert len(responders) == NUM_DETECTIVES - 1
            assert mover not in responders
            assert sorted(responders) == sorted(d for d in DETECTIVE_IDS if d != mover)

    def test_order_starts_at_the_movers_immediate_successor(self):
        assert responders_for("agent_red") == [
            "agent_blue", "agent_green", "agent_yellow", "agent_purple",
        ]
        # The wrap-around case: Agent Green is answered by Yellow and Purple before the order
        # comes back round to Red and Blue.
        assert responders_for("agent_green") == [
            "agent_yellow", "agent_purple", "agent_red", "agent_blue",
        ]
        assert responders_for("agent_purple") == [
            "agent_red", "agent_blue", "agent_green", "agent_yellow",
        ]


class TestFetchLegalMoves:
    """
    The single exclusion that makes duplicate destinations structurally impossible. There is
    now exactly one call site pattern - the mover's own turn - so the two-call-sites drift that
    was ISSUE-025 can no longer arise.
    """

    def test_reserved_nodes_are_excluded_from_legal_targets(self, state):
        unreserved, _ = fetch_legal_moves(state, ["agent_red"], reserved_nodes=set())
        target = unreserved["agent_red"][0]["target_node"]

        _, reserved_sets = fetch_legal_moves(state, ["agent_red"], reserved_nodes={target})
        assert target not in reserved_sets["agent_red"]

    def test_other_detectives_current_nodes_are_excluded(self, state, seed_positions):
        _, legal_sets = fetch_legal_moves(state, DETECTIVE_IDS, reserved_nodes=set())
        occupied_by_others = {seed_positions[d] for d in DETECTIVE_IDS}
        for det_id, legal in legal_sets.items():
            assert not (legal & (occupied_by_others - {seed_positions[det_id]}))

    def test_a_detectives_own_node_is_never_a_legal_target(self, state, seed_positions):
        _, legal_sets = fetch_legal_moves(state, DETECTIVE_IDS, reserved_nodes=set())
        for det_id, legal in legal_sets.items():
            assert seed_positions[det_id] not in legal

    def test_no_later_mover_can_reach_an_already_committed_destination(self, state):
        """
        The core turn-wise guarantee, walked through a whole round: committing each detective's
        move in turn order and re-deriving the next mover's options can never offer a node
        someone has already taken.
        """
        committed = {}
        for mover in DETECTIVE_IDS:
            _, legal_sets = fetch_legal_moves(
                state, [mover], reserved_nodes=set(committed.values())
            )
            assert not (legal_sets[mover] & set(committed.values()))
            # Take the lowest-numbered option, the same choice _enforce_legal_node falls back to.
            committed[mover] = min(legal_sets[mover])

        assert len(set(committed.values())) == NUM_DETECTIVES


class TestEnforceLegalNode:
    """
    Deterministic backup enforcement. Under turn-wise play there is no vote tally left to
    discard a bad answer, so this is the only thing standing between an LLM naming a nonsense
    node and that node reaching committed_moves.
    """

    def test_a_legal_node_is_passed_through_untouched(self, state):
        _, legal_sets = fetch_legal_moves(state, ["agent_red"], reserved_nodes=set())
        chosen = max(legal_sets["agent_red"])
        assert _enforce_legal_node(chosen, "agent_red", legal_sets["agent_red"], state, "test") == chosen

    def test_an_illegal_node_is_reassigned_to_the_lowest_legal_one(self, state):
        _, legal_sets = fetch_legal_moves(state, ["agent_red"], reserved_nodes=set())
        legal = legal_sets["agent_red"]
        assert _enforce_legal_node(9999, "agent_red", legal, state, "test") == min(legal)

    def test_a_missing_answer_is_reassigned_rather_than_left_stationary(self, state):
        """A failed or timed-out call must still produce a move if one is available."""
        _, legal_sets = fetch_legal_moves(state, ["agent_red"], reserved_nodes=set())
        legal = legal_sets["agent_red"]
        assert _enforce_legal_node(None, "agent_red", legal, state, "test") == min(legal)

    def test_with_no_legal_move_at_all_the_detective_stays_put(self, state, seed_positions):
        assert _enforce_legal_node(
            9999, "agent_red", set(), state, "test"
        ) == seed_positions["agent_red"]


class TestProjectedOccupancy:
    """
    The forward projection the onward-move annotation is computed against: a detective that has
    already committed is treated as standing on its destination, not its current node.
    """

    def test_committed_detectives_are_projected_to_their_destinations(self, state, seed_positions):
        committed = {"agent_blue": 9999}
        projected = projected_occupied_nodes(state, "agent_red", committed)

        assert 9999 in projected
        assert seed_positions["agent_blue"] not in projected
        # The excluded detective never appears, and everyone else keeps their current node.
        assert seed_positions["agent_red"] not in projected
        assert seed_positions["agent_green"] in projected


class TestOnwardOptions:
    """
    ADR-0009's anti-stranding annotation: how many moves a detective would still have next
    round, after paying for the move that got it there.
    """

    def test_every_candidate_is_annotated(self, state):
        legal_context, _ = fetch_legal_moves(state, ["agent_red"], reserved_nodes=set())
        annotate_onward_options(state, "agent_red", legal_context, committed={})

        assert legal_context["agent_red"]
        for move in legal_context["agent_red"]:
            assert isinstance(move["onward_moves_after"], int)
            assert move["onward_moves_after"] >= 0

    def test_a_detective_with_no_tickets_left_is_shown_every_destination_as_a_dead_end(self, state):
        """The signal that actually matters: spending your last ticket strands you."""
        state["detectives"]["agent_red"].update(taxi_tickets=1, bus_tickets=0, metro_tickets=0)
        legal_context, _ = fetch_legal_moves(state, ["agent_red"], reserved_nodes=set())
        annotate_onward_options(state, "agent_red", legal_context, committed={})

        assert legal_context["agent_red"], "the seeded board should still offer a taxi move"
        assert all(m["onward_moves_after"] == 0 for m in legal_context["agent_red"])


class TestCollaborationTiers:
    """
    The round -> collaboration-tendency ladder that replaced the old desperation scale. The
    boundaries are a design decision (ADR-0009), so they are pinned literally rather than
    recomputed from the table under test.
    """

    @pytest.mark.parametrize(
        "round_number,expected",
        [
            (1, (1, "MINIMUM")), (4, (1, "MINIMUM")),
            (5, (25, "LOW")), (8, (25, "LOW")),
            (9, (50, "MEDIUM")), (12, (50, "MEDIUM")),
            (13, (75, "HIGH")), (16, (75, "HIGH")),
            (17, (99, "MAXIMUM")), (24, (99, "MAXIMUM")),
        ],
    )
    def test_tier_boundaries(self, round_number, expected):
        assert get_collaboration_tier(round_number) == expected

    def test_every_legal_round_falls_inside_a_tier(self):
        for round_number in range(1, MAX_ROUND + 1):
            percentage, label = get_collaboration_tier(round_number)
            assert (percentage, label) in {(p, lbl) for _, p, lbl in COLLABORATION_TIERS}

    def test_collaboration_rises_monotonically_with_the_round(self):
        percentages = [get_collaboration_tier(r)[0] for r in range(1, MAX_ROUND + 1)]
        assert percentages == sorted(percentages)


class TestFinalizeRoundNode:
    """
    Finalize no longer resolves fallbacks - every turn commits - so its remaining job is to
    assemble final_moves and assert the invariant downstream consumers rely on.
    """

    def test_committed_moves_become_final_moves(self, state):
        committed = {}
        for mover in DETECTIVE_IDS:
            _, legal_sets = fetch_legal_moves(state, [mover], reserved_nodes=set(committed.values()))
            committed[mover] = min(legal_sets[mover])
        state["committed_moves"] = committed

        result = finalize_round_node(state)

        assert result["final_moves"] == committed
        assert set(result["final_move_details"]) == set(DETECTIVE_IDS)
        for det_id, detail in result["final_move_details"].items():
            assert detail["from_node"] == state["detectives"][det_id]["node_id"]
            assert detail["to_node"] == committed[det_id]
            assert detail["transport"] in {"taxi", "bus", "metro"}

    def test_a_turn_that_never_committed_fails_loudly(self, state):
        state["committed_moves"] = {"agent_red": 15}
        with pytest.raises(AssertionError, match="no committed move"):
            finalize_round_node(state)

    def test_colliding_destinations_fail_loudly(self, state):
        """
        Unreachable given fetch_legal_moves' reservation, which is exactly why it is asserted:
        a duplicate arriving here means that exclusion has regressed, and it must not degrade
        into a silently forfeited turn.
        """
        collision = {det_id: 9999 for det_id in DETECTIVE_IDS}
        state["committed_moves"] = collision
        with pytest.raises(AssertionError, match="colliding destinations"):
            finalize_round_node(state)


class TestRoundReset:
    """
    A round's per-round fields must not leak into the next one. Left unreset, turn_index would
    already be at NUM_DETECTIVES and the next round would finalize without anyone taking a turn.
    """

    def test_per_round_turn_state_is_cleared(self, state):
        state.update(
            turn_index=NUM_DETECTIVES,
            committed_moves={d: 1 for d in DETECTIVE_IDS},
            turn_records={d: {} for d in DETECTIVE_IDS},
            mrx_zone_context=None,
        )

        nxt = build_next_round_state(state)

        assert nxt["round_number"] == state["round_number"] + 1
        assert nxt["turn_index"] == 0
        assert nxt["committed_moves"] == {}
        assert nxt["turn_records"] == {}
        assert nxt["messages"] == []
        # Key ABSENCE, not None: agents.get_mrx_zone_context treats None as a valid cached
        # pre-reveal value, so leaving the key behind would wrongly read as a cache hit.
        assert "mrx_zone_context" not in nxt
