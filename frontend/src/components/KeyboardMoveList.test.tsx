/**
 * KeyboardMoveList: the only way to make a move without a mouse.
 *
 * The board is a Phaser canvas (ADR-0015), so its nodes are unreachable by keyboard and
 * invisible to assistive technology. Everything here is about that: the buttons have to exist
 * in the DOM, stay focusable while visually hidden, and describe a complete action on their own.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { LegalMove } from "../types";
import { KeyboardMoveList } from "./KeyboardMoveList";

const MOVES: LegalMove[] = [
  { target_node: 14, ticket_options: ["taxi", "black"] },
  { target_node: 23, ticket_options: ["bus"] },
];

function renderList(overrides: Partial<Parameters<typeof KeyboardMoveList>[0]> = {}) {
  const onChoose = vi.fn();
  render(
    <KeyboardMoveList
      legalMoves={MOVES}
      isMrXTurn
      submitting={false}
      hopLabel={null}
      onChoose={onChoose}
      {...overrides}
    />,
  );
  return { onChoose };
}

describe("what it offers", () => {
  it("renders one complete action per destination and ticket pair", () => {
    renderList();

    // Self-describing: a screen-reader user hears the whole move, not "node 14" followed by a
    // mode change that alters what the next keypress means.
    expect(screen.getByRole("button", { name: "Node 14 by Taxi" })).toBeDefined();
    expect(screen.getByRole("button", { name: "Node 14 by Black" })).toBeDefined();
    expect(screen.getByRole("button", { name: "Node 23 by Bus" })).toBeDefined();
    expect(screen.getAllByRole("button")).toHaveLength(3);
  });

  it("reports the chosen node and ticket together", () => {
    const { onChoose } = renderList();

    screen.getByRole("button", { name: "Node 14 by Black" }).click();

    expect(onChoose).toHaveBeenCalledWith(14, "black");
  });

  it("names the hop when a double move is in progress", () => {
    renderList({ hopLabel: "Double move, hop 2" });
    expect(screen.getByRole("heading").textContent).toContain("Double move, hop 2");
  });

  it("counts the destinations in its heading", () => {
    renderList();
    expect(screen.getByRole("heading").textContent).toContain("2 legal destinations");
  });

  it("does not say 'destinations' for a single one", () => {
    renderList({ legalMoves: [MOVES[1]] });
    expect(screen.getByRole("heading").textContent).toContain("1 legal destination");
    expect(screen.getByRole("heading").textContent).not.toContain("destinations");
  });
});

describe("when it should not be there at all", () => {
  it("renders nothing between turns", () => {
    renderList({ isMrXTurn: false });
    // An empty but focusable region would be a tab stop that says nothing.
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("renders nothing when there are no legal moves", () => {
    renderList({ legalMoves: [] });
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("disables every action while a move is in flight", () => {
    renderList({ submitting: true });
    for (const button of screen.getAllByRole("button")) {
      expect((button as HTMLButtonElement).disabled).toBe(true);
    }
  });
});

describe("staying reachable", () => {
  it("is labelled by its own heading", () => {
    renderList();
    // Without this the region is an unnamed landmark - reachable, but not identifiable.
    const region = screen.getByRole("region", { name: /legal destination/ });
    expect(region).toBeDefined();
  });

  it("uses a clipping style rather than one that would remove it from the tab order", () => {
    renderList();
    const region = screen.getByRole("region", { name: /legal destination/ });
    // display:none and the hidden attribute both make an element unfocusable, which would
    // defeat the entire purpose. The CSS class is what hides it; assert we did not also do
    // either of those.
    expect(region.hasAttribute("hidden")).toBe(false);
    expect(region.style.display).not.toBe("none");
    expect(region.className).toBe("sy-keyboard-moves");
  });
});
