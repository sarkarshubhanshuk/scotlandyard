import type { LegalMove, MapData, PublicGameState, TicketType } from "../types";

// No Vite proxy configured - the backend already opens CORS to "*" (server.py) specifically so
// the frontend can call it directly across ports in dev. Override via VITE_API_BASE_URL (a .env
// file, or the shell environment at build/dev time) to point at a non-default backend origin.
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

export function openRoundStream(gameId: string): EventSource {
  return new EventSource(`${API_BASE}/games/${gameId}/round/stream`);
}

export { ApiError, API_BASE };
