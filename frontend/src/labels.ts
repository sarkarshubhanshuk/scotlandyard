import { DETECTIVE_IDS, type DetectiveId, type TravelLogTicket } from "./types";

// Mirrors backend/scotland_yard/rules_constants.py's AGENT_DISPLAY_NAMES.
export const DETECTIVE_LABELS: Record<DetectiveId, string> = {
  agent_red: "Agent Red",
  agent_blue: "Agent Blue",
  agent_green: "Agent Green",
  agent_orange: "Agent Orange",
  agent_purple: "Agent Purple",
};

// Reverse of DETECTIVE_LABELS ("Agent Red" -> "agent_red") - used to recognize an agent's display
// name wherever it appears in free-text (e.g. the AI debate transcript) so it can be colored.
export const AGENT_LABEL_TO_ID: Record<string, DetectiveId> = Object.fromEntries(
  DETECTIVE_IDS.map((id) => [DETECTIVE_LABELS[id], id]),
);

// Mirrors DETECTIVE_LABELS without the "Agent " prefix - used only by the sidebar's "Ongoing
// actions" line (useRoundStream.ts/GameScreen.tsx), which names detectives by callsign alone
// ("Red's turn - ...") rather than the full "Agent Red" ChatLog and tooltips use everywhere else.
export const DETECTIVE_SHORT_LABELS: Record<DetectiveId, string> = {
  agent_red: "Red",
  agent_blue: "Blue",
  agent_green: "Green",
  agent_orange: "Orange",
  agent_purple: "Purple",
};

// Reverse of DETECTIVE_SHORT_LABELS ("Red" -> "agent_red") - same purpose as AGENT_LABEL_TO_ID,
// scoped to the short form.
export const AGENT_SHORT_LABEL_TO_ID: Record<string, DetectiveId> = Object.fromEntries(
  DETECTIVE_IDS.map((id) => [DETECTIVE_SHORT_LABELS[id], id]),
);

// Kept here (not in board/BoardScene.ts, which imports Phaser) so both the Phaser board and plain
// React components (TicketInventory, ChatLog) can use the same per-agent colors without pulling
// Phaser into the main bundle - see boardDimensions.ts for the same rationale re: board sizing.
// Specific hex values chosen for this project, not plain CSS named colors.
export const AGENT_COLORS: Record<DetectiveId, number> = {
  agent_red: 0xe0115f,
  agent_blue: 0x0f52ba,
  agent_green: 0x2e8b57,
  agent_orange: 0xf28500,
  agent_purple: 0x9966cc,
};

// Mr. X's pawn colour. Lives here rather than in BoardScene.ts for the same reason
// AGENT_COLORS does: TicketInventory needs it for the active-turn row marker and is in the main
// bundle, so it must not import a Phaser module to get it.
export const MR_X_COLOR = 0x3a3a3a;

export function toCssColor(hex: number): string {
  return `#${hex.toString(16).padStart(6, "0")}`;
}

// Covers every TicketType (used for the ticket-choice popup BoardCanvas renders near a clicked
// destination node) plus "double" (only ever appears in mr_x.transport_history, rendered by
// TravelLog).
export const TICKET_LABELS: Record<TravelLogTicket, string> = {
  taxi: "Taxi",
  bus: "Bus",
  metro: "Metro",
  black: "Black",
  double: "Double Move",
};

// Sourced from data/tickets/ (the project's canonical ticket art). Vite only serves static
// assets from inside the frontend project, so scripts/sync-assets.mjs copies them into
// public/tickets/ on every dev/build run - the copies are generated, not committed.
export const TICKET_ICONS: Record<TravelLogTicket, string> = {
  taxi: "/tickets/taxi_ticket.jpg",
  bus: "/tickets/bus_ticket.jpg",
  metro: "/tickets/metro_ticket.jpg",
  black: "/tickets/black_ticket.jpg",
  double: "/tickets/doublemove_ticket.jpg",
};

// Mirrors backend/scotland_yard/rules_constants.py:SURFACING_ROUNDS - the rounds after which Mr. X must reveal
// his position (rules.md: "Mr. X must reveal his location at the end of turns 3, 8, 13, 18, and 24").
export const SURFACING_ROUNDS = [3, 8, 13, 18, 24];

// Mirrors backend/scotland_yard/rules_constants.py's MAX_ROUND - TravelLog always renders one slot per possible
// round (played or not), rather than growing as the game progresses.
export const MAX_ROUND = 24;
