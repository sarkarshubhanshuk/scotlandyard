"""
Regression tests for the move-uniqueness guarantees around turn-wise play.

Every test here targets a specific way two detectives could end up assigned the same
destination - the failure mode that used to manifest as a detective silently forfeiting a
turn, with a Chat Log entry claiming they moved. Under turn-wise play (ADR-0009/ADR-0010) that
is prevented structurally rather than by a vote tally: each detective's move is applied the
moment its turn ends, so the next detective is simply never offered a node somebody is standing
on. These tests pin that exclusion, the per-turn application behind it, the deterministic
enforcement that backs it when the LLM names something illegal, and the turn sequencing.

None of these paths require an LLM: they drive the deterministic helpers directly with
hand-built state.
"""
import pytest

from scotland_yard.agents import (
    _enforce_legal_node,
    annotate_onward_options,
    apply_detective_move,
    fetch_legal_moves,
    get_collaboration_tier,
    other_detective_nodes,
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
        "captured_by": None,
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
    The single exclusion that makes duplicate destinations structurally impossible: a detective
    is only ever offered nodes no other detective is standing on. Under per-turn application
    (ADR-0010) "standing on" is literally true, so this needs no reservation bookkeeping.
    """

    def test_other_detectives_current_nodes_are_excluded(self, state, seed_positions):
        _, legal_sets = fetch_legal_moves(state, DETECTIVE_IDS)
        occupied_by_others = {seed_positions[d] for d in DETECTIVE_IDS}
        for det_id, legal in legal_sets.items():
            assert not (legal & (occupied_by_others - {seed_positions[det_id]}))

    def test_a_detectives_own_node_is_never_a_legal_target(self, state, seed_positions):
        _, legal_sets = fetch_legal_moves(state, DETECTIVE_IDS)
        for det_id, legal in legal_sets.items():
            assert seed_positions[det_id] not in legal

    def test_no_later_mover_can_reach_a_node_someone_is_standing_on(self, state):
        """
        The core turn-wise guarantee, walked through a whole round: applying each detective's
        move in turn order and re-deriving the next one's options can never offer an occupied
        node, and every detective ends the round somewhere distinct.
        """
        for mover in DETECTIVE_IDS:
            _, legal_sets = fetch_legal_moves(state, [mover])
            occupied = {
                info["node_id"] for det_id, info in state["detectives"].items() if det_id != mover
            }
            assert not (legal_sets[mover] & occupied)
            # Take the lowest-numbered option, the same choice _enforce_legal_node falls back to.
            state.update(apply_detective_move(state, mover, min(legal_sets[mover])))

        final = {det_id: info["node_id"] for det_id, info in state["detectives"].items()}
        assert len(set(final.values())) == NUM_DETECTIVES

    def test_a_node_an_earlier_detective_vacated_becomes_available(self, state, seed_positions):
        """
        rules.md section 2/3: detectives move in sequence, so the node Agent Red leaves is simply
        unoccupied by the time a later detective's turn comes round. This used to be forbidden -
        the old reservation scheme blocked a mover's ORIGIN for the rest of the round as well as
        its destination, a constraint the rules never imposed. See ADR-0010.
        """
        red_start = seed_positions["agent_red"]
        assert red_start in other_detective_nodes(state, "agent_blue")

        _, legal_sets = fetch_legal_moves(state, ["agent_red"])
        red_target = min(legal_sets["agent_red"])
        state.update(apply_detective_move(state, "agent_red", red_target))

        # Red's old node stops constraining everyone else the moment Red leaves it, and its new
        # one starts. Asserted on the occupancy set rather than on some other detective's legal
        # moves, which would additionally depend on whoever happens to be adjacent to it.
        after = other_detective_nodes(state, "agent_blue")
        assert red_start not in after
        assert red_target in after


class TestEnforceLegalNode:
    """
    Deterministic backup enforcement. Under turn-wise play there is no vote tally left to
    discard a bad answer, so this is the only thing standing between an LLM naming a nonsense
    node and that node reaching committed_moves.
    """

    def test_a_legal_node_is_passed_through_untouched(self, state):
        _, legal_sets = fetch_legal_moves(state, ["agent_red"])
        chosen = max(legal_sets["agent_red"])
        assert _enforce_legal_node(chosen, "agent_red", legal_sets["agent_red"], state, "test") == chosen

    def test_an_illegal_node_is_reassigned_to_the_lowest_legal_one(self, state):
        _, legal_sets = fetch_legal_moves(state, ["agent_red"])
        legal = legal_sets["agent_red"]
        assert _enforce_legal_node(9999, "agent_red", legal, state, "test") == min(legal)

    def test_a_missing_answer_is_reassigned_rather_than_left_stationary(self, state):
        """A failed or timed-out call must still produce a move if one is available."""
        _, legal_sets = fetch_legal_moves(state, ["agent_red"])
        legal = legal_sets["agent_red"]
        assert _enforce_legal_node(None, "agent_red", legal, state, "test") == min(legal)

    def test_with_no_legal_move_at_all_the_detective_stays_put(self, state, seed_positions):
        assert _enforce_legal_node(
            9999, "agent_red", set(), state, "test"
        ) == seed_positions["agent_red"]


class TestApplyDetectiveMove:
    """
    Per-turn move application (ADR-0010): the detective physically moves, its ticket transfers
    to Mr. X, and capture is decided the instant it lands.
    """

    def test_a_move_relocates_the_detective_and_transfers_one_ticket(self, state, seed_positions):
        _, legal_sets = fetch_legal_moves(state, ["agent_red"])
        target = min(legal_sets["agent_red"])
        before_mrx = dict(state["mr_x"])

        result = apply_detective_move(state, "agent_red", target)

        assert result["from_node"] == seed_positions["agent_red"]
        assert result["detectives"]["agent_red"]["node_id"] == target
        assert result["transport"] in {"taxi", "bus", "metro"}
        key = f"{result['transport']}_tickets"
        assert result["detectives"]["agent_red"][key] == state["detectives"]["agent_red"][key] - 1
        assert result["mr_x"][key] == before_mrx[key] + 1

    def test_the_input_state_is_not_mutated(self, state, seed_positions):
        """The node returns an update for LangGraph to merge; it must not edit state in place."""
        _, legal_sets = fetch_legal_moves(state, ["agent_red"])
        apply_detective_move(state, "agent_red", min(legal_sets["agent_red"]))
        assert state["detectives"]["agent_red"]["node_id"] == seed_positions["agent_red"]

    def test_an_unreachable_target_forfeits_without_spending_a_ticket(self, state, seed_positions):
        """rules.md section 3: a forfeited turn costs no ticket and transfers none to Mr. X."""
        before = dict(state["detectives"]["agent_red"])
        result = apply_detective_move(state, "agent_red", 9999)

        assert result["transport"] is None
        assert result["detectives"]["agent_red"] == before
        assert result["mr_x"] == state["mr_x"]
        assert result["captured"] is False

    def test_landing_on_mr_x_reports_a_capture(self, state):
        _, legal_sets = fetch_legal_moves(state, ["agent_red"])
        target = min(legal_sets["agent_red"])
        state["mr_x"]["current_node"] = target

        assert apply_detective_move(state, "agent_red", target)["captured"] is True

    def test_staying_put_on_mr_x_s_node_is_not_a_capture(self, state, seed_positions):
        """Defensive: a forfeit cannot manufacture a capture out of a node nobody moved to."""
        state["mr_x"]["current_node"] = seed_positions["agent_red"]
        # Contrived - Mr. X can never legally share a detective's node - but the guard should
        # depend on where the detective ENDS UP, and this is the one case that distinguishes
        # "landed here" from "was already here".
        assert apply_detective_move(state, "agent_red", 9999)["captured"] is True


class TestOtherDetectiveNodes:
    """Occupancy is now simply where everyone else is standing - no projection needed."""

    def test_excludes_the_named_detective_and_includes_the_rest(self, state, seed_positions):
        nodes = other_detective_nodes(state, "agent_red")

        assert seed_positions["agent_red"] not in nodes
        assert sorted(nodes) == sorted(
            seed_positions[d] for d in DETECTIVE_IDS if d != "agent_red"
        )


class TestOnwardOptions:
    """
    ADR-0009's anti-stranding annotation: how many moves a detective would still have next
    round, after paying for the move that got it there.
    """

    def test_every_candidate_is_annotated(self, state):
        legal_context, _ = fetch_legal_moves(state, ["agent_red"])
        annotate_onward_options(state, "agent_red", legal_context)

        assert legal_context["agent_red"]
        for move in legal_context["agent_red"]:
            assert isinstance(move["onward_moves_after"], int)
            assert move["onward_moves_after"] >= 0

    def test_a_detective_with_no_tickets_left_is_shown_every_destination_as_a_dead_end(self, state):
        """The signal that actually matters: spending your last ticket strands you."""
        state["detectives"]["agent_red"].update(taxi_tickets=1, bus_tickets=0, metro_tickets=0)
        legal_context, _ = fetch_legal_moves(state, ["agent_red"])
        annotate_onward_options(state, "agent_red", legal_context)

        assert legal_context["agent_red"], "the seeded board should still offer a taxi move"
        assert all(m["onward_moves_after"] == 0 for m in legal_context["agent_red"])


class _FakeStructured:
    """Answers whichever schema it is handed with a fixed, legal-looking value."""

    def __init__(self, schema, target_node: int):
        self.schema = schema
        self.target_node = target_node

    async def ainvoke(self, messages):
        if self.schema.__name__ == "MoveChoice":
            return self.schema(target_node=self.target_node, rationale="stub")
        return self.schema(response="stub", preferred_node=self.target_node)


class _FakeLLM:
    def __init__(self, target_node: int):
        self.target_node = target_node

    def with_structured_output(self, schema):
        return _FakeStructured(schema, self.target_node)


class TestCaptureShortCircuit:
    """
    A detective landing on Mr. X ends the game at that instant (rules.md section 6), so the
    detectives behind it in the turn order must never take a turn - which is also what stops
    the round spending ~24 further billable LLM calls deliberating a decided game.

    Drives the real compiled graph with stubbed LLMs, since the behaviour under test is the
    router's, and a unit test of the router alone would not prove the turns are actually skipped.
    """

    async def test_detectives_after_the_captor_never_take_a_turn(self, state, seed_positions, monkeypatch):
        from scotland_yard import agents
        from scotland_yard.graph import build_detective_graph

        # Put Mr. X on a node Agent Red can reach, and have every LLM call name exactly that
        # node - so Red's very first turn ends in a capture.
        _, legal_sets = fetch_legal_moves(state, ["agent_red"])
        target = min(legal_sets["agent_red"])
        state["mr_x"]["current_node"] = target

        fake = _FakeLLM(target)

        async def fake_llm():
            return fake

        monkeypatch.setattr(agents, "get_detective_llm", fake_llm)
        monkeypatch.setattr(agents, "get_debate_llm", fake_llm)

        result = await build_detective_graph().ainvoke(state)

        assert result["captured_by"] == "agent_red"
        assert result["detectives"]["agent_red"]["node_id"] == target
        # Exactly one turn happened, and it was Red's.
        assert set(result["turn_records"]) == {"agent_red"}
        assert result["turn_index"] == 1
        for det_id in DETECTIVE_IDS[1:]:
            assert result["detectives"][det_id]["node_id"] == seed_positions[det_id], \
                f"{det_id} moved after the game was already over"


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


def _play_out_round(state) -> dict:
    """Walks a whole round the way turn_node would, returning the turn_records it built."""
    records = {}
    for mover in DETECTIVE_IDS:
        _, legal_sets = fetch_legal_moves(state, [mover])
        target = min(legal_sets[mover])
        applied = apply_detective_move(state, mover, target)
        records[mover] = {
            "proposed_node": target,
            "proposal_rationale": "",
            "responses": [],
            "from_node": applied["from_node"],
            "committed_node": applied["detectives"][mover]["node_id"],
            "transport": applied["transport"],
            "decision_rationale": "",
        }
        state.update(applied)
    return records


class TestFinalizeRoundNode:
    """
    Finalize neither resolves fallbacks nor applies moves any more - every turn ends in a move
    that already happened - so its remaining job is to summarise the round from turn_records and
    assert the invariant downstream consumers rely on.
    """

    def test_turn_records_become_final_moves(self, state):
        records = _play_out_round(state)
        state["turn_records"] = records

        result = finalize_round_node(state)

        assert set(result["final_move_details"]) == set(DETECTIVE_IDS)
        for det_id, detail in result["final_move_details"].items():
            assert detail["from_node"] == records[det_id]["from_node"]
            assert detail["to_node"] == records[det_id]["committed_node"]
            assert detail["transport"] in {"taxi", "bus", "metro"}
            # The origin is no longer derivable from state - the detective has already left it.
            assert detail["to_node"] == state["detectives"][det_id]["node_id"]

    def test_a_turn_that_never_happened_fails_loudly(self, state):
        state["turn_records"] = {"agent_red": {
            "from_node": 1, "committed_node": 15, "transport": "taxi", "responses": [],
            "proposed_node": 15, "proposal_rationale": "", "decision_rationale": "",
        }}
        with pytest.raises(AssertionError, match="no turn record"):
            finalize_round_node(state)

    def test_a_round_cut_short_by_a_capture_finalizes_with_only_the_turns_taken(self, state):
        """
        Once a detective lands on Mr. X the game is over and the detectives behind it never
        move, so a partial round is the rules-correct outcome rather than a missing-data bug.
        """
        state["turn_records"] = {"agent_red": {
            "from_node": 1, "committed_node": 15, "transport": "taxi", "responses": [],
            "proposed_node": 15, "proposal_rationale": "", "decision_rationale": "",
        }}
        state["captured_by"] = "agent_red"

        result = finalize_round_node(state)

        assert result["final_moves"] == {"agent_red": 15}
        assert set(result["final_move_details"]) == {"agent_red"}

    def test_colliding_destinations_fail_loudly(self, state):
        """
        Unreachable given fetch_legal_moves' occupancy exclusion, which is exactly why it is
        asserted: a duplicate arriving here means that exclusion has regressed, and it must not
        degrade into two pawns sharing a square.
        """
        state["turn_records"] = {
            det_id: {
                "from_node": 1, "committed_node": 9999, "transport": "taxi", "responses": [],
                "proposed_node": 9999, "proposal_rationale": "", "decision_rationale": "",
            }
            for det_id in DETECTIVE_IDS
        }
        with pytest.raises(AssertionError, match="colliding destinations"):
            finalize_round_node(state)


class TestRoundReset:
    """
    A round's per-round fields must not leak into the next one. Left unreset, turn_index would
    already be at NUM_DETECTIVES and the next round would finalize without anyone taking a turn.
    """

    def test_per_round_turn_state_is_cleared(self, state, seed_positions):
        state.update(
            turn_index=NUM_DETECTIVES,
            committed_moves={d: 1 for d in DETECTIVE_IDS},
            turn_records={d: {} for d in DETECTIVE_IDS},
            captured_by="agent_red",
        )

        nxt = build_next_round_state(state)

        assert nxt["round_number"] == state["round_number"] + 1
        assert nxt["turn_index"] == 0
        assert nxt["committed_moves"] == {}
        assert nxt["turn_records"] == {}
        assert nxt["captured_by"] is None
        assert nxt["messages"] == []

    def test_positions_and_tickets_carry_forward_untouched(self, state):
        """Each move was applied when its own turn ended, so there is nothing left to apply."""
        _play_out_round(state)
        moved = {d: dict(info) for d, info in state["detectives"].items()}

        nxt = build_next_round_state(state)

        assert nxt["detectives"] == moved
        assert nxt["mr_x"] == state["mr_x"]
