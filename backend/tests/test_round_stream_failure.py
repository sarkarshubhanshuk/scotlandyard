"""
What the round stream does when the round itself fails.

By the time the detective loop is running, the SSE response has already begun - the status
line and headers are long gone, so there is no HTTP error code left to send. An exception
escaping the loop therefore used to do nothing except end the stream, which reaches the browser
as a bare `onerror`: indistinguishable from a network blip, and giving the player no way to tell
"your wifi dropped" from "this deployment has no API key". The failure has to become an EVENT.

The loop is stubbed here rather than run: what is under test is the route's failure handling,
not any particular way of failing, so the stub just raises.
"""
import httpx
import pytest

from scotland_yard import server
from scotland_yard.server import PLAYER_COOKIE
from scotland_yard.session import GAMES, create_game

from .conftest import SEED_POSITIONS

OWNER_TOKEN = "test-owner-token"


@pytest.fixture
def failing_round(monkeypatch):
    """A game mid-round whose detective loop raises partway through."""
    GAMES.clear()
    session = create_game(seed_positions=SEED_POSITIONS)
    session.owner_token = OWNER_TOKEN
    session.status = "detective_loop_running"

    async def exploding_loop(sess):
        # One good event first, so the test proves the error is handled mid-stream rather than
        # only before anything has been written.
        yield {"type": "turn_event", "payload": {"event": "turn_started", "detective": "agent_red"}}
        raise RuntimeError("the detectives fell over")

    monkeypatch.setattr(server, "run_detective_loop", exploding_loop)
    yield session
    GAMES.clear()


async def _event_names_and_data(session) -> tuple[list[str], list[str]]:
    transport = httpx.ASGITransport(app=server.app)
    names: list[str] = []
    data: list[str] = []
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver", cookies={PLAYER_COOKIE: OWNER_TOKEN}
    ) as client:
        async with client.stream("GET", f"/games/{session.game_id}/round/stream") as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    names.append(line.split(":", 1)[1].strip())
                elif line.startswith("data:"):
                    data.append(line.split(":", 1)[1].strip())
    return names, data


async def test_a_failing_round_ends_in_a_round_error_event(failing_round):
    names, data = await _event_names_and_data(failing_round)

    assert names[-1] == "round_error", (
        f"the stream must end by SAYING it failed, not by just stopping; got {names}")
    assert "turn_started" in names, "events emitted before the failure should still reach the client"
    assert "round_result" not in names, "a failed round must not also report a result"
    # The message is what the player actually reads, and it is deliberately generic - the real
    # exception text belongs in the server log, not on a public unauthenticated endpoint.
    assert "the detectives fell over" not in " ".join(data), (
        "the raw exception must not be leaked to the client")


async def test_a_failed_round_leaves_the_game_retryable_and_untouched(failing_round):
    round_before = failing_round.state["round_number"]
    mr_x_before = dict(failing_round.state["mr_x"])
    detectives_before = {d: dict(v) for d, v in failing_round.state["detectives"].items()}

    await _event_names_and_data(failing_round)

    # Status is deliberately NOT reset: leaving it here is what lets the client simply reopen
    # the stream. run_detective_loop only writes session.state back once the graph completes, so
    # there is no half-moved board to resume into - a retry replays the round from the start.
    assert failing_round.status == "detective_loop_running"
    assert failing_round.state["round_number"] == round_before
    assert failing_round.state["mr_x"] == mr_x_before
    assert failing_round.state["detectives"] == detectives_before


async def test_the_round_can_actually_be_retried_after_a_failure(failing_round, monkeypatch):
    """The point of leaving the session alone: the next subscriber gets a clean round."""
    await _event_names_and_data(failing_round)

    async def working_loop(sess):
        yield {"type": "turn_event", "payload": {"event": "turn_started", "detective": "agent_red"}}

    def fake_resolve(sess):
        sess.status = "awaiting_mr_x_move"
        sess.state["round_number"] += 1
        return {"status": sess.status, "winner": None, "round_number": sess.state["round_number"]}

    monkeypatch.setattr(server, "run_detective_loop", working_loop)
    monkeypatch.setattr(server, "resolve_round", fake_resolve)

    names, _ = await _event_names_and_data(failing_round)
    assert names[-1] == "round_result"
    assert failing_round.status == "awaiting_mr_x_move"
