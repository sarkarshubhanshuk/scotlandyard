export const DETECTIVE_LABELS: Record<string, string> = {
  detective_1: "Detective 1",
  detective_2: "Detective 2",
  detective_3: "Detective 3",
  detective_4: "Detective 4",
  detective_5: "Detective 5",
};

export const TICKET_LABELS: Record<string, string> = {
  taxi: "Taxi",
  bus: "Bus",
  metro: "Metro",
  black: "Black",
};

export const TICKET_ICONS: Record<string, string> = {
  taxi: "/tickets/taxi_ticket.svg",
  bus: "/tickets/bus_ticket.svg",
  metro: "/tickets/metro_ticket.svg",
  black: "/tickets/concealed_ticket.svg",
};

// Mirrors backend/round_resolver.py:SURFACING_ROUNDS - the rounds after which Mr. X must reveal
// his position (rules.md: "Mr. X must reveal his location at the end of turns 3, 8, 13, 18, and 24").
export const SURFACING_ROUNDS = [3, 8, 13, 18, 24];
