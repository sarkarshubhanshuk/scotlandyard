import { useId, useState, type CSSProperties } from "react";
import { AGENT_COLORS, DETECTIVE_LABELS, MR_X_COLOR, toCssColor } from "../labels";
import { DETECTIVE_IDS, type PublicGameState } from "../types";

const cellStyle: CSSProperties = { padding: "2px 4px", textAlign: "right" };
// Headers stay on one line (project owner's call), which is what drives the smaller size and the
// tightened padding above - at the table's previous 13px the six full-length labels do not fit
// the sidebar. See the note in frontend/README.md about what that costs at high display scaling.
const headerStyle: CSSProperties = {
  ...cellStyle,
  fontWeight: 600,
  fontSize: 10,
  whiteSpace: "nowrap",
};
const nameStyle: CSSProperties = { ...cellStyle, fontWeight: 600, textAlign: "left" };

// The left edge of the active player's row carries a bar in that player's own colour. Rendered
// as a border on the name cell rather than the <tr>, because a table row's border is not painted
// under `border-collapse: collapse`. Transparent (not absent) when inactive, so a row never
// changes width as the turn moves down the table.
const TURN_BAR_WIDTH = 3;

/**
 * What each ticket actually buys, keyed by the column it sits above.
 *
 * Wording deliberately tracks `HowToPlayModal` - the route colours are that screen's own
 * vocabulary (yellow taxi, green bus, red dashed metro, black dashed boat), and the two facts
 * that matter strategically are the ones it already teaches: a black ticket hides which route
 * type was used, and a double-move still costs a transport ticket per hop. rules.md sections 3
 * and 4 are the source for both.
 */
const TICKET_HELP: Record<string, string> = {
  taxi: "Pays for yellow Taxi routes.",
  bus: "Pays for green Bus routes.",
  metro: "Pays for red Metro routes.",
  black:
    'Mr. X only. Pays for any route — Taxi, Bus, Metro or Boat — and the travel log only ever says "black".',
  double: "Mr. X only. Move twice in one turn, spending a transport ticket for each hop.",
};

// `anchor` is which edge the tooltip hangs from. The popup is a fixed 210px, so one anchored
// left under the rightmost columns would run off the sidebar entirely - these two flip to the
// right edge instead. Stated per column rather than measured at runtime: the column count and
// the sidebar are both fixed, so there is nothing here worth a ResizeObserver.
const COLUMNS: { key: string; label: string; anchor: "left" | "right" }[] = [
  { key: "taxi", label: "Taxi Tickets", anchor: "left" },
  { key: "bus", label: "Bus Tickets", anchor: "left" },
  { key: "metro", label: "Metro Tickets", anchor: "left" },
  { key: "black", label: "Black Tickets", anchor: "right" },
  { key: "double", label: "Double Moves", anchor: "right" },
];

/**
 * One column header, with a tooltip explaining what that ticket buys.
 *
 * Opens on focus as well as hover. A hover-only tooltip is invisible to anyone navigating by
 * keyboard, which would undo the accessibility work the board's own KeyboardMoveList exists for -
 * so the label is a real tab stop and `aria-describedby` ties the popup to it for screen readers.
 */
function TicketHeader({
  label,
  help,
  anchor,
}: {
  label: string;
  help: string;
  anchor: "left" | "right";
}) {
  const [open, setOpen] = useState(false);
  const tooltipId = useId();

  return (
    <th style={headerStyle} scope="col">
      <span
        tabIndex={0}
        aria-describedby={open ? tooltipId : undefined}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        // Escape dismisses without moving focus, the expected behaviour for a tooltip that a
        // keyboard user opened by tabbing onto its label.
        onKeyDown={(event) => {
          if (event.key === "Escape") setOpen(false);
        }}
        style={{ position: "relative", display: "inline-block", cursor: "help" }}
      >
        {label}
        {open && (
          <span
            id={tooltipId}
            role="tooltip"
            style={{
              position: "absolute",
              // Below the label, not above: this table sits at the very top of the sidebar, so a
              // tooltip above the header row would be clipped by the viewport.
              top: "100%",
              [anchor]: 0,
              marginTop: 6,
              background: "#222",
              color: "#fff",
              padding: "6px 8px",
              borderRadius: 6,
              fontSize: 12,
              fontWeight: 400,
              lineHeight: 1.4,
              // Wraps rather than nowrap: the black-ticket text is a full sentence and would
              // otherwise run off the sidebar entirely.
              width: 210,
              whiteSpace: "normal",
              textAlign: "left",
              zIndex: 20,
              pointerEvents: "none",
              boxShadow: "0 2px 6px rgba(0,0,0,0.3)",
            }}
          >
            {help}
          </span>
        )}
      </span>
    </th>
  );
}

interface TicketInventoryProps {
  gameState: PublicGameState;
  /**
   * Whose turn it is right now - "mr_x", a DetectiveId, or null between turns.
   *
   * The same value `GameScreen` hands the board for its turn halo (ADR-0011), deliberately: the
   * sidebar and the board then cannot disagree about whose turn it is, because there is only one
   * answer being rendered twice.
   */
  activeTurnPawnId?: string | null;
}

export function TicketInventory({ gameState, activeTurnPawnId = null }: TicketInventoryProps) {
  function rowStyle(pawnId: string): CSSProperties {
    return activeTurnPawnId === pawnId ? { background: "rgba(0, 0, 0, 0.055)" } : {};
  }

  function barStyle(pawnId: string, color: number): CSSProperties {
    return {
      borderLeft: `${TURN_BAR_WIDTH}px solid ${
        activeTurnPawnId === pawnId ? toCssColor(color) : "transparent"
      }`,
    };
  }

  return (
    <section>
      <h3 style={{ marginBottom: 8 }}>Ticket Inventory</h3>
      <table style={{ borderCollapse: "collapse", fontSize: 13, width: "100%" }}>
        <thead>
          <tr>
            <th
              style={{ ...headerStyle, textAlign: "left", paddingLeft: 4 + TURN_BAR_WIDTH }}
              scope="col"
            >
              Player
            </th>
            {COLUMNS.map((column) => (
              <TicketHeader
                key={column.key}
                label={column.label}
                help={TICKET_HELP[column.key]}
                anchor={column.anchor}
              />
            ))}
          </tr>
        </thead>
        <tbody>
          <tr style={rowStyle("mr_x")}>
            <td style={{ ...nameStyle, fontWeight: 700, ...barStyle("mr_x", MR_X_COLOR) }}>
              Mr. X
            </td>
            <td style={cellStyle}>{gameState.mr_x.taxi_tickets}</td>
            <td style={cellStyle}>{gameState.mr_x.bus_tickets}</td>
            <td style={cellStyle}>{gameState.mr_x.metro_tickets}</td>
            <td style={cellStyle}>{gameState.mr_x.black_tickets}</td>
            <td style={cellStyle}>{gameState.mr_x.double_tickets}</td>
          </tr>
          {DETECTIVE_IDS.map((detId) => {
            const detective = gameState.detectives[detId];
            return (
              <tr key={detId} style={rowStyle(detId)}>
                <td
                  style={{
                    ...nameStyle,
                    color: toCssColor(AGENT_COLORS[detId]),
                    ...barStyle(detId, AGENT_COLORS[detId]),
                  }}
                >
                  {DETECTIVE_LABELS[detId]}
                </td>
                <td style={cellStyle}>{detective.taxi_tickets}</td>
                <td style={cellStyle}>{detective.bus_tickets}</td>
                <td style={cellStyle}>{detective.metro_tickets}</td>
                {/* Detectives hold neither (rules.md section 1), so these are structurally blank
                    rather than zero - a 0 would read as "spent them all". */}
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
