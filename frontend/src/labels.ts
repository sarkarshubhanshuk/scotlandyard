import { DETECTIVE_IDS, type DetectiveId, type TicketType } from "./types";

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

export const TICKET_LABELS: Record<TicketType, string> = {
  taxi: "Taxi",
  bus: "Bus",
  metro: "Metro",
  black: "Black",
};

export const TICKET_ICONS: Record<TicketType, string> = {
  taxi: "/tickets/taxi_ticket.svg",
  bus: "/tickets/bus_ticket.svg",
  metro: "/tickets/metro_ticket.svg",
  black: "/tickets/concealed_ticket.svg",
};

// Mirrors backend/round_resolver.py:SURFACING_ROUNDS - the rounds after which Mr. X must reveal
// his position (rules.md: "Mr. X must reveal his location at the end of turns 3, 8, 13, 18, and 24").
export const SURFACING_ROUNDS = [3, 8, 13, 18, 24];
