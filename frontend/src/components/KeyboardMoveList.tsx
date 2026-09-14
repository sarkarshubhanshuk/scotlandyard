import type { LegalMove, TicketType } from "../types";
import { TICKET_LABELS } from "../labels";

/**
 * A keyboard- and screen-reader-reachable way to make Mr. X's move.
 *
 * The board is a Phaser canvas (ADR-0015), and canvas content is invisible to assistive
 * technology and unreachable by keyboard - so clicking a node was the *only* way to play. This
 * is the parallel affordance: the same legal destinations the board highlights, as real buttons.
 *
 * Visually hidden until something inside it receives focus, then revealed in place - the same
 * pattern as a skip link. A sighted mouse user never sees it; a keyboard user reaches it with
 * Tab and can see exactly what they have selected. It is deliberately NOT `display: none` or
 * `hidden`, both of which would take it out of the tab order and defeat the purpose.
 *
 * It renders one button per (destination, ticket) pair rather than mirroring the board's
 * two-step click-then-choose-ticket popup: a list of complete, self-describing actions is easier
 * to operate without sight than a mode that changes what the next keypress means.
 */
interface KeyboardMoveListProps {
  legalMoves: LegalMove[];
  isMrXTurn: boolean;
  submitting: boolean;
  /** The double-move hop currently being chosen, if one is in progress - labels say which. */
  hopLabel: string | null;
  onChoose: (targetNode: number, ticket: TicketType) => void;
}

export function KeyboardMoveList({
  legalMoves,
  isMrXTurn,
  submitting,
  hopLabel,
  onChoose,
}: KeyboardMoveListProps) {
  // Nothing to offer between turns; rendering an empty, focusable region would just be a tab
  // stop that says nothing.
  if (!isMrXTurn || legalMoves.length === 0) return null;

  return (
    <section className="sy-keyboard-moves" aria-labelledby="keyboard-moves-heading">
      <h2 id="keyboard-moves-heading">
        {hopLabel ?? "Your move"} — {legalMoves.length} legal destination
        {legalMoves.length === 1 ? "" : "s"}
      </h2>
      <p>
        The board is a canvas and cannot be reached with a keyboard. Choose a destination and
        ticket here instead.
      </p>
      <ul>
        {legalMoves.map((move) => (
          <li key={move.target_node}>
            {move.ticket_options.map((ticket) => (
              <button
                key={ticket}
                type="button"
                disabled={submitting}
                onClick={() => onChoose(move.target_node, ticket)}
              >
                Node {move.target_node} by {TICKET_LABELS[ticket]}
              </button>
            ))}
          </li>
        ))}
      </ul>
    </section>
  );
}
