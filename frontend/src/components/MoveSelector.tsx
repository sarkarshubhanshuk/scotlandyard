import type { MrXMoveWizard } from "../hooks/useMrXMoveWizard";
import { TICKET_LABELS } from "../labels";

export function MoveSelector({ wizard }: { wizard: MrXMoveWizard }) {
  if (!wizard.isMrXTurn) {
    return (
      <section style={{ opacity: 0.5, border: "1px dashed #ccc", borderRadius: 4, padding: 8 }}>
        <h3 style={{ margin: 0 }}>Move Selector</h3>
        <p style={{ margin: "4px 0 0", fontSize: 12 }}>Waiting on the detectives...</p>
      </section>
    );
  }

  return (
    <section style={{ border: "1px solid #ddd", borderRadius: 4, padding: 8 }}>
      <h3 style={{ margin: "0 0 8px" }}>Move Selector</h3>

      <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, marginBottom: 8 }}>
        <input
          type="checkbox"
          checked={wizard.doubleMode}
          disabled={!wizard.canDoubleMove || wizard.hop1 !== null || wizard.submitting}
          onChange={(e) => wizard.setDoubleMode(e.target.checked)}
        />
        Double move {!wizard.canDoubleMove && "(no double tickets left)"}
      </label>

      {wizard.hop1 && (
        <div style={{ fontSize: 13, marginBottom: 8, display: "flex", alignItems: "center", gap: 8 }}>
          <span>
            Hop 1: node {wizard.hop1.target} via {TICKET_LABELS[wizard.hop1.ticket]}
          </span>
          <button onClick={wizard.cancelHop1} disabled={wizard.submitting}>
            Cancel
          </button>
        </div>
      )}

      {wizard.pendingTarget ? (
        <div style={{ fontSize: 13 }}>
          <p style={{ margin: "0 0 6px" }}>
            Move to node {wizard.pendingTarget.target} via:
          </p>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {wizard.pendingTarget.ticketOptions.map((ticket) => (
              <button key={ticket} onClick={() => void wizard.chooseTicket(ticket)} disabled={wizard.submitting}>
                {TICKET_LABELS[ticket]}
              </button>
            ))}
            <button onClick={wizard.cancelPendingTarget} disabled={wizard.submitting}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <p style={{ fontSize: 13, color: "#666", margin: 0 }}>
          {wizard.hop1 ? "Click a highlighted node for hop 2." : "Click a highlighted node on the board to move there."}
        </p>
      )}

      {wizard.legalMoves.length === 0 && !wizard.submitting && (
        <p style={{ fontSize: 13, color: "#b00020" }}>No legal moves available.</p>
      )}
      {wizard.submitting && <p style={{ fontSize: 13, color: "#666" }}>Submitting move...</p>}
      {wizard.error && <p style={{ fontSize: 13, color: "#b00020" }}>{wizard.error}</p>}
    </section>
  );
}
