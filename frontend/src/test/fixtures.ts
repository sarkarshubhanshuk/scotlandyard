import { DETECTIVE_IDS, type DetectiveId, type PublicGameState } from "../types";

/**
 * A minimal but structurally complete PublicGameState, matching backend
 * serializers.py:serialize_public_state. Every detective starts on a distinct node with the
 * rules' opening ticket inventory, so a test can assert on a ticket transfer without first
 * having to set one up.
 */
export function makeGameState(overrides: Partial<PublicGameState> = {}): PublicGameState {
  const startingNodes: Record<DetectiveId, number> = {
    agent_red: 26,
    agent_blue: 29,
    agent_green: 34,
    agent_orange: 50,
    agent_purple: 53,
  };
  return {
    game_id: "test-game",
    status: "detective_loop_running",
    winner: null,
    winning_detective: null,
    round_number: 1,
    mr_x: {
      current_node: 13,
      taxi_tickets: 2,
      bus_tickets: 5,
      metro_tickets: 3,
      black_tickets: 5,
      double_tickets: 2,
      last_known_node: null,
      last_known_round: null,
      transport_history: [],
    },
    detectives: Object.fromEntries(
      DETECTIVE_IDS.map((id) => [
        id,
        { node_id: startingNodes[id], taxi_tickets: 11, bus_tickets: 8, metro_tickets: 4 },
      ]),
    ) as PublicGameState["detectives"],
    ...overrides,
  };
}

/**
 * Stands in for the browser's EventSource, which jsdom does not drive.
 *
 * `emit` delivers one named SSE event exactly as the real thing would - the hook registers a
 * listener per event NAME (see useRoundStream's `on` helper), so dispatching by name is what
 * makes this a faithful stand-in rather than a reimplementation of the hook's own dispatch.
 */
export class FakeEventSource {
  static last: FakeEventSource | null = null;

  listeners = new Map<string, (event: { data: string }) => void>();
  closed = false;
  onerror: (() => void) | null = null;
  url: string;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.last = this;
  }

  addEventListener(name: string, handler: (event: { data: string }) => void) {
    this.listeners.set(name, handler);
  }

  close() {
    this.closed = true;
  }

  emit(name: string, payload: unknown) {
    const handler = this.listeners.get(name);
    if (!handler) throw new Error(`no listener registered for SSE event "${name}"`);
    handler({ data: JSON.stringify(payload) });
  }
}
