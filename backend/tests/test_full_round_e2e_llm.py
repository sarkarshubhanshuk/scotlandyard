"""
End-to-end test driving detective_graph with REAL DeepSeek v4 calls over OpenRouter
through one full turn-wise round: five turns of proposal -> four responses -> decision,
then finalize (ADR-0009).

Unlike test_detective_loop_llm.py's run_test_round() (which only prints the round as it
unfolds), this asserts the structural invariants the pipeline must uphold regardless of what
the LLM actually said this run: every detective took exactly one turn, no two ended up on the
same final node, every committed move survives into final_moves unchanged, and every final
move was actually legally reachable from that detective's OWN starting position (recomputed
independently via board.compute_valid_moves rather than trusting the graph's own
bookkeeping).

Assertion 4 is the one that catches a whole class of regression: it would have failed the old
MCP tool-result unwrapping bug, which silently reverted every detective to its CURRENT node -
a node compute_valid_moves never lists as a reachable target.

Non-deterministic and costs real OpenRouter API calls. A round is 30 sequential calls
(5 detectives x 6 calls each), so expect a couple of minutes. Run standalone:
    python test_full_round_e2e_llm.py
"""
import asyncio
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler

from scotland_yard.agents import responders_for
from scotland_yard.board import compute_valid_moves
from scotland_yard.round_resolver import detective_graph
from scotland_yard.rules_constants import DETECTIVE_IDS, NUM_DETECTIVES

# agents.py logs detective output (which routinely contains em-dashes and curly quotes
# from the model) through the logging module, whose stderr stream still defaults to the
# system codepage on Windows (e.g. cp1252). Reconfiguring both streams to UTF-8 with a
# replace fallback keeps a run alive regardless of what the model says - ISSUE-017.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import pytest

# Every test in this module makes real, billable OpenRouter calls.
pytestmark = pytest.mark.llm

LOG_PATH = Path(__file__).resolve().parent.parent / "llm_io_log_full_round_e2e.txt"

# agents.py's propose/debate/vote prompts all open with "You are {detective_id}." -
# recovering the speaker from that line lets us attribute every call (propose, debate,
# vote, and any future LLM call added to the pipeline) without threading explicit
# tags/metadata through every ainvoke() call in agents.py.
DETECTIVE_ID_PATTERN = re.compile(r"You are (Agent \w+)")


class LLMTranscriptLogger(AsyncCallbackHandler):
    """
    Records every LLM call's raw input messages and raw output, in the exact
    chronological order the callback manager fires them, and appends each as a
    timestamped entry to LOG_PATH. Turn-wise play (ADR-0009) issues all 30 of a round's calls
    sequentially, so the log reads in true chronological order - but the call-number labeling
    is kept regardless, so a START/END pair stays matchable if concurrency is reintroduced.
    """

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self._call_counter = 0
        self._pending: dict[UUID, tuple[int, str]] = {}
        self._lock = threading.Lock()
        self.log_path.write_text("", encoding="utf-8")  # fresh log for this test run

    @staticmethod
    def _extract_detective_id(text: str) -> str:
        match = DETECTIVE_ID_PATTERN.search(text)
        return match.group(1) if match else "UNKNOWN"

    def _append(self, text: str) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(text)

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        flat_messages = messages[0] if messages else []
        prompt_text = "\n\n".join(f"[{m.type}]\n{m.content}" for m in flat_messages)
        detective_id = self._extract_detective_id(prompt_text)

        with self._lock:
            self._call_counter += 1
            call_no = self._call_counter
            self._pending[run_id] = (call_no, detective_id)

        entry = (
            f"\n{'=' * 100}\n"
            f"CALL #{call_no} | {detective_id.upper()} | INPUT | "
            f"{datetime.now().isoformat(timespec='milliseconds')}\n"
            f"{'=' * 100}\n"
            f"{prompt_text}\n"
        )
        self._append(entry)

    async def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        with self._lock:
            call_no, detective_id = self._pending.pop(run_id, (0, "UNKNOWN"))

        output_text = ""
        try:
            message = response.generations[0][0].message
            output_text = message.content or ""
            tool_calls = getattr(message, "tool_calls", None)
            if tool_calls:
                output_text += f"\n[tool_calls]\n{tool_calls}"
        except Exception:
            output_text = str(response)

        entry = (
            f"\n{'-' * 100}\n"
            f"CALL #{call_no} | {detective_id.upper()} | OUTPUT | "
            f"{datetime.now().isoformat(timespec='milliseconds')}\n"
            f"{'-' * 100}\n"
            f"{output_text}\n"
        )
        self._append(entry)

    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        with self._lock:
            call_no, detective_id = self._pending.pop(run_id, (0, "UNKNOWN"))

        entry = (
            f"\n{'-' * 100}\n"
            f"CALL #{call_no} | {detective_id.upper()} | ERROR | "
            f"{datetime.now().isoformat(timespec='milliseconds')}\n"
            f"{'-' * 100}\n"
            f"{error!r}\n"
        )
        self._append(entry)


llm_logger = LLMTranscriptLogger(LOG_PATH)
GRAPH_CONFIG = {"callbacks": [llm_logger]}

INITIAL_STATE = {
    "round_number": 3,
    "turn_index": 0,
    "mr_x": {
        "last_known_node": 13,
        "last_known_round": 3,
        "transport_history": ["taxi", "taxi", "bus"],
        "taxi_tickets": 10,
        "bus_tickets": 8,
        "metro_tickets": 4,
        "black_tickets": 5,
        "double_tickets": 2,
    },
    # Spread the detectives out across the map, same seed as test_detective_loop_llm.py's round.
    "detectives": {
        "agent_red": {"node_id": 29, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
        "agent_blue": {"node_id": 50, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
        "agent_green": {"node_id": 91, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
        "agent_orange": {"node_id": 117, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
        "agent_purple": {"node_id": 123, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
    },
    "messages": [],
    "committed_moves": {},
    "turn_records": {},
    "final_moves": {},
}


async def test_full_round_turns_and_finalize():
    print("\n=== TEST: full turn-wise round, five turns then finalize (real LLM) ===")

    starting_detectives = INITIAL_STATE["detectives"]
    starting_nodes = {d: info["node_id"] for d, info in starting_detectives.items()}

    result = await detective_graph.ainvoke(INITIAL_STATE, config=GRAPH_CONFIG)

    final_moves = result["final_moves"]
    committed_moves = result["committed_moves"]
    turn_records = result["turn_records"]

    print(f"turn_index={result['turn_index']}")
    print(f"committed_moves={committed_moves}")
    print(f"final_moves={final_moves}")

    # 1. Every detective got a final move out of the finalize stage.
    assert set(final_moves.keys()) == set(DETECTIVE_IDS), \
        f"expected final moves for all 5 detectives, got {sorted(final_moves.keys())}"

    # 2. No two detectives ended up on the same node - catches both a proposal/vote
    # collision slipping through AND a detective being finalized onto a node another
    # detective already locked this round.
    destinations = list(final_moves.values())
    assert len(destinations) == len(set(destinations)), \
        f"final_moves contains duplicate destinations: {final_moves}"

    # 3. Every committed move survived into final_moves unchanged - finalize assembles
    # committed_moves, it must never override a decision a detective already made.
    for det_id, committed_node in committed_moves.items():
        assert final_moves[det_id] == committed_node, \
            f"{det_id} committed to Node {committed_node} but finalized to Node {final_moves[det_id]}"

    # 4. Every final move is a node that was ACTUALLY legally reachable from that
    # detective's own starting position this round, recomputed independently of the
    # graph/MCP path (occupied = every OTHER detective's starting node, mirroring
    # fetch_legal_moves' own occupancy check - positions never change mid-round,
    # so the starting board state is the correct occupancy snapshot for the whole round).
    for det_id, final_node in final_moves.items():
        start_info = starting_detectives[det_id]
        other_starts = [n for d, n in starting_nodes.items() if d != det_id]
        legal = compute_valid_moves(
            start_info["node_id"], start_info["taxi_tickets"], start_info["bus_tickets"],
            start_info["metro_tickets"], 0, other_starts
        )
        legal_nodes = {m["target_node"] for m in legal}
        assert final_node in legal_nodes, (
            f"{det_id} finalized to Node {final_node}, which was not one of its legal "
            f"moves from Node {start_info['node_id']}: {sorted(legal_nodes)}"
        )

    # 5. Every detective took exactly one turn, and each turn produced the full six-call
    # record the Chat Log renders: a proposal, one response from each of the other four, and
    # a final decision.
    assert result["turn_index"] == NUM_DETECTIVES, \
        f"expected {NUM_DETECTIVES} turns, turn_index ended at {result['turn_index']}"
    assert set(turn_records) == set(DETECTIVE_IDS), \
        f"expected a turn record per detective, got {sorted(turn_records)}"
    for det_id, record in turn_records.items():
        responders = [r["responder"] for r in record["responses"]]
        assert responders == responders_for(det_id), \
            f"{det_id}'s turn was answered by {responders}, expected {responders_for(det_id)}"
        assert record["committed_node"] == final_moves[det_id]

    print("PASSED")


if __name__ == "__main__":  # pragma: no cover - manual, opt-in runner
    asyncio.run(test_full_round_turns_and_finalize())
    print(f"\nFull LLM input/output transcript written to: {LOG_PATH}")
