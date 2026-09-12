// Mirrors backend/serializers.py's serialize_public_state and game_master.py's map/{node_positions} shapes.
// Kept as plain hand-written types (no shared codegen) since the backend has no OpenAPI schema.

export type TransportType = "taxi" | "bus" | "metro" | "boat";
export type TicketType = "taxi" | "bus" | "metro" | "black";

export interface MapConnection {
  destination: number;
  type: TransportType;
}

export interface MapNode {
  id: number;
  allowed_transport: TransportType[];
  connections: MapConnection[];
}

export interface NodePosition {
  x: number;
  y: number;
}

export interface MapData {
  nodes: MapNode[];
  positions: Record<string, NodePosition>;
}

export type GameStatus = "awaiting_mr_x_move" | "detective_loop_running" | "game_over";
export type Winner = "detectives" | "mr_x" | null;

export interface PublicMrX {
  taxi_tickets: number;
  bus_tickets: number;
  metro_tickets: number;
  black_tickets: number;
  double_tickets: number;
  last_known_node: number | null;
  last_known_round: number | null;
  transport_history: TicketType[];
}

export interface PublicDetective {
  node_id: number;
  taxi_tickets: number;
  bus_tickets: number;
  metro_tickets: number;
}

export const DETECTIVE_IDS = [
  "detective_1",
  "detective_2",
  "detective_3",
  "detective_4",
  "detective_5",
] as const;
export type DetectiveId = (typeof DETECTIVE_IDS)[number];

export interface PublicGameState {
  game_id: string;
  status: GameStatus;
  winner: Winner;
  round_number: number;
  mr_x: PublicMrX;
  detectives: Record<DetectiveId, PublicDetective>;
}

export interface LegalMove {
  target_node: number;
  ticket_options: TicketType[];
}

// Mirrors backend/serializers.py:serialize_loop_event's payload shapes - the SSE events
// GET /games/{id}/round/stream emits, one named event per detective_graph step.
export interface DetectiveStrategy {
  proposed_board_moves: Record<string, number>;
  rationale: string;
}

export interface ProposalEvent {
  type: "proposal";
  proposed_strategies: Record<string, DetectiveStrategy>;
}

export interface DebateEvent {
  type: "debate";
  transcript: string;
}

export interface VoteTallyEvent {
  type: "vote_tally";
  locked_moves: Record<string, number>;
  loop_number: number;
}

export interface RoundFinalizedEvent {
  type: "round_finalized";
  final_moves: Record<string, number>;
}

export interface RoundResultEvent {
  type: "round_result";
  status: GameStatus;
  winner: Winner;
  round_number: number;
  state: PublicGameState;
}
