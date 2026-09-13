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
export const DETECTIVE_IDS = ["agent_red", "agent_blue", "agent_green", "agent_orange", "agent_purple"] as const;
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

// The SSE events GET /games/{id}/round/stream emits. Most of them are agents.py's own
// per-LLM-call stream-writer payloads, relayed verbatim by server.py:round_stream_route - one
// event per call rather than one per graph node, so a turn's six messages arrive as they
// happen instead of landing together once the whole turn has finished. Only round_finalized
// and round_result come from serializers.py:serialize_loop_event.

// A detective's turn is beginning. Nothing has been decided yet.
export interface TurnStartedEvent {
  type: "turn_started";
  detective: DetectiveId;
}

// The mover's opening proposal, broadcast to the other four before any of them answer.
export interface TurnProposalEvent {
  type: "turn_proposal";
  detective: DetectiveId;
  target_node: number;
  rationale: string;
}

// One non-mover's answer to the proposal on the table. preferred_node is that responder's own
// stated intent for its own turn and is advisory only (ADR-0009) - it reserves nothing. It is
// null when the response call failed, or when the node named wasn't one of the responder's own
// legal moves and was dropped rather than silently rewritten (see agents.py:turn_node).
export interface TurnResponseEvent {
  type: "turn_response";
  detective: DetectiveId;
  responding_to: DetectiveId;
  response: string;
  preferred_node: number | null;
}

// The mover's final choice, ALREADY APPLIED on the server (ADR-0010) - this detective has
// moved, spent its ticket, and every detective still to move this round sees it there. The
// client mirrors the same change locally so the board animates the pawn and the ticket counts
// stay live; round_result re-syncs against server truth at the end of the round regardless.
//
// transport is null only if the detective could not legally move at all and stayed put, in
// which case from_node === target_node and no ticket changed hands.
export interface TurnDecisionEvent {
  type: "turn_decision";
  detective: DetectiveId;
  from_node: number;
  target_node: number;
  transport: TicketType | null;
  rationale: string;
  // True if this detective landed on Mr. X. The game ends here and the detectives behind it in
  // the turn order never move - see graph.py's router.
  captured: boolean;
}

// The graph's turn node finished - a boundary marker, since every message it produced has
// already been streamed by the four events above.
export interface TurnFinishedEvent {
  type: "turn_finished";
  detective: DetectiveId | null;
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
