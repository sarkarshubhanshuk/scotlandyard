/**
 * TicketInventory: the sidebar table, its per-ticket tooltips, and the active-turn row marker.
 *
 * The turn marker is the part worth testing rather than eyeballing: Mr. X's row can be checked in
 * a browser just by loading a game, but a detective's row only highlights partway through a
 * detective round - which costs a full round of billable LLM calls to reach. Same code path,
 * different id, so it is covered here instead.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AGENT_COLORS } from "../labels";
import { makeGameState } from "../test/fixtures";
import { TicketInventory } from "./TicketInventory";

const HEADERS = [
  "Taxi Tickets",
  "Bus Tickets",
  "Metro Tickets",
  "Black Tickets",
  "Double Moves",
];

function renderInventory(activeTurnPawnId: string | null = null) {
  return render(
    <TicketInventory gameState={makeGameState()} activeTurnPawnId={activeTurnPawnId} />,
  );
}

/** The <tr> a given player's name cell belongs to. */
function rowFor(name: string): HTMLTableRowElement {
  return screen.getByText(name).closest("tr") as HTMLTableRowElement;
}

describe("columns", () => {
  it("names every ticket type in full", () => {
    renderInventory();
    for (const header of HEADERS) {
      expect(screen.getByRole("columnheader", { name: header })).toBeDefined();
    }
  });

  it("shows a detective's missing ticket types as blank, not as zero", () => {
    renderInventory();
    const cells = within(rowFor("Agent Red")).getAllByRole("cell");
    // A 0 would read as "spent them all"; detectives never hold these at all (rules.md §1).
    expect(cells[4].textContent).toBe("-");
    expect(cells[5].textContent).toBe("-");
  });
});

describe("tooltips", () => {
  it("explains a ticket on hover and hides it again on mouse-out", () => {
    renderInventory();
    const label = screen.getByText("Taxi Tickets");

    expect(screen.queryByRole("tooltip")).toBeNull();
    fireEvent.mouseEnter(label);
    expect(screen.getByRole("tooltip").textContent).toBe("Pays for yellow Taxi routes.");
    fireEvent.mouseLeave(label);
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("opens on keyboard focus too, not only on hover", () => {
    renderInventory();
    const label = screen.getByText("Double Moves");

    // A hover-only tooltip is unreachable by keyboard, which would undo the point of the
    // board's own KeyboardMoveList.
    fireEvent.focus(label);
    expect(screen.getByRole("tooltip").textContent).toContain("Move twice in one turn");
    fireEvent.blur(label);
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("is dismissible with Escape without moving focus away", () => {
    renderInventory();
    const label = screen.getByText("Bus Tickets");

    fireEvent.focus(label);
    expect(screen.getByRole("tooltip")).toBeDefined();
    fireEvent.keyDown(label, { key: "Escape" });
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("ties the tooltip to its label for screen readers", () => {
    renderInventory();
    const label = screen.getByText("Metro Tickets");

    fireEvent.mouseEnter(label);
    // Without the association the popup is just floating text with no owner.
    expect(label.getAttribute("aria-describedby")).toBe(screen.getByRole("tooltip").id);
  });

  it("says what each ticket actually buys", () => {
    renderInventory();
    const expected: Record<string, string> = {
      "Taxi Tickets": "yellow Taxi routes",
      "Bus Tickets": "green Bus routes",
      "Metro Tickets": "red Metro routes",
      "Black Tickets": "Taxi, Bus, Metro or Boat",
      "Double Moves": "Move twice in one turn",
    };
    for (const [header, fragment] of Object.entries(expected)) {
      const label = screen.getByText(header);
      fireEvent.mouseEnter(label);
      expect(screen.getByRole("tooltip").textContent).toContain(fragment);
      fireEvent.mouseLeave(label);
    }
  });
});

describe("the active-turn row", () => {
  it("marks nobody between turns", () => {
    renderInventory(null);
    for (const name of ["Mr. X", "Agent Red", "Agent Purple"]) {
      expect(rowFor(name).style.background).toBe("");
    }
  });

  it("marks Mr. X while it is his move", () => {
    renderInventory("mr_x");
    expect(rowFor("Mr. X").style.background).not.toBe("");
    expect(rowFor("Agent Red").style.background).toBe("");
  });

  it("marks the detective currently taking its turn, and only that one", () => {
    renderInventory("agent_green");
    expect(rowFor("Agent Green").style.background).not.toBe("");
    for (const name of ["Mr. X", "Agent Red", "Agent Blue", "Agent Orange", "Agent Purple"]) {
      expect(rowFor(name).style.background).toBe("");
    }
  });

  it("colours the marker bar to match that player's own pawn", () => {
    renderInventory("agent_green");
    const nameCell = screen.getByText("Agent Green").closest("td") as HTMLTableCellElement;
    // Derived from AGENT_COLORS rather than written out, so this asserts the bar matches the
    // value the board actually tints the pawn with - not that it equals some literal that could
    // drift away from it. jsdom normalises the hex to rgb(), hence the conversion.
    const green = AGENT_COLORS.agent_green;
    const rgb = `rgb(${(green >> 16) & 255}, ${(green >> 8) & 255}, ${green & 255})`;
    expect(nameCell.style.borderLeft).toContain(rgb);
  });

  it("reserves the bar's width even when inactive, so rows never shift", () => {
    renderInventory("mr_x");
    const inactive = screen.getByText("Agent Red").closest("td") as HTMLTableCellElement;
    // Transparent rather than absent: a row that gained a border only when active would jump
    // sideways every time the turn moved down the table.
    expect(inactive.style.borderLeft).toContain("transparent");
    expect(inactive.style.borderLeft).toContain("3px");
  });
});
