import type { CSSProperties } from "react";
import { AGENT_COLORS, DETECTIVE_LABELS, toCssColor } from "../labels";
import { DETECTIVE_IDS, type PublicGameState } from "../types";

const cellStyle: CSSProperties = { padding: "2px 8px", textAlign: "right" };
const headerStyle: CSSProperties = { ...cellStyle, fontWeight: 600, textAlign: "left" };

export function TicketInventory({ gameState }: { gameState: PublicGameState }) {
  return (
    <section>
      <h3 style={{ marginBottom: 8 }}>Ticket Inventory</h3>
      <table style={{ borderCollapse: "collapse", fontSize: 13, width: "100%" }}>
        <thead>
          <tr>
            <th style={headerStyle}>Player</th>
            <th style={cellStyle}>Taxi</th>
            <th style={cellStyle}>Bus</th>
            <th style={cellStyle}>Metro</th>
            <th style={cellStyle}>Black</th>
            <th style={cellStyle}>2x</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td style={{ ...headerStyle, fontWeight: 700 }}>Mr. X</td>
            <td style={cellStyle}>{gameState.mr_x.taxi_tickets}</td>
            <td style={cellStyle}>{gameState.mr_x.bus_tickets}</td>
            <td style={cellStyle}>{gameState.mr_x.metro_tickets}</td>
            <td style={cellStyle}>{gameState.mr_x.black_tickets}</td>
            <td style={cellStyle}>{gameState.mr_x.double_tickets}</td>
          </tr>
          {DETECTIVE_IDS.map((detId) => {
            const detective = gameState.detectives[detId];
            return (
              <tr key={detId}>
                <td style={{ ...headerStyle, color: toCssColor(AGENT_COLORS[detId]) }}>{DETECTIVE_LABELS[detId]}</td>
                <td style={cellStyle}>{detective.taxi_tickets}</td>
                <td style={cellStyle}>{detective.bus_tickets}</td>
                <td style={cellStyle}>{detective.metro_tickets}</td>
                <td style={cellStyle}>-</td>
                <td style={cellStyle}>-</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
