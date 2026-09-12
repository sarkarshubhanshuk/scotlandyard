import { SURFACING_ROUNDS, TICKET_ICONS, TICKET_LABELS } from "../labels";
import type { PublicMrX } from "../types";

// The ticket-type history itself is always fully visible (rules.md: "Mr. X must display a
// continuous, visible history of the transport tickets he has used") - only his actual NODE is
// concealed, and the backend only ever retains the MOST RECENT surfacing reveal
// (last_known_node/last_known_round), not a full per-round history. So the log shows every
// hop's ticket icon (never concealed) plus a single "last known position" line, rather than
// pretending to reconstruct node reveals for past surfacing rounds the backend no longer has.
export function TravelLog({ mrX }: { mrX: PublicMrX }) {
  const nextSurfacing = SURFACING_ROUNDS.find((round) => round > (mrX.last_known_round ?? 0));

  return (
    <section>
      <h3 style={{ margin: "0 0 8px" }}>Travel Log</h3>
      <div style={{ display: "flex", gap: 4, flexWrap: "wrap", minHeight: 28 }}>
        {mrX.transport_history.length === 0 && <p style={{ fontSize: 12, color: "#666", margin: 0 }}>No moves yet.</p>}
        {mrX.transport_history.map((ticket, i) => (
          <img key={i} src={TICKET_ICONS[ticket]} alt={TICKET_LABELS[ticket]} title={TICKET_LABELS[ticket]} width={28} height={28} />
        ))}
      </div>
      <p style={{ fontSize: 13, margin: "8px 0 0" }}>
        {mrX.last_known_node != null
          ? `Last known position: node ${mrX.last_known_node} (as of round ${mrX.last_known_round})`
          : "Mr. X has not yet surfaced."}
      </p>
      {nextSurfacing && <p style={{ fontSize: 12, color: "#666", margin: "2px 0 0" }}>Next reveal: round {nextSurfacing}</p>}
    </section>
  );
}
