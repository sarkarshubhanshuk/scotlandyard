// Mirrors backend/serializers.py's serialize_public_state and game_master.py's map/{node_positions} shapes.
// Kept as plain hand-written types (no shared codegen) since the backend has no OpenAPI schema.

export type TransportType = "taxi" | "bus" | "metro" | "boat";
export type TicketType = "taxi" | "bus" | "metro" | "black";
// mr_x.transport_history's entries: every hop's TicketType, plus a "double" sentinel a
// double-move inserts immediately before its own two hop entries (mrx_turn.py) - "double" is
// never a valid TicketType to spend for an actual hop, only ever a log entry.
export type TravelLogTicket = TicketType | "double";

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
  // Mr. X's real position - safe to expose because the only human-facing client is played BY
  // Mr. X (see backend/serializers.py's own note on this). Used to always render his board pawn.
  current_node: number;
  taxi_tickets: number;
  bus_tickets: number;
  metro_tickets: number;
  black_tickets: number;
  double_tickets: number;
  last_known_node: number | null;
  last_known_round: number | null;
  transport_history: TravelLogTicket[];
}

export interface PublicDetective {
  node_id: number;
  taxi_tickets: number;
  bus_tickets: number;
  metro_tickets: number;
}

// Mirrors backend/agents.py's DETECTIVE_IDS - the internal identifier for each detective. Their
// human-readable callsigns ("Agent Red" etc.) live in labels.ts's DETECTIVE_LABELS.
export const DETECTIVE_IDS = ["agent_red", "agent_blue", "agent_green", "agent_yellow", "agent_purple"] as const;
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

// Fired the moment a stage's first LLM call actually goes out (agents.py's get_stream_writer()
// calls) - distinct from the proposal/debate/vote_tally events below, which only arrive once the
// ENTIRE stage (all 5 detectives) has finished.
export interface StageStartedEvent {
  type: "stage_started";
  stage: "proposal" | "debate" | "vote";
}

export interface VoteTallyEvent {
  type: "vote_tally";
  locked_moves: Record<string, number>;
  loop_number: number;
}

// Mirrors backend/transport.py:determine_move_transport's output shape - the same transport
// round_resolver.py:resolve_round will actually apply, computed early so the Chat Log can show
// it before the round is actually resolved. transport is null only if from_node === to_node
// (a detective with no legal move stayed put - an emergency-fallback edge case).
export interface FinalizedMove {
  from_node: number;
  to_node: number;
  transport: TicketType | null;
}

export interface RoundFinalizedEvent {
  type: "round_finalized";
  final_moves: Record<string, FinalizedMove>;
}

export interface RoundResultEvent {
  type: "round_result";
  status: GameStatus;
  winner: Winner;
  round_number: number;
  state: PublicGameState;
}
