/**
 * useRoundStream: the SSE round stream, and the game state it mirrors as events arrive.
 *
 * This hook duplicates rules the backend also implements - which ticket a detective's move
 * transfers to Mr. X, which pawn an ack belongs to - because the board has to stay live
 * mid-round rather than waiting for the round to resolve (ADR-0010). Duplicated rules drift,
 * and nothing else in the toolchain would notice: eslint cannot see a wrong ticket.
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PAWN_MOVE_DURATION_MS } from "../board/boardDimensions";
import { FakeEventSource, makeGameState } from "../test/fixtures";
import type { PublicGameState } from "../types";
import { useRoundStream } from "./useRoundStream";

const postTurnAck =
  vi.fn<(gameId: string, round: number, detective: string) => Promise<{ applied: boolean }>>(
    () => Promise.resolve({ applied: true }),
  );

vi.mock("../api/client", () => ({
  openRoundStream: (gameId: string) => new FakeEventSource("/games/" + gameId + "/round/stream"),
  postTurnAck: (gameId: string, round: number, detective: string) =>
    postTurnAck(gameId, round, detective),
}));

/**
 * Renders the hook and opens its stream. The hook deliberately delays opening by
 * PAWN_MOVE_DURATION_MS so Mr. X's own pawn finishes moving first (ADR-0010), so every test
 * has to advance past that before any event can be delivered.
 */
function openStream(initial?: PublicGameState) {
  let state = initial ?? makeGameState();
  const onGameStateChange = vi.fn((update: unknown) => {
    state =
      typeof update === "function"
        ? (update as (p: PublicGameState) => PublicGameState)(state)
        : (update as PublicGameState);
  });

  const view = renderHook(() => useRoundStream("test-game", state, onGameStateChange));
  act(() => {
    vi.advanceTimersByTime(PAWN_MOVE_DURATION_MS);
  });
  return { view, source: FakeEventSource.last!, latest: () => state };
}

const decision = (overrides: Record<string, unknown> = {}) => ({
  type: "turn_decision",
  detective: "agent_red",
  from_node: 26,
  target_node: 27,
  transport: "taxi",
  rationale: "Closing on the zone.",
  captured: false,
  ...overrides,
});

beforeEach(() => {
  vi.useFakeTimers();
  postTurnAck.mockClear();
  FakeEventSource.last = null;
});

afterEach(() => {
  vi.useRealTimers();
});

describe("chat log", () => {
  it("appends one entry per LLM call, tagged with the turn it belongs to", () => {
    const { view, source } = openStream();

    act(() => {
      source.emit("turn_started", { type: "turn_started", detective: "agent_red" });
      source.emit("turn_proposal", {
        type: "turn_proposal",
        detective: "agent_red",
        target_node: 27,
        rationale: "Best distance.",
      });
      source.emit("turn_response", {
        type: "turn_response",
        detective: "agent_blue",
        responding_to: "agent_red",
        response: "Agreed.",
        preferred_node: 30,
      });
      source.emit("turn_decision", decision());
    });

    const entries = view.result.current.entries;
    // turn_started carries no message of its own - it only moves the header.
    expect(entries.map((e) => e.kind)).toEqual(["proposal", "response", "decision"]);
    // A response from Blue during Red's turn belongs to RED's turn - that grouping is what
    // makes ChatLog render a round as five turns rather than one flat stream.
    expect(entries[1].turn).toBe("agent_red");
    expect(entries[1].speaker).toBe("agent_blue");
  });

  it("only reports a game over, not an ordinary round continuing", () => {
    const { view, source } = openStream();
    act(() => {
      source.emit("round_result", {
        type: "round_result",
        status: "awaiting_mr_x_move",
        winner: null,
        round_number: 2,
        state: makeGameState({ round_number: 2 }),
      });
    });
    expect(view.result.current.entries).toHaveLength(0);
  });
});

describe("mirroring an applied move", () => {
  it("moves the pawn and transfers the spent ticket to Mr. X", () => {
    const { source, latest } = openStream();
    const before = latest();

    act(() => {
      source.emit("turn_decision", decision({ transport: "taxi" }));
    });

    const after = latest();
    expect(after.detectives.agent_red.node_id).toBe(27);
    // Rules: a detective's spent ticket is transferred to Mr. X, not removed from the game.
    expect(after.detectives.agent_red.taxi_tickets).toBe(
      before.detectives.agent_red.taxi_tickets - 1,
    );
    expect(after.mr_x.taxi_tickets).toBe(before.mr_x.taxi_tickets + 1);
    // Only the spent type moves.
    expect(after.detectives.agent_red.bus_tickets).toBe(before.detectives.agent_red.bus_tickets);
    expect(after.detectives.agent_blue).toEqual(before.detectives.agent_blue);
  });

  it("transfers nothing when a detective could not move", () => {
    const { source, latest } = openStream();
    const before = latest();

    act(() => {
      source.emit("turn_decision", decision({ target_node: 26, transport: null }));
    });

    const after = latest();
    expect(after.detectives.agent_red).toEqual(before.detectives.agent_red);
    expect(after.mr_x).toEqual(before.mr_x);
  });

  it("applies five turns cumulatively rather than against the opening snapshot", () => {
    const { source, latest } = openStream();
    const before = latest();

    act(() => {
      source.emit(
        "turn_decision",
        decision({ detective: "agent_red", from_node: 26, target_node: 27 }),
      );
      source.emit(
        "turn_decision",
        decision({ detective: "agent_blue", from_node: 29, target_node: 30 }),
      );
    });

    const after = latest();
    expect(after.detectives.agent_red.node_id).toBe(27);
    expect(after.detectives.agent_blue.node_id).toBe(30);
    // Two detectives each spent one taxi ticket, so Mr. X gained two.
    expect(after.mr_x.taxi_tickets).toBe(before.mr_x.taxi_tickets + 2);
  });
});

describe("the pawn-settled ack", () => {
  it("acks the detective whose pawn just moved", () => {
    const { view, source } = openStream();

    act(() => {
      source.emit("turn_decision", decision({ detective: "agent_red" }));
    });
    act(() => {
      view.result.current.handlePawnSettled("agent_red");
    });

    expect(postTurnAck).toHaveBeenCalledWith("test-game", 1, "agent_red");
  });

  it("ignores a settle report for a pawn the round is not waiting on", () => {
    const { view, source } = openStream();

    act(() => {
      source.emit("turn_decision", decision({ detective: "agent_red" }));
    });
    act(() => {
      // Mr. X's pawn needs no ack, and a late ack for another pawn must never release the
      // turn the backend is actually waiting on.
      view.result.current.handlePawnSettled("mr_x");
    });

    expect(postTurnAck).not.toHaveBeenCalled();
  });

  it("acks immediately when the detective stayed put, since no tween will ever fire", () => {
    const { source } = openStream();

    act(() => {
      source.emit("turn_decision", decision({ target_node: 26, transport: null }));
    });

    expect(postTurnAck).toHaveBeenCalledWith("test-game", 1, "agent_red");
  });
});

describe("a round that fails server-side", () => {
  it("surfaces the server's own message and stops the stream", () => {
    const { view, source } = openStream();

    act(() => {
      source.emit("round_error", {
        type: "round_error",
        message: "The detectives hit an unexpected problem this round.",
        state: makeGameState(),
      });
    });

    // The server's wording, not the generic "lost connection" this used to render as.
    expect(view.result.current.connectionError).toBe(
      "The detectives hit an unexpected problem this round.",
    );
    // Closed deliberately, so EventSource does not reconnect behind the Retry button.
    expect(source.closed).toBe(true);
    expect(view.result.current.activeTurnDetective).toBeNull();
  });
});

describe("the turn halo", () => {
  it("follows the mover and comes off the instant its move starts animating", () => {
    const { view, source } = openStream();

    act(() => {
      source.emit("turn_started", { type: "turn_started", detective: "agent_green" });
    });
    expect(view.result.current.activeTurnDetective).toBe("agent_green");

    act(() => {
      source.emit(
        "turn_decision",
        decision({ detective: "agent_green", from_node: 34, target_node: 35 }),
      );
    });
    // Cleared by the same event that starts the pawn moving - the halo never tracks a pawn
    // in flight (ADR-0011).
    expect(view.result.current.activeTurnDetective).toBeNull();
  });
});
