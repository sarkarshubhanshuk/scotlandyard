import { useEffect, useRef } from "react";
import type { ChatLogEntry } from "../hooks/useRoundStream";
import { AGENT_COLORS, AGENT_LABEL_TO_ID, toCssColor } from "../labels";

const KIND_LABELS: Record<ChatLogEntry["kind"], string> = {
  proposal: "Proposals",
  debate: "Debate",
  vote_tally: "Vote",
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
    <section style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
      <h3 style={{ margin: "0 0 8px" }}>Chat Log</h3>
      <div
        ref={scrollRef}
        onScroll={handleScroll}
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
        {entries.length === 0 && <p style={{ color: "#666", margin: 0 }}>No debate yet.</p>}
        {entries.map((entry, index) => {
          const previous = entries[index - 1];
          const sameRound = previous?.round === entry.round;
          // A "proposal" entry that isn't the round's very first entry can only follow a
          // previous loop's "vote_tally" (see check_vote_status in graph.py) - i.e. it marks a
          // new loop starting, which gets the more prominent solid separator. Any other same-
          // round transition (proposal->debate, debate->vote_tally, vote_tally->round_finalized)
          // is a stage change within the same loop, marked with a lighter dashed separator.
          const isNewLoop = sameRound && entry.kind === "proposal";
          const isStageChange = sameRound && !isNewLoop;
          return (
            <div key={entry.id}>
              {isNewLoop && <hr style={{ border: "none", borderTop: "2px solid #999", margin: "0 0 8px" }} />}
              {isStageChange && <hr style={{ border: "none", borderTop: "1px dashed #ccc", margin: "0 0 8px" }} />}
              <div style={{ fontWeight: 600, color: "#444" }}>
                Round {entry.round} - {KIND_LABELS[entry.kind]}
              </div>
              {entry.lines.map((line, i) => (
                <div key={i} style={{ whiteSpace: "pre-wrap" }}>
                  <ColoredLine text={line} />
                </div>
              ))}
            </div>
          );
        })}
      </div>
      {connectionError && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 4 }}>
          <p style={{ fontSize: 12, color: "#b00020", margin: 0 }}>{connectionError}</p>
          <button onClick={onRetry}>Retry</button>
        </div>
      )}
    </section>
  );
}
