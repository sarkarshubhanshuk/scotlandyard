/**
 * useMrXMoveWizard: the human player's single/double-move state machine.
 *
 * The double-move path is the reason this file exists. Hop 1 is locked in locally and its
 * options for hop 2 are fetched from a position Mr. X is not standing on yet, so there is a
 * window where the client holds a move the server has never seen - and a failure inside that
 * window has to unwind cleanly, or the player is stranded holding half a move they cannot
 * submit or cancel.
 */
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PAWN_MOVE_DURATION_MS } from "../board/boardDimensions";
import { makeGameState } from "../test/fixtures";
import type { LegalMove, PublicGameState } from "../types";
import { ApiError } from "../api/client";
import { useMrXMoveWizard } from "./useMrXMoveWizard";

type Hop2Preview = { fromNode: number; ticketTypeSpent: string };

const getMrXLegalMoves =
  vi.fn<(gameId: string, hop2Preview?: Hop2Preview) => Promise<LegalMove[]>>();
const submitMrXMove = vi.fn<(gameId: string, move: unknown) => Promise<PublicGameState>>();

// The stand-in ApiError is declared INSIDE the factory: vi.mock is hoisted above the module
// body, so a class defined out here would not exist yet when the factory runs. The hook checks
// `err instanceof ApiError`, so the test has to throw the very class the hook imports - which
// it gets by importing it back from the mocked module below.
vi.mock("../api/client", () => {
  class ApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.status = status;
    }
  }
  return {
    ApiError,
    getMrXLegalMoves: (gameId: string, hop2Preview?: Hop2Preview) =>
      getMrXLegalMoves(gameId, hop2Preview),
    submitMrXMove: (gameId: string, move: unknown) => submitMrXMove(gameId, move),
  };
});

const HOP1_OPTIONS: LegalMove[] = [
  { target_node: 14, ticket_options: ["taxi", "black"] },
  { target_node: 23, ticket_options: ["bus", "black"] },
];
const HOP2_OPTIONS: LegalMove[] = [{ target_node: 15, ticket_options: ["taxi"] }];

/** Renders the wizard on Mr. X's turn and waits for its opening legal-move fetch to land. */
async function renderWizard(initial?: PublicGameState) {
  const state = initial ?? makeGameState({ status: "awaiting_mr_x_move" });
  const onMoved = vi.fn();
  const view = renderHook(() => useMrXMoveWizard("test-game", state, onMoved));
  await waitFor(() => expect(view.result.current.legalMoves.length).toBeGreaterThan(0));
  return { view, onMoved, state };
}

beforeEach(() => {
  getMrXLegalMoves.mockReset().mockResolvedValue(HOP1_OPTIONS);
  submitMrXMove.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("selecting a destination", () => {
  it("offers only the tickets that can actually pay for the clicked node", async () => {
    const { view } = await renderWizard();

    act(() => view.result.current.handleNodeClick(23));

    expect(view.result.current.pendingTarget).toEqual({
      target: 23,
      ticketOptions: ["bus", "black"],
    });
  });

  it("ignores a click on a node that is not a legal destination", async () => {
    const { view } = await renderWizard();

    act(() => view.result.current.handleNodeClick(999));

    expect(view.result.current.pendingTarget).toBeNull();
  });

  it("highlights exactly the legal destinations", async () => {
    const { view } = await renderWizard();
    expect(view.result.current.highlightedNodeIds).toEqual([14, 23]);
  });
});

describe("a single move", () => {
  it("submits the chosen node and ticket, and reports the new state up", async () => {
    const moved = makeGameState({ status: "detective_loop_running" });
    submitMrXMove.mockResolvedValue(moved);
    const { view, onMoved } = await renderWizard();

    act(() => view.result.current.handleNodeClick(14));
    await act(async () => {
      await view.result.current.chooseTicket("taxi");
    });

    expect(submitMrXMove).toHaveBeenCalledWith("test-game", {
      move_type: "single",
      target_node: 14,
      ticket_type_spent: "taxi",
    });
    expect(onMoved).toHaveBeenCalledWith(moved);
    expect(view.result.current.pendingTarget).toBeNull();
  });

  it("surfaces a rejected move instead of failing silently", async () => {
    submitMrXMove.mockRejectedValue(new ApiError("Node 14 is occupied by a detective.", 400));
    const { view, onMoved } = await renderWizard();

    act(() => view.result.current.handleNodeClick(14));
    await act(async () => {
      await view.result.current.chooseTicket("taxi");
    });

    // The server's own message - this is the only place a submit failure reaches the player.
    expect(view.result.current.error).toBe("Node 14 is occupied by a detective.");
    expect(onMoved).not.toHaveBeenCalled();
    expect(view.result.current.submitting).toBe(false);
  });
});

describe("a double move", () => {
  it("locks hop 1 and re-fetches the options from that hypothetical position", async () => {
    const { view } = await renderWizard();

    act(() => view.result.current.setDoubleMode(true));
    act(() => view.result.current.handleNodeClick(14));
    getMrXLegalMoves.mockResolvedValue(HOP2_OPTIONS);
    await act(async () => {
      await view.result.current.chooseTicket("taxi");
    });

    expect(view.result.current.hop1).toEqual({ target: 14, ticket: "taxi" });
    // Hop 2's options must come from hop 1's TARGET with hop 1's ticket already spent - not
    // from Mr. X's live position and inventory, or spending the same scarce ticket type twice
    // would be offered and then rejected on submit.
    expect(getMrXLegalMoves).toHaveBeenLastCalledWith("test-game", {
      fromNode: 14,
      ticketTypeSpent: "taxi",
    });
    expect(view.result.current.highlightedNodeIds).toEqual([15]);
  });

  it("rolls hop 1 back if its follow-up fetch fails, rather than stranding half a move", async () => {
    const { view } = await renderWizard();

    act(() => view.result.current.setDoubleMode(true));
    act(() => view.result.current.handleNodeClick(14));
    getMrXLegalMoves.mockRejectedValue(new ApiError("No double-move tickets remaining.", 400));
    await act(async () => {
      await view.result.current.chooseTicket("taxi");
    });

    // Hop 1 was never submitted to the server, so the client must not keep pretending it holds
    // one - otherwise the player is stuck in a double-move they can neither finish nor cancel.
    expect(view.result.current.hop1).toBeNull();
    expect(view.result.current.error).toBe("No double-move tickets remaining.");
    expect(submitMrXMove).not.toHaveBeenCalled();
  });

  it("submits both hops in one request", async () => {
    submitMrXMove.mockResolvedValue(makeGameState({ status: "detective_loop_running" }));
    const { view } = await renderWizard();

    act(() => view.result.current.setDoubleMode(true));
    act(() => view.result.current.handleNodeClick(14));
    getMrXLegalMoves.mockResolvedValue(HOP2_OPTIONS);
    await act(async () => {
      await view.result.current.chooseTicket("taxi");
    });
    act(() => view.result.current.handleNodeClick(15));
    await act(async () => {
      await view.result.current.chooseTicket("taxi");
    });

    // Atomic: there is no cross-request "pending double-move" state on the server, so both
    // hops go in one body or neither does.
    expect(submitMrXMove).toHaveBeenCalledWith("test-game", {
      move_type: "double",
      hop1: { target_node: 14, ticket_type_spent: "taxi" },
      hop2: { target_node: 15, ticket_type_spent: "taxi" },
    });
  });

  it("cannot switch modes once hop 1 is locked in", async () => {
    const { view } = await renderWizard();

    act(() => view.result.current.setDoubleMode(true));
    act(() => view.result.current.handleNodeClick(14));
    getMrXLegalMoves.mockResolvedValue(HOP2_OPTIONS);
    await act(async () => {
      await view.result.current.chooseTicket("taxi");
    });

    act(() => view.result.current.setDoubleMode(false));
    expect(view.result.current.doubleMode).toBe(true);
  });

  it("cancelling hop 1 clears the whole in-progress pick", async () => {
    const { view } = await renderWizard();

    act(() => view.result.current.setDoubleMode(true));
    act(() => view.result.current.handleNodeClick(14));
    getMrXLegalMoves.mockResolvedValue(HOP2_OPTIONS);
    await act(async () => {
      await view.result.current.chooseTicket("taxi");
    });

    act(() => view.result.current.cancelHop1());

    expect(view.result.current.hop1).toBeNull();
    expect(view.result.current.hop2Options).toBeNull();
    expect(view.result.current.pendingTarget).toBeNull();
    // Back to hop 1's own options.
    expect(view.result.current.highlightedNodeIds).toEqual([14, 23]);
  });
});

describe("a double move on a surfacing round", () => {
  it("shows the intermediate node first, then the hidden destination", async () => {
    vi.useFakeTimers();
    // The server reveals only the INTERMEDIATE node on a surfacing round, so last_known_node
    // is hop 1's target while current_node is the still-hidden final destination.
    const settled = makeGameState({
      status: "detective_loop_running",
      round_number: 3,
      mr_x: {
        ...makeGameState().mr_x,
        current_node: 15,
        last_known_node: 14,
        last_known_round: 3,
      },
    });
    submitMrXMove.mockResolvedValue(settled);

    const state = makeGameState({ status: "awaiting_mr_x_move", round_number: 3 });
    const onMoved = vi.fn();
    const view = renderHook(() => useMrXMoveWizard("test-game", state, onMoved));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    act(() => view.result.current.setDoubleMode(true));
    act(() => view.result.current.handleNodeClick(14));
    getMrXLegalMoves.mockResolvedValue(HOP2_OPTIONS);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
      await view.result.current.chooseTicket("taxi");
    });
    act(() => view.result.current.handleNodeClick(15));

    const submitted = act(async () => {
      await view.result.current.chooseTicket("taxi");
    });
    // The pawn has to visibly stop at the revealed intermediate node before continuing to the
    // hidden one - a single update would tween it straight past the only node the detectives
    // were ever shown.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PAWN_MOVE_DURATION_MS * 2);
    });
    await submitted;

    expect(onMoved).toHaveBeenCalledTimes(2);
    const first = onMoved.mock.calls[0][0] as PublicGameState;
    expect(first.mr_x.current_node).toBe(14);
    // Held at awaiting_mr_x_move so the detective round does not start on the intermediate leg.
    expect(first.status).toBe("awaiting_mr_x_move");
    expect(onMoved.mock.calls[1][0]).toEqual(settled);
  });
});
