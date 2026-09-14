import { useEffect, useRef } from "react";
import type { ChatLogEntry } from "../hooks/useRoundStream";
import { AGENT_COLORS, AGENT_LABEL_TO_ID, DETECTIVE_LABELS, toCssColor } from "../labels";

// Entries carry the turn they belong to (see ChatLogEntry.turn), so a header is only needed
// for the two end-of-round entries that belong to no detective's turn.
const KIND_LABELS: Partial<Record<ChatLogEntry["kind"], string>> = {
  round_finalized: "Finalized",
  round_result: "Result",
};

// Matches any agent's display name ("Agent Red", etc.) wherever it appears in a line - built
// once from AGENT_LABEL_TO_ID's keys rather than assuming a fixed prefix position, since some
// lines are built by the frontend (label always leads) and some are the AI's own free-text
// debate transcript (a name could appear anywhere, e.g. "Agent Blue is wrong about node 45").
// Escaped defensively even though none of the current names contain regex metacharacters.
const AGENT_NAME_PATTERN = new RegExp(
  `(${Object.keys(AGENT_LABEL_TO_ID)
    .map((name) => name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .join("|")})`,
  "g",
);

function ColoredLine({ text }: { text: string }) {
  const parts = text.split(AGENT_NAME_PATTERN);
  return (
    <>
      {parts.map((part, i) => {
        const agentId = AGENT_LABEL_TO_ID[part];
        if (!agentId) return part;
        return (
          <span key={i} style={{ color: toCssColor(AGENT_COLORS[agentId]), fontWeight: 600 }}>
            {part}
          </span>
        );
      })}
    </>
  );
}

// How close to the bottom (px) counts as "already at the bottom" for auto-scroll purposes.
const NEAR_BOTTOM_THRESHOLD = 24;

interface ChatLogProps {
  entries: ChatLogEntry[];
  connectionError: string | null;
  onRetry: () => void;
}

export function ChatLog({ entries, connectionError, onRetry }: ChatLogProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const wasNearBottomRef = useRef(true);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    // Only auto-scroll if the user hadn't scrolled up to reread something - otherwise a new
    // event arriving would yank their view back down to the bottom mid-read.
    if (wasNearBottomRef.current) {
      el.scrollTo({ top: el.scrollHeight });
    }
  }, [entries]);

  function handleScroll() {
    const el = scrollRef.current;
    if (!el) return;
    wasNearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_THRESHOLD;
  }

  return (
    <section
      aria-labelledby="chat-log-heading"
      style={{ display: "flex", flexDirection: "column", minHeight: 0 }}
    >
      <h3 id="chat-log-heading" style={{ margin: "0 0 8px" }}>Chat Log</h3>
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        // A round streams in one entry per LLM call over a minute or more, and without this a
        // screen-reader user gets silence for the entire time the detectives are deliberating.
        // role="log" is the role defined for exactly this shape - a running list appended to at
        // the end - and carries an implicit polite live region, so new entries are announced
        // without interrupting. aria-relevant="additions" keeps the auto-scroll above from
        // re-announcing anything it merely moved.
        role="log"
        aria-relevant="additions"
        style={{
          border: "1px solid #ddd",
          borderRadius: 4,
          padding: 8,
          height: 220,
          overflowY: "auto",
          fontSize: 12,
          display: "flex",
          flexDirection: "column",
          gap: 8,
        }}
      >
        {entries.length === 0 && <p style={{ color: "#666", margin: 0 }}>No turns taken yet.</p>}
        {entries.map((entry, index) => {
          const previous = entries[index - 1];
          // A turn boundary - a new detective picking up the round, or the round ending - gets
          // the prominent header. Everything inside one turn (the mover's proposal, the four
          // responses, the mover's decision) runs on unbroken underneath it, which is what
          // makes the log read as five turns rather than thirty loose messages.
          const newTurn = previous?.round !== entry.round || previous?.turn !== entry.turn;
          const heading = entry.turn
            ? `Round ${entry.round} - ${DETECTIVE_LABELS[entry.turn]}'s turn`
            : `Round ${entry.round} - ${KIND_LABELS[entry.kind] ?? ""}`;
          return (
            <div key={entry.id} style={{ marginTop: newTurn && index > 0 ? 4 : 0 }}>
              {newTurn && index > 0 && (
                <hr style={{ border: "none", borderTop: "2px solid #999", margin: "0 0 8px" }} />
              )}
              {newTurn && (
                <div
                  style={{
                    fontWeight: 600,
                    color: entry.turn ? toCssColor(AGENT_COLORS[entry.turn]) : "#444",
                  }}
                >
                  {heading}
                </div>
              )}
              {entry.lines.map((line, i) => (
                <div
                  key={i}
                  style={{
                    whiteSpace: "pre-wrap",
                    // The mover's own two messages bracket the turn; the four responses sit
                    // indented between them, so the shape of a turn is visible at a glance.
                    paddingLeft: entry.kind === "response" ? 12 : 0,
                    fontWeight: entry.kind === "decision" ? 600 : 400,
                  }}
                >
                  <ColoredLine text={line} />
                </div>
              ))}
            </div>
          );
        })}
      </div>
      {connectionError && (
        <div
          role="alert"
          style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 4 }}
        >
          <p style={{ fontSize: 12, color: "var(--color-error)", margin: 0 }}>{connectionError}</p>
          <button onClick={onRetry}>Retry</button>
        </div>
      )}
    </section>
  );
}
