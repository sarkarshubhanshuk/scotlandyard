import { useState } from "react";
import { MAX_ROUND, SURFACING_ROUNDS, TICKET_ICONS, TICKET_LABELS } from "../labels";
import type { PublicMrX, TicketType, TravelLogTicket } from "../types";

interface RoundSlot {
  round: number;
  isReveal: boolean;
  played: boolean;
  icon: TravelLogTicket | null;
  statusText: string;
}

// Walks transport_history into one entry per ROUND (not per hop): a double-move round logs
// "double" followed by its two hop tickets (mrx_turn.py), all three belonging to the single round
// that spent the double-move ticket, so they must collapse into one slot rather than three.
// Rounds are always played strictly in order with no gaps, so the Nth parsed entry here is
// always round N+1 - there is no separate per-entry round number to read from the backend.
function playedRoundsFromHistory(history: TravelLogTicket[]): { icon: TravelLogTicket; statusText: string }[] {
  const rounds: { icon: TravelLogTicket; statusText: string }[] = [];
  let i = 0;
  while (i < history.length) {
    if (history[i] === "double") {
      const hop1 = history[i + 1] as TicketType;
      const hop2 = history[i + 2] as TicketType;
      rounds.push({
        icon: "double",
        statusText: `Mr. X used Double Move (${TICKET_LABELS[hop1]} + ${TICKET_LABELS[hop2]})`,
      });
      i += 3;
    } else {
      const ticket = history[i] as TicketType;
      rounds.push({ icon: ticket, statusText: `Mr. X used ${TICKET_LABELS[ticket]}` });
      i += 1;
    }
  }
  return rounds;
}

function buildSlots(mrX: PublicMrX): RoundSlot[] {
  const playedRounds = playedRoundsFromHistory(mrX.transport_history);
  return Array.from({ length: MAX_ROUND }, (_, i) => {
    const round = i + 1;
    const isReveal = SURFACING_ROUNDS.includes(round);
    const playedEntry = playedRounds[i];
    return playedEntry
      ? { round, isReveal, played: true, icon: playedEntry.icon, statusText: playedEntry.statusText }
      : { round, isReveal, played: false, icon: null, statusText: "Not Played" };
  });
}

// The ticket-type history itself is always fully visible (rules.md: "Mr. X must display a
// continuous, visible history of the transport tickets he has used") - only his actual NODE is
// concealed. Node identity is deliberately not shown anywhere here (not even for a past
// surfacing round) - the backend only ever retains the MOST RECENT reveal
// (last_known_node/last_known_round), so a slot for an earlier surfacing round has no node data
// left to show; the reveal/non-reveal border below marks WHICH rounds are surfacing rounds
// structurally, not what was actually seen.
export function TravelLog({ mrX }: { mrX: PublicMrX }) {
  const [hoveredRound, setHoveredRound] = useState<number | null>(null);
  const slots = buildSlots(mrX);

  return (
    <section>
      <h3 style={{ margin: "0 0 8px" }}>Mr. X Travel Log</h3>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(52px, 1fr))", gap: 6 }}>
        {slots.map((slot) => (
          <div
            key={slot.round}
            // position:relative lives on this OUTER wrapper, not the bordered box below - that
            // box needs its own overflow:hidden to clip the ticket image to its rounded corners,
            // and an absolutely-positioned tooltip parented to an overflow:hidden ancestor would
            // get clipped the instant it pokes outside that ancestor's box (which "bottom: 100%"
            // always does). Keeping the tooltip a SIBLING of the clipped box, both inside this
            // unclipped wrapper, is what lets it float above without being cut off.
            // minWidth: 0 overrides a grid item's default min-width:auto, which otherwise sizes
            // a column to fit its widest UNBROKEN content - "Round 19"/"Round 24"'s text was
            // stretching whichever column it landed in wider than columns holding a ticket image,
            // so placeholders and tickets ended up different widths despite sharing a grid track.
            style={{ position: "relative", minWidth: 0 }}
            onMouseEnter={() => setHoveredRound(slot.round)}
            onMouseLeave={() => setHoveredRound((current) => (current === slot.round ? null : current))}
          >
            <div
              style={{
                aspectRatio: "2 / 1",
                boxSizing: "border-box",
                border: `2px solid ${slot.isReveal ? "#e67e00" : "#000"}`,
                borderRadius: 8,
                overflow: "hidden",
                background: "#fff",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              {slot.played ? (
                // Ticket art is itself a 2:1 rectangle (docs/tickets/*.jpg) - object-fit: contain
                // preserves that aspect ratio exactly rather than stretching to fill the slot.
                <img
                  src={TICKET_ICONS[slot.icon!]}
                  alt={TICKET_LABELS[slot.icon!]}
                  style={{ width: "100%", height: "100%", objectFit: "contain" }}
                />
              ) : (
                <span style={{ fontSize: 11, color: "#666", fontWeight: 600 }}>Round {slot.round}</span>
              )}
            </div>
            {hoveredRound === slot.round && (
              <div
                style={{
                  position: "absolute",
                  bottom: "100%",
                  left: "50%",
                  transform: "translateX(-50%)",
                  marginBottom: 6,
                  background: "#222",
                  color: "#fff",
                  padding: "6px 8px",
                  borderRadius: 6,
                  fontSize: 12,
                  lineHeight: 1.4,
                  whiteSpace: "nowrap",
                  zIndex: 10,
                  pointerEvents: "none",
                  boxShadow: "0 2px 6px rgba(0,0,0,0.3)",
                }}
              >
                <div>Round {slot.round}</div>
                <div>{slot.statusText}</div>
                <div>{slot.isReveal ? "Reveal Node" : "Non-Reveal Node"}</div>
              </div>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
