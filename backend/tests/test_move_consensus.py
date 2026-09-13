"""
Regression tests for the move-uniqueness guarantees around the consensus loop.

Every test here targets a specific way two detectives could end up assigned the same
destination - the failure mode that used to manifest as a detective silently forfeiting a
turn, with a Chat Log entry claiming they moved. None of these paths require an LLM: they
drive the deterministic helpers directly with hand-built state.
"""
import pytest

from scotland_yard.agents import fetch_legal_moves, find_proposal_conflicts
from scotland_yard.graph import _resolve_fallback_moves, finalize_round_node
from scotland_yard.rules_constants import (
    DETECTIVE_IDS,
    DETECTIVE_STARTING_TICKETS,
    NUM_DETECTIVES,
    VOTE_THRESHOLD,
)


def build_state(positions: dict, **overrides) -> dict:
    """A minimal ScotlandYardState good enough for the deterministic helpers under test."""
    state = {
        "round_number": 1,
        "debate_loop_count": 0,
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
        "proposed_strategies": {},
        "locked_moves": {},
        "final_moves": {},
        "final_move_details": {},
    }
    state.update(overrides)
    return state


@pytest.fixture
def state(seed_positions):
    return build_state({d: seed_positions[d] for d in DETECTIVE_IDS})


class TestFetchLegalMoves:
    """
    ISSUE-025: propose_node reserved already-locked destinations, vote_node did not. The two
    call sites now share one helper, so they cannot drift apart again.
    """

    def test_reserved_nodes_are_excluded_from_legal_targets(self, state):
        pending = ["agent_red"]
        unreserved, _ = fetch_legal_moves(state, pending, reserved_nodes=set())
        target = unreserved["agent_red"][0]["target_node"]

        _, reserved_sets = fetch_legal_moves(state, pending, reserved_nodes={target})
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

    def test_vote_and_propose_see_identical_legality_for_identical_inputs(self, state):
        """
        The concrete ISSUE-025 regression: given the same locked moves, the legal-move set a
        voter sees must match the one a proposer sees. Previously the voter's set was strictly
        larger, by exactly the already-locked destinations.
        """
        pending = ["agent_blue", "agent_green"]
        locked = {"agent_red": next(iter(
            fetch_legal_moves(state, ["agent_red"], reserved_nodes=set())[1]["agent_red"]
        ))}
        state["locked_moves"] = locked

        _, as_proposer = fetch_legal_moves(state, pending, reserved_nodes=set(locked.values()))
        _, as_voter = fetch_legal_moves(state, pending, reserved_nodes=set(locked.values()))
        assert as_proposer == as_voter
        for legal in as_voter.values():
            assert not (legal & set(locked.values()))


class TestFallbackResolution:
    """
    ISSUE-026: finalize's fallback drew each unlocked detective's move from a DIFFERENT
    proposer's strategy object, so two fallbacks could name the same node.
    """

    def test_colliding_self_proposals_are_deduplicated(self, state):
        _, legal = fetch_legal_moves(state, DETECTIVE_IDS, reserved_nodes=set())
        # Find one node that two different detectives can both legally reach.
        contested = next(
            node for node in legal["agent_red"]
            if node in legal["agent_blue"]
        )
        # Each proposer proposes that same node for itself - exactly what independent,
        # concurrent proposals can produce.
        state["proposed_strategies"] = {
            "agent_red": {"proposed_board_moves": {"agent_red": contested}, "rationale": ""},
            "agent_blue": {"proposed_board_moves": {"agent_blue": contested}, "rationale": ""},
        }

        final_moves = _resolve_fallback_moves(state, locked={})

        assert final_moves["agent_red"] == contested  # earlier in DETECTIVE_IDS order wins
        assert final_moves["agent_blue"] != contested
        moving = [n for d, n in final_moves.items() if n != state["detectives"][d]["node_id"]]
        assert len(moving) == len(set(moving))

    def test_an_illegal_self_proposal_is_reassigned_not_accepted(self, state):
        state["proposed_strategies"] = {
            "agent_red": {"proposed_board_moves": {"agent_red": 9999}, "rationale": ""},
        }
        final_moves = _resolve_fallback_moves(state, locked={})
        _, legal = fetch_legal_moves(state, ["agent_red"], reserved_nodes=set())
        assert final_moves["agent_red"] in legal["agent_red"]

    def test_a_missing_self_proposal_still_produces_a_legal_move(self, state):
        # No proposed_strategies at all - the "data missing/corrupted" path.
        final_moves = _resolve_fallback_moves(state, locked={})
        assert set(final_moves) == set(DETECTIVE_IDS)
        moving = [n for d, n in final_moves.items() if n != state["detectives"][d]["node_id"]]
        assert len(moving) == len(set(moving))

    def test_locked_moves_are_preserved_verbatim(self, state):
        _, legal = fetch_legal_moves(state, DETECTIVE_IDS, reserved_nodes=set())
        locked = {"agent_red": min(legal["agent_red"])}
        final_moves = _resolve_fallback_moves(state, locked=locked)
        assert final_moves["agent_red"] == locked["agent_red"]

    def test_a_fallback_never_takes_a_locked_destination(self, state):
        _, legal = fetch_legal_moves(state, DETECTIVE_IDS, reserved_nodes=set())
        contested = next(n for n in legal["agent_red"] if n in legal["agent_blue"])
        locked = {"agent_red": contested}
        state["locked_moves"] = locked
        state["proposed_strategies"] = {
            "agent_blue": {"proposed_board_moves": {"agent_blue": contested}, "rationale": ""},
        }
        final_moves = _resolve_fallback_moves(state, locked=locked)
        assert final_moves["agent_blue"] != contested


class TestFinalizeRoundNode:
    def test_produces_one_destination_per_detective_with_a_transport_preview(self, state):
        result = finalize_round_node(state)
        assert set(result["final_moves"]) == set(DETECTIVE_IDS)
        assert set(result["final_move_details"]) == set(DETECTIVE_IDS)
        for det_id, details in result["final_move_details"].items():
            assert details["from_node"] == state["detectives"][det_id]["node_id"]
            assert details["to_node"] == result["final_moves"][det_id]
            # A detective that actually moved must have a real transport, never null - that
            # null is what the Chat Log renders as "stays at Node N".
            if details["to_node"] != details["from_node"]:
                assert details["transport"] in ("taxi", "bus", "metro")

    def test_raises_rather_than_emitting_colliding_destinations(self, state, monkeypatch):
        """
        The invariant assertion itself. Forcing a collision past the de-duplication must fail
        loudly here, not degrade into a silent forfeit inside resolve_round.
        """
        collision = {det_id: 46 for det_id in DETECTIVE_IDS}
        monkeypatch.setattr(
            "scotland_yard.graph._resolve_fallback_moves", lambda *a, **kw: collision
        )
        with pytest.raises(AssertionError, match="colliding destinations"):
            finalize_round_node(state)


class TestProposalConflictDetection:
    def test_flags_an_out_of_bounds_destination(self):
        class Strategy:
            agent_red_move = 9999

        conflicts = find_proposal_conflicts(Strategy(), ["agent_red"], {"agent_red": {1, 2}})
        assert len(conflicts) == 1
        assert "not one of its legal moves" in conflicts[0]

    def test_flags_a_duplicate_destination_within_one_proposal(self):
        class Strategy:
            agent_red_move = 5
            agent_blue_move = 5

        conflicts = find_proposal_conflicts(
            Strategy(), ["agent_red", "agent_blue"], {"agent_red": {5}, "agent_blue": {5}}
        )
        assert len(conflicts) == 1
        assert "duplicates" in conflicts[0]

    def test_reports_nothing_for_a_clean_proposal(self):
        class Strategy:
            agent_red_move = 5
            agent_blue_move = 6

        conflicts = find_proposal_conflicts(
            Strategy(), ["agent_red", "agent_blue"], {"agent_red": {5}, "agent_blue": {6}}
        )
        assert conflicts == []


def test_vote_threshold_is_a_strict_majority():
    """
    The arithmetic that makes a cross-tally collision impossible within one loop (ISSUE-010):
    each ballot is already de-duplicated, so two targets both reaching VOTE_THRESHOLD on the
    same node would need VOTE_THRESHOLD * 2 distinct ballots. rules_constants asserts this at
    import; this test states it where a reader of the test suite will actually see it.
    """
    assert VOTE_THRESHOLD * 2 > NUM_DETECTIVES
