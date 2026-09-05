"""
End-to-end test driving detective_graph with REAL DeepSeek v4 calls over OpenRouter
through one full round: propose -> debate -> vote -> (loop up to 3x) -> finalize.

Unlike test_phase3.py's run_test_round() (which only prints the round as it unfolds),
this asserts the structural invariants the pipeline must uphold regardless of what the
LLM actually said this run: no two detectives end up on the same final node, every
locked_moves entry survives into final_moves unchanged, every final move was actually
legally reachable from that detective's OWN starting position (recomputed independently
via game_master.compute_valid_moves rather than trusting the graph's own bookkeeping),
and the consensus loop never exceeds its 3-loop cap. Assertions 2 and 4 together are
exactly what the MCP tool-result unwrapping bug (fixed in agents.py) would have failed:
it made propose_node silently revert every pending detective to its CURRENT node - which
compute_valid_moves never lists as a reachable target - instead of a legal move, and made
vote_node discard every vote as illegal.

Non-deterministic and costs real OpenRouter API calls - can take several minutes (5
detectives x up to 3 calls per loop x up to 3 loops). Run standalone:
    python test_full_round_e2e.py
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
from game_master import compute_valid_moves
from graph import detective_graph
from agents import DETECTIVE_NAMES

# agents.py's own print() calls (propose/debate/vote node console output) share this
# process's stdout, whose default encoding on Windows is the system codepage (e.g.
# cp1252) - not UTF-8. A detective's LLM-generated text routinely contains characters
# outside that codepage (em-dashes, curly quotes), which crashes the whole test with an
# unrelated UnicodeEncodeError before it ever reaches an assertion. Reconfiguring stdout
# to UTF-8 with a replace fallback keeps the run alive regardless of what the model says.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LOG_PATH = Path(__file__).resolve().parent / "llm_io_log_full_round_e2e.txt"

# agents.py's propose/debate/vote prompts all open with "You are {detective_id}." -
# recovering the speaker from that line lets us attribute every call (propose, debate,
# vote, and any future LLM call added to the pipeline) without threading explicit
# tags/metadata through every ainvoke() call in agents.py.
DETECTIVE_ID_PATTERN = re.compile(r"You are (detective_\d+)")


class LLMTranscriptLogger(AsyncCallbackHandler):
    """
    Records every LLM call's raw input messages and raw output, in the exact
    chronological order the callback manager fires them, and appends each as a
    timestamped entry to LOG_PATH. Calls made concurrently (propose_node/vote_node fire
    all 5 detectives via asyncio.gather) are labeled with a call number assigned at
    start time, so a START/END pair can still be matched up even with several calls in
    flight at once.
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
    "debate_loop_count": 0,
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
    # Spread the detectives out across the map, same seed as test_phase3.py's round.
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
    "final_moves": {},
}


async def test_full_round_propose_debate_vote_finalize():
    print("\n=== TEST: full round propose -> debate -> vote -> finalize (real LLM) ===")

    starting_detectives = INITIAL_STATE["detectives"]
    starting_nodes = {d: info["node_id"] for d, info in starting_detectives.items()}

    result = await detective_graph.ainvoke(INITIAL_STATE, config=GRAPH_CONFIG)

    final_moves = result["final_moves"]
    locked_moves = result["locked_moves"]
    debate_loop_count = result["debate_loop_count"]

    print(f"debate_loop_count={debate_loop_count}")
    print(f"locked_moves={locked_moves}")
    print(f"final_moves={final_moves}")

    # 1. Every detective got a final move out of the finalize stage.
    assert set(final_moves.keys()) == set(DETECTIVE_NAMES), \
        f"expected final moves for all 5 detectives, got {sorted(final_moves.keys())}"

    # 2. No two detectives ended up on the same node - catches both a proposal/vote
    # collision slipping through AND a detective being finalized onto a node another
    # detective already locked this round.
    destinations = list(final_moves.values())
    assert len(destinations) == len(set(destinations)), \
        f"final_moves contains duplicate destinations: {final_moves}"

    # 3. Every locked (>=3-vote) move survived into final_moves unchanged - finalize
    # must never override a passed vote.
    for det_id, locked_node in locked_moves.items():
        assert final_moves[det_id] == locked_node, \
            f"{det_id} locked at Node {locked_node} but finalized to Node {final_moves[det_id]}"

    # 4. Every final move is a node that was ACTUALLY legally reachable from that
    # detective's own starting position this round, recomputed independently of the
    # graph/MCP path (occupied = every OTHER detective's starting node, mirroring
    # propose_node/vote_node's own occupancy check - positions never change mid-round,
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

    # 5. The consensus loop never exceeds its 3-loop cap.
    assert debate_loop_count <= 3, f"debate_loop_count exceeded cap: {debate_loop_count}"

    print("PASSED")


if __name__ == "__main__":
    asyncio.run(test_full_round_propose_debate_vote_finalize())
    print(f"\nFull LLM input/output transcript written to: {LOG_PATH}")
