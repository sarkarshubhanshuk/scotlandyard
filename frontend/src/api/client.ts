import type { DetectiveId, LegalMove, MapData, PublicGameState, TicketType } from "../types";

// No Vite proxy configured - the backend opens CORS to this dev origin (server.py) specifically
// so the frontend can call it directly across ports in dev. Override via VITE_API_BASE_URL (a
// .env file, or the shell environment at build/dev time) to point at a non-default backend
// origin - and note server.py's ALLOWED_ORIGINS has to agree, since it no longer allows "*".
const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

interface ErrorBody {
  error?: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    // The game-ownership cookie the API sets on /games. Deployed, the SPA and API share an
    // origin and this would be redundant - but in dev they are :5173 and :8000, and without it
    // every request would arrive anonymous and be refused, so dev and prod would disagree about
    // something worth testing in dev.
    credentials: "include",
    ...init,
  });
  if (!res.ok) {
    const body = (await res.json().catch(() => ({ error: res.statusText }))) as ErrorBody;
    throw new ApiError(body.error ?? res.statusText, res.status);
  }
  return res.json() as Promise<T>;
}

export function createGame(): Promise<PublicGameState> {
  return request("/games", { method: "POST" });
}

export function getGame(gameId: string): Promise<PublicGameState> {
  return request(`/games/${gameId}`);
}

export function getMap(gameId: string): Promise<MapData> {
  return request(`/games/${gameId}/map`);
}

export interface Hop2PreviewArgs {
  fromNode: number;
  ticketTypeSpent: TicketType;
}

export function getMrXLegalMoves(gameId: string, hop2Preview?: Hop2PreviewArgs): Promise<LegalMove[]> {
  const query = hop2Preview
    ? `?from_node=${hop2Preview.fromNode}&ticket_type_spent=${hop2Preview.ticketTypeSpent}`
    : "";
  return request<{ legal_moves: LegalMove[] }>(`/games/${gameId}/mrx/legal-moves${query}`).then(
    (body) => body.legal_moves,
  );
}

export type MrXMoveRequest =
  | { move_type: "single"; target_node: number; ticket_type_spent: TicketType }
  | {
      move_type: "double";
      hop1: { target_node: number; ticket_type_spent: TicketType };
      hop2: { target_node: number; ticket_type_spent: TicketType };
    };

export function submitMrXMove(gameId: string, move: MrXMoveRequest): Promise<PublicGameState> {
  return request(`/games/${gameId}/mrx/move`, {
    method: "POST",
    body: JSON.stringify(move),
  });
}

/**
 * Tells the backend that a detective's pawn has finished animating, which is what releases the
 * next detective's turn (ADR-0010).
 *
 * Deliberately fire-and-forget from the caller's point of view: the backend waits only a few
 * seconds for this before continuing anyway, so a failed or late ack costs a little pacing and
 * nothing else. There is nothing useful for the UI to say about it and nothing worth retrying.
 */
export function postTurnAck(gameId: string, roundNumber: number, detective: DetectiveId): Promise<{ applied: boolean }> {
  return request(`/games/${gameId}/turn-ack`, {
    method: "POST",
    body: JSON.stringify({ round_number: roundNumber, detective }),
  });
}

export function openRoundStream(gameId: string): EventSource {
  // withCredentials is what carries the ownership cookie cross-origin in dev. EventSource cannot
  // set headers at all, which is precisely why ownership is a cookie rather than a bearer header.
  return new EventSource(`${API_BASE}/games/${gameId}/round/stream`, { withCredentials: true });
}

export { ApiError, API_BASE };
