"""
Regression tests for ISSUE-027: the round/stream endpoint's check-then-act race.

The status guard used to sit entirely OUTSIDE the session lock. Two concurrent subscribers
both passed it; the second then blocked on the lock, and once the first finished - having run
the whole detective loop AND called resolve_round, which advances the round and flips status
back to awaiting_mr_x_move - the second went on to run a second full detective loop against
the NEXT round's state, with Mr. X having never moved.

React StrictMode's dev-mode double-invoke of effects opens exactly two EventSources, so this
was routine rather than theoretical, and each spurious loop is ~15 billable LLM calls.

These tests stub run_detective_loop entirely - the race is in the route's locking, not in the
loop, and stubbing keeps this fast, deterministic, and free.
"""
import asyncio

import httpx
import pytest

from scotland_yard import server
from scotland_yard.session import GAMES, create_game

from .conftest import SEED_POSITIONS


@pytest.fixture
def running_game(monkeypatch):
    """
    A game parked in `detective_loop_running`, with the detective loop stubbed out by a
    slow-but-trivial coroutine and a resolver that just advances the round.

    `loop_runs` counts how many times the detective loop actually executed - the number this
    whole test module exists to pin at 1.
    """
    GAMES.clear()
    session = create_game(seed_positions=SEED_POSITIONS)
    session.status = "detective_loop_running"
    counters = {"loop_runs": 0, "resolves": 0}

    async def fake_loop(sess):
        counters["loop_runs"] += 1
        # Long enough that a second request reliably arrives while this one holds the lock.
        await asyncio.sleep(0.05)
        yield {"type": "stage_started", "stage": "proposal"}

    def fake_resolve(sess):
        counters["resolves"] += 1
        sess.status = "awaiting_mr_x_move"
        sess.state["round_number"] += 1
        return {"status": sess.status, "winner": None, "round_number": sess.state["round_number"]}

    monkeypatch.setattr(server, "run_detective_loop", fake_loop)
    monkeypatch.setattr(server, "resolve_round", fake_resolve)
    yield session, counters
    GAMES.clear()


async def _consume(client: httpx.AsyncClient, game_id: str) -> list[str]:
    """Opens the SSE stream and returns the list of event names it emitted."""
    events = []
    async with client.stream("GET", f"/games/{game_id}/round/stream") as response:
        assert response.status_code == 200
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                events.append(line.split(":", 1)[1].strip())
    return events


async def test_two_concurrent_subscribers_run_the_loop_exactly_once(running_game):
    session, counters = running_game
    transport = httpx.ASGITransport(app=server.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first, second = await asyncio.gather(
            _consume(client, session.game_id),
            _consume(client, session.game_id),
        )

    assert counters["loop_runs"] == 1, (
        f"detective loop ran {counters['loop_runs']} times for one round - "
        "the second subscriber re-ran it (ISSUE-027)"
    )
    assert counters["resolves"] == 1

    # Exactly one subscriber drove the round; the other was told it was already resolved.
    outcomes = sorted([first[-1], second[-1]])
    assert outcomes == ["round_already_resolved", "round_result"]


async def test_the_late_subscriber_still_receives_the_current_state(running_game):
    """
    Being told "already resolved" must not leave that client stranded - it gets a fresh state
    snapshot, the same way round_result carries one, so the UI can resynchronise.
    """
    import json

    session, _ = running_game
    transport = httpx.ASGITransport(app=server.app)

    async def consume_raw(client):
        payloads = []
        async with client.stream("GET", f"/games/{session.game_id}/round/stream") as response:
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    payloads.append(json.loads(line.split(":", 1)[1].strip()))
        return payloads

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        results = await asyncio.gather(consume_raw(client), consume_raw(client))

    late = next(
        payloads for payloads in results
        if payloads and payloads[-1].get("type") == "round_already_resolved"
    )
    assert "state" in late[-1]
    assert late[-1]["state"]["round_number"] == session.state["round_number"]


async def test_a_stream_request_outside_the_detective_loop_is_still_rejected_early(running_game):
    """The cheap outer guard still does its job: no stream is opened at all when it's not time."""
    session, counters = running_game
    session.status = "awaiting_mr_x_move"

    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/games/{session.game_id}/round/stream")

    assert response.status_code == 409
    assert counters["loop_runs"] == 0
