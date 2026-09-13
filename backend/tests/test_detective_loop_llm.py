import asyncio
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from scotland_yard.graph import build_next_round_state
from scotland_yard.round_resolver import detective_graph
from scotland_yard.rules_constants import DETECTIVE_IDS, NUM_DETECTIVES

import pytest

# Every test in this module makes real, billable OpenRouter calls.
pytestmark = pytest.mark.llm

LOG_PATH = Path(__file__).resolve().parent.parent / "llm_io_log.txt"

# agents.py's proposal/response/decision prompts all open with "You are {display_name}." -
# recovering the speaker from that line lets us attribute every call without touching
# agents.py, since none of its ainvoke() calls thread through explicit tags/metadata.
DETECTIVE_ID_PATTERN = re.compile(r"You are (Agent \w+)")


class LLMTranscriptLogger(AsyncCallbackHandler):
    """
    Records every detective LLM call's raw input messages and raw output, in the exact
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
        prompt_text = "\n\n".join(
            f"[{m.type}]\n{m.content}" for m in flat_messages
        )
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


def build_round_3_state() -> dict:
    """
    A round-3 board with Mr. X freshly revealed at Node 13 and the detectives spread out, so
    the possible-zone context is populated and every detective has real options to argue over.
    """
    return {
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
        # Spread the detectives out across the map
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


async def run_test_round():
    print("\n=== TURN-WISE ROUND END-TO-END TEST ===")
    print("Initializing Game State (Round 3 - Mr. X Revealed at Node 13)...")

    # Mock starting state for Round 3
    initial_state = build_round_3_state()

    print("\nFiring up the LangGraph Multi-Agent Engine...")

    # We use astream() to process the graph asynchronously and watch the steps unfold
    async for output in detective_graph.astream(initial_state, config=GRAPH_CONFIG):
        # Output is a dict showing which node just completed
        for node_name, state_update in output.items():
            print(f"\n[SYSTEM] --> Node '{node_name.upper()}' finished execution.")

    print("\n=== ROUND COMPLETE ===")


async def run_multi_round_reset_test():
    """
    Verifies that the per-round turn fields reset between rounds. Left unreset, turn_index
    would already be at NUM_DETECTIVES when round 2 starts, so the graph would route straight
    to finalize and replay round 1's committed nodes as every future round's move.
    """
    print("\n=== MULTI-ROUND RESET TEST ===")

    initial_state = build_round_3_state()

    print("\n--- Running ROUND 1 ---")
    round_1_result = await detective_graph.ainvoke(initial_state, config=GRAPH_CONFIG)
    print(f"\nRound 1 finished. committed_moves={round_1_result['committed_moves']}, "
          f"turn_index={round_1_result['turn_index']}, "
          f"final_moves={round_1_result['final_moves']}")
    assert round_1_result["turn_index"] == NUM_DETECTIVES, "Every detective must have taken a turn"
    assert set(round_1_result["turn_records"]) == set(DETECTIVE_IDS)

    round_2_initial_state = build_next_round_state(round_1_result)
    assert round_2_initial_state["turn_index"] == 0, "Round 2 must start at Agent Red's turn"
    assert round_2_initial_state["committed_moves"] == {}, "Round 2 must start with nothing committed"
    assert round_2_initial_state["turn_records"] == {}, "Round 2 must start with no stale turn records"
    print(f"\nBuilt Round 2 initial state: round_number={round_2_initial_state['round_number']}, "
          f"turn_index={round_2_initial_state['turn_index']} (reset), "
          f"committed_moves={round_2_initial_state['committed_moves']} (reset)")

    print("\n--- Running ROUND 2 ---")
    round_2_result = await detective_graph.ainvoke(round_2_initial_state, config=GRAPH_CONFIG)
    print(f"\nRound 2 finished. final_moves={round_2_result['final_moves']}")

    print("\n=== MULTI-ROUND RESET TEST PASSED ===")


if __name__ == "__main__":  # pragma: no cover - manual, opt-in runner
    asyncio.run(run_test_round())
    asyncio.run(run_multi_round_reset_test())
    print(f"\nFull LLM input/output transcript written to: {LOG_PATH}")
