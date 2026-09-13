"""
Unit tests for reading Mr. X's public travel log back out, round by round.

Everything here guards one invariant, because everything downstream of it uses the result to
RULE OUT places Mr. X could be: if this module ever returns hops that did not happen, or drops
hops that did, the detectives' possible-zone stops containing his true position and the game
silently becomes unfair. So the failure mode these tests care about is not "wrong answer" but
"confident wrong answer" - a malformed log must come back as None so the caller falls back to
the wider, assumption-free ball.
"""
import pytest

from scotland_yard.travel_log import DOUBLE_SENTINEL, bucket_hops_by_round, hops_since_surfacing


class TestBucketHopsByRound:
    def test_single_moves_are_one_round_each(self):
        assert bucket_hops_by_round(["taxi", "bus", "black"]) == [["taxi"], ["bus"], ["black"]]

    def test_a_double_move_is_two_hops_in_one_round(self):
        log = ["taxi", DOUBLE_SENTINEL, "bus", "metro", "black"]
        assert bucket_hops_by_round(log) == [["taxi"], ["bus", "metro"], ["black"]]

    def test_an_empty_log_is_no_rounds_not_a_failure(self):
        assert bucket_hops_by_round([]) == []

    @pytest.mark.parametrize("malformed", [
        [DOUBLE_SENTINEL],                       # sentinel with no hops behind it
        ["taxi", DOUBLE_SENTINEL, "bus"],        # double-move missing its second hop
        [DOUBLE_SENTINEL, DOUBLE_SENTINEL, "bus", "metro"],  # sentinel where a hop belongs
    ])
    def test_a_log_that_cannot_be_read_returns_none_rather_than_guessing(self, malformed):
        assert bucket_hops_by_round(malformed) is None


class TestHopsSinceSurfacing:
    def test_on_the_surfacing_round_he_has_not_moved_since(self):
        # Mr. X moves first, so when the detectives deliberate in round 3 the round-3 hop is
        # already logged and it is the one that produced the revealed node.
        assert hops_since_surfacing(["taxi", "bus", "metro"], last_known_round=3, round_number=3) == []

    def test_one_round_later_yields_exactly_that_round_s_ticket(self):
        log = ["taxi", "bus", "metro", "black"]
        assert hops_since_surfacing(log, last_known_round=3, round_number=4) == ["black"]

    def test_several_rounds_later_yields_them_in_order(self):
        log = ["taxi", "bus", "metro", "black", "taxi", "bus"]
        assert hops_since_surfacing(log, last_known_round=3, round_number=6) == ["black", "taxi", "bus"]

    def test_a_double_move_after_surfacing_contributes_both_hops(self):
        log = ["taxi", "bus", "metro", DOUBLE_SENTINEL, "bus", "taxi"]
        assert hops_since_surfacing(log, last_known_round=3, round_number=4) == ["bus", "taxi"]

    def test_a_double_move_ON_the_surfacing_round_contributes_only_its_second_hop(self):
        # The reveal is the INTERMEDIATE node (game_mechanics.md §2), so hop 1 is what produced
        # the sighting and hop 2 already happened after it. Getting this backwards would put
        # the zone one hop off in the wrong direction - the exact class of bug that makes the
        # zone exclude his true position.
        log = ["taxi", "bus", DOUBLE_SENTINEL, "metro", "black"]
        assert hops_since_surfacing(log, last_known_round=3, round_number=3) == ["black"]

    def test_a_double_move_on_the_surfacing_round_then_more_rounds(self):
        log = ["taxi", "bus", DOUBLE_SENTINEL, "metro", "black", "taxi"]
        assert hops_since_surfacing(log, last_known_round=3, round_number=4) == ["black", "taxi"]

    @pytest.mark.parametrize("last_known_round,round_number,reason", [
        (None, 4, "he has never surfaced"),
        (0, 4, "round numbers start at 1"),
        (5, 4, "surfaced in a round that has not happened"),
        (3, 9, "log is shorter than the round count claims"),
    ])
    def test_anything_that_does_not_reconcile_returns_none(self, last_known_round, round_number, reason):
        log = ["taxi", "bus", "metro", "black"]
        assert hops_since_surfacing(log, last_known_round, round_number) is None, reason

    def test_a_malformed_log_returns_none_even_when_the_rounds_would_line_up(self):
        assert hops_since_surfacing(["taxi", DOUBLE_SENTINEL, "bus"], 1, 2) is None
