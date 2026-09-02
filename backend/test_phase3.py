import asyncio
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from graph import detective_graph, build_next_round_state

LOG_PATH = Path(__file__).resolve().parent / "llm_io_log.txt"

# agents.py's propose/debate/vote prompts all open with "You are {detective_id}." -
# recovering the speaker from that line lets us attribute every call without touching
# agents.py, since none of its ainvoke() calls thread through explicit tags/metadata.
DETECTIVE_ID_PATTERN = re.compile(r"You are (detective_\d+)")


class LLMTranscriptLogger(AsyncCallbackHandler):
    """
    Records every detective LLM call's raw input messages and raw output, in the exact
    chronological order the callback manager fires them, and appends each as a
    timestamped entry to LOG_PATH. Calls made concurrently (propose_node/vote_node fire
    all 5 detectives via asyncio.gather) are ordered by call-start order and labeled with
    a matching call number so a START/END pair can still be matched up even when several
    calls are in flight at once.
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


async def run_test_round():
    print("\n=== STARTING PHASE 3 END-TO-END TEST ===")
    print("Initializing Game State (Round 3 - Mr. X Revealed at Node 13)...")

    # Mock starting state for Round 3
    initial_state = {
        "round_number": 3,
        "debate_loop_count": 0,
        "mr_x": {
            "last_known_node": 13,
            "last_known_round": 3,
            "transport_history": ["taxi", "taxi", "bus"],
            "taxi_tickets": 10,
            "bus_tickets": 8,
            "metro_tickets": 4,
            "black_tickets": 5,
            "double_tickets": 2
        },
        # Spread the detectives out across the map
        "detectives": {
            "detective_1": {"node_id": 29, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
            "detective_2": {"node_id": 50, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
            "detective_3": {"node_id": 91, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
            "detective_4": {"node_id": 117, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
            "detective_5": {"node_id": 123, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
        },
        "messages": [],
        "proposed_strategies": {},
        "locked_moves": {},
        "final_moves": {}
    }

    print("\nFiring up the LangGraph Multi-Agent Engine...")

    # We use astream() to process the graph asynchronously and watch the steps unfold
    async for output in detective_graph.astream(initial_state, config=GRAPH_CONFIG):
        # Output is a dict showing which node just completed
        for node_name, state_update in output.items():
            print(f"\n[SYSTEM] --> Node '{node_name.upper()}' finished execution.")

    print("\n=== ROUND COMPLETE ===")


async def run_multi_round_reset_test():
    """
    Verifies the fix for Critical Issue 3: locked_moves/debate_loop_count/proposed_strategies
    must reset between rounds, or detectives locked in round 1 stay permanently locked and
    stop proposing/voting for the rest of the game.
    """
    print("\n=== MULTI-ROUND RESET TEST ===")

    initial_state = {
        "round_number": 3,
        "debate_loop_count": 0,
        "mr_x": {
            "last_known_node": 13,
            "last_known_round": 3,
            "transport_history": ["taxi", "taxi", "bus"],
            "taxi_tickets": 10,
            "bus_tickets": 8,
            "metro_tickets": 4,
            "black_tickets": 5,
            "double_tickets": 2
        },
        "detectives": {
            "detective_1": {"node_id": 29, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
            "detective_2": {"node_id": 50, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
            "detective_3": {"node_id": 91, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
            "detective_4": {"node_id": 117, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
            "detective_5": {"node_id": 123, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
        },
        "messages": [],
        "proposed_strategies": {},
        "locked_moves": {},
        "final_moves": {}
    }

    print("\n--- Running ROUND 1 ---")
    round_1_result = await detective_graph.ainvoke(initial_state, config=GRAPH_CONFIG)
    print(f"\nRound 1 finished. locked_moves={round_1_result['locked_moves']}, "
          f"debate_loop_count={round_1_result['debate_loop_count']}, "
          f"final_moves={round_1_result['final_moves']}")

    round_2_initial_state = build_next_round_state(round_1_result)
    assert round_2_initial_state["locked_moves"] == {}, "Round 2 must start with no locked moves"
    assert round_2_initial_state["debate_loop_count"] == 0, "Round 2 must start at loop 0"
    assert round_2_initial_state["proposed_strategies"] == {}, "Round 2 must start with no stale proposals"
    print(f"\nBuilt Round 2 initial state: round_number={round_2_initial_state['round_number']}, "
          f"locked_moves={round_2_initial_state['locked_moves']} (reset), "
          f"debate_loop_count={round_2_initial_state['debate_loop_count']} (reset)")

    print("\n--- Running ROUND 2 ---")
    round_2_result = await detective_graph.ainvoke(round_2_initial_state, config=GRAPH_CONFIG)
    print(f"\nRound 2 finished. final_moves={round_2_result['final_moves']}")

    print("\n=== MULTI-ROUND RESET TEST PASSED ===")


if __name__ == "__main__":
    asyncio.run(run_test_round())
    asyncio.run(run_multi_round_reset_test())
    print(f"\nFull LLM input/output transcript written to: {LOG_PATH}")
