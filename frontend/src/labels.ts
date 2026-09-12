import { DETECTIVE_IDS, type DetectiveId, type TravelLogTicket } from "./types";

// Mirrors backend/agents.py's AGENT_DISPLAY_NAMES.
export const DETECTIVE_LABELS: Record<DetectiveId, string> = {
  agent_red: "Agent Red",
  agent_blue: "Agent Blue",
  agent_green: "Agent Green",
  agent_yellow: "Agent Yellow",
  agent_purple: "Agent Purple",
};

// Reverse of DETECTIVE_LABELS ("Agent Red" -> "agent_red") - used to recognize an agent's display
// name wherever it appears in free-text (e.g. the AI debate transcript) so it can be colored.
export const AGENT_LABEL_TO_ID: Record<string, DetectiveId> = Object.fromEntries(
  DETECTIVE_IDS.map((id) => [DETECTIVE_LABELS[id], id]),
);

// Kept here (not in board/BoardScene.ts, which imports Phaser) so both the Phaser board and plain
// React components (TicketInventory, ChatLog) can use the same per-agent colors without pulling
// Phaser into the main bundle - see boardDimensions.ts for the same rationale re: board sizing.
// Standard CSS named-color hex values, matching each agent's own display name.
export const AGENT_COLORS: Record<DetectiveId, number> = {
  agent_red: 0xff0000,
  agent_blue: 0x0000ff,
  agent_green: 0x008000,
  agent_yellow: 0xffff00,
  agent_purple: 0x800080,
};

export function toCssColor(hex: number): string {
  return `#${hex.toString(16).padStart(6, "0")}`;
}

// Covers every TicketType (used for MoveSelector's ticket-choice buttons) plus "double" (only
// ever appears in mr_x.transport_history, rendered by TravelLog).
export const TICKET_LABELS: Record<TravelLogTicket, string> = {
  taxi: "Taxi",
  bus: "Bus",
  metro: "Metro",
  black: "Black",
  double: "Double Move",
};

// Sourced from docs/tickets/ (the project's canonical ticket art) - copied verbatim into
// frontend/public/tickets/ since Vite only serves static assets from within the frontend project.
export const TICKET_ICONS: Record<TravelLogTicket, string> = {
  taxi: "/tickets/taxi_ticket.jpg",
  bus: "/tickets/bus_ticket.jpg",
  metro: "/tickets/metro_ticket.jpg",
  black: "/tickets/black_ticket.jpg",
  double: "/tickets/doublemove_ticket.jpg",
};

// Mirrors backend/round_resolver.py:SURFACING_ROUNDS - the rounds after which Mr. X must reveal
// his position (rules.md: "Mr. X must reveal his location at the end of turns 3, 8, 13, 18, and 24").
export const SURFACING_ROUNDS = [3, 8, 13, 18, 24];

// Mirrors backend/round_resolver.py's MAX_ROUND - TravelLog always renders one slot per possible
// round (played or not), rather than growing as the game progresses.
export const MAX_ROUND = 24;
