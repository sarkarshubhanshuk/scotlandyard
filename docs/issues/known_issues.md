# Scotland Yard: Known Issues Log

## Purpose

The running reference log for known bugs, limitations, and identified-but-unresolved issues
across the codebase. This was split out of `docs/mechanics/game_mechanics.md`'s per-mechanic
"Known Limitations" subsections so the issue backlog can grow on its own timeline without
cluttering that file's "how the system works" narrative. Each mechanic's `### Known Limitations`
subsection in `game_mechanics.md` now just links here instead of holding inline text.

This is the first place to check before investigating a weird agent behavior, and the first
place to write down a new one once confirmed — don't let a re-discovered issue go unlogged just
because raising it feels obvious in the moment.

## How to Use This Log

Every entry follows this template:

- **ID** — sequential `ISSUE-NNN`, never reused even if the issue is later closed.
- **Status** — `Open`, `Fixed`, `Won't Fix`, or `Not Yet Built` (an intentional non-goal, not a
  bug — e.g. the frontend not existing yet).
- **Area** — which mechanic/file(s) it lives in.
- **Logged** — date first recorded in this log.
- **Description** — what's wrong and why it matters.
- **Evidence** — how it was observed/confirmed (log file, test, specific call), if applicable.
- **Fix** (if `Fixed`) or **Proposed Fix** (if `Open`) — what resolved it, or the direction a fix
  should take.

New issues get appended under the most relevant existing group heading, or a new group heading
if none fits. Don't renumber or delete old entries when one is fixed — flip its `Status` to
`Fixed` and fill in `Fix` instead, so the log stays a true history.

---

## Group A: Detective Move Decision Cycle (`propose_node` / `debate_node` / `vote_node`)

See `docs/mechanics/game_mechanics.md` §1 for how this cycle works.

### ISSUE-001 — MCP tool-result unwrapping bug silently broke move legality end-to-end

- **Status**: Fixed
- **Area**: `backend/agents.py` (`propose_node`, `vote_node`)
- **Logged**: 2026-09-01
- **Description**: `valid_moves_tool.ainvoke(...)` (an MCP tool call via `langchain-mcp-adapters`)
  never returns the MCP server's raw Python list — MCP results travel as content blocks, so the
  call returns `[{"type": "text", "text": "<JSON-encoded move list>", "id": "..."}]`. Both
  `propose_node`'s and `vote_node`'s `fetch_moves` closures read `m["target_node"]` directly off
  that wrapper instead of parsing the JSON string inside `"text"`, so `legal_move_sets[d_id]` was
  **always the empty set** for every detective, on every call. Effects: (1) `find_proposal_conflicts`
  flagged every single proposal as illegal regardless of correctness, forcing a needless retry
  every loop; (2) `propose_node`'s deterministic backup enforcement then had no legal-and-unclaimed
  fallback to reassign to, so it silently reverted every pending detective to their **current**
  node instead of a legal move — `propose_node`'s output never actually moved anyone; (3)
  `vote_node`'s tally discarded every vote as illegal, since `node not in legal_move_sets.get(...)`
  was always true.
- **Evidence**: `backend/llm_io_log_full_round_e2e.txt` CALL #6's input shows
  `"proposed Node 41 is not one of its legal moves []"` for every detective — the empty `[]` is
  the tell.
- **Fix**: Added `agents.py:parse_valid_moves()`, which unwraps the MCP text block and
  `json.loads()`s it before use; wired into both `fetch_moves` call sites.

### ISSUE-002 — Model sometimes leaks chain-of-thought into the `rationale` field, producing an illegal proposal

- **Status**: Open
- **Area**: `backend/agents.py:propose_node`, LLM behavior (`deepseek/deepseek-v4-flash-0731`)
- **Logged**: 2026-09-02
- **Description**: On an otherwise-identical prompt to its peers, one proposer's response can
  dump ~180 words of visible internal deliberation into the free-text `rationale` field (no
  length constraint on that field), cut off mid-sentence, and default every `*_move` field to
  that detective's **current** node — which is never a legal target (no self-loops in the
  graph) — instead of doing the actual reasoning to pick a real move. This both triggers an
  unnecessary retry (see ISSUE-001's now-fixed retry path — this is a separate, still-open cause
  of legitimate retries) and inflates token cost for that call.
- **Evidence**: `llm_io_log_full_round_e2e.txt` CALL #3 OUTPUT — rationale ends mid-thought
  ("...Wait"), all five `*_move` fields equal each detective's starting `node_id`.
- **Proposed Fix**: Likely the same root cause as ISSUE-006 (uncontrolled reasoning-token
  budget on this model) — capping/disabling reasoning mode should reduce how often this happens.
  Independently, consider an explicit `max_length` on the `rationale` `Field` in
  `build_strategy_schema`/`build_ballot_schema`, or an explicit "no chain-of-thought in this
  field" instruction in the prompt.

### ISSUE-003 — `debate_node` uses a tool-bound LLM for a text-only task, sometimes producing empty pitches

- **Status**: Fixed
- **Area**: `backend/agents.py:debate_node`, `backend/mcp_client.py:get_detective_llm`, `get_debate_llm`
- **Logged**: 2026-09-02
- **Description**: `debate_node` calls `llm.ainvoke(...)` using the same shared LLM instance
  `get_detective_llm()` returns to every node — bound with `get_valid_moves`, `get_node_info`,
  and `read_rules` via `bind_tools()`. Given a prompt full of bare node numbers with no board
  context (see ISSUE-005), the model can reasonably decide to call `get_node_info` to look them
  up instead of answering in prose. `debate_node` never executes or feeds back tool calls — it
  just reads `response.content`, which is empty when the model chose to call tools, and appends
  that empty string to the debate transcript with no error raised. This silently breaks the
  "pitch" the debate phase is supposed to produce.
- **Evidence**: `llm_io_log_full_round_e2e.txt` CALL #7/#8 OUTPUT — empty `content`, a
  `[tool_calls]` block of five `get_node_info` calls instead. CALL #8's own INPUT shows the
  damage propagating: `"Debate Transcript so far: [detective_1]: "` — nothing after the colon.
- **Proposed Fix**: Give `debate_node` an LLM call that cannot emit tool calls — either a
  separate non-tool-bound `ChatOpenAI` instance, or `tool_choice="none"` for this call
  specifically — so it's forced to answer in prose. `get_detective_llm()` currently hands the
  same tool-bound instance to all three nodes; only `propose_node`/`vote_node` actually need
  tools.
- **Update (2026-09-03)**: `get_psychology_prompt` now includes an explicit "use only the data
  given to you in this prompt, do not seek outside information" instruction (added as part of
  the ISSUE-005 fix), which should reduce how often the model reaches for `get_node_info` in the
  first place — but it's a prompt-level mitigation, not a structural fix; the LLM is still
  tool-bound for this call and `debate_node` still can't handle a tool-call response if one
  happens anyway. Status stays Open.
- **Fix (2026-09-05)**: Went with the first proposed option — `mcp_client.py` gained
  `get_debate_llm()`, a separately cached `ChatOpenAI` instance sharing `get_detective_llm()`'s
  OpenRouter config (via the new `_build_chat_llm()` helper both now call) but with **no**
  `bind_tools()` call. `debate_node` now calls `get_debate_llm()` instead of
  `get_detective_llm()`. With no tool schemas bound to the request at all, the model has nothing
  to call, so `response.content` is guaranteed to be real prose — this removes the root cause
  structurally rather than relying on prompt instructions the model might not follow. As a side
  effect, debate calls no longer pay the token cost of the unused tool schemas either. Status
  now Fixed.

### ISSUE-004 — Vote prompts never include the structured proposals, only the (currently broken) debate transcript

- **Status**: Fixed
- **Area**: `backend/agents.py:vote_node` (`cast_ballot`), `debate_node`, `build_annotated_proposals`, `build_debate_position_schema`
- **Logged**: 2026-09-02
- **Description**: `cast_ballot`'s prompt includes only `debate_transcript` (free text) — never
  `state["proposed_strategies"]` (the structured `proposed_board_moves` + `rationale` JSON),
  even though `propose_node`'s and `debate_node`'s own prompts do include it. A 2-3 sentence
  pitch is a lossy summary of a 5-detective, 5-node proposal; relying on it alone (especially
  once ISSUE-003 is fixed and pitches are non-empty again) is a fragile way to ensure every
  voter — including a detective voting on their own proposed move — actually understands what
  was proposed.
- **Evidence**: compare `vote_node`'s `cast_ballot` prompt construction against `propose_node`'s
  `board_state`/`legal_moves_context` inclusion and `debate_node`'s `proposals_context` inclusion
  in `backend/agents.py`.
- **Proposed Fix**: Include `json.dumps(state["proposed_strategies"], indent=2)` in the vote
  prompt alongside the debate transcript, the same way `debate_node` already does.
- **Update (2026-09-03)**: As part of the ISSUE-005 fix, `vote_node` now also builds and shows
  `legal_moves_context` (the candidate nodes each still-undecided detective could move to, same
  as `propose_node` already displayed) — so ballots are no longer *purely* dependent on the
  debate transcript. Still missing: the actual `proposed_strategies` rationale/reasoning behind
  each proposal. Status stays Open for that gap.
- **Update (2026-09-05)**: ISSUE-003 (the "currently broken" debate transcript this issue's
  title refers to) is now Fixed, so the transcript `cast_ballot` reads is no longer at risk of
  silently blank pitches. This issue's own gap is unrelated and still open: ballots still never
  see `state["proposed_strategies"]` directly, only its lossy prose summary.
- **Fix (2026-09-05)**: Two changes, going beyond the originally proposed fix:
  - `build_annotated_proposals(state, distances_to_zone, pending_targets)` extracts
    `debate_node`'s existing proposal-annotation logic into a shared helper, now called by both
    `debate_node` and `vote_node` so they see an identical projection of the same data. Scoped
    to `pending_targets` when given (both callers now pass it) — a proposer's full 5-target
    board would otherwise duplicate already-locked targets, which are shown separately via
    "Already Locked Moves" wherever this appears.
  - `cast_ballot`'s prompt now includes this scoped `proposals_context` (the structured
    pre-debate proposals) alongside a new `debate_positions_context`: each debate speaker's own
    structured post-debate stance per pending target, captured via a new
    `build_debate_position_schema` field (`{target}_position: int`) added to `debate_node`'s
    existing per-speaker structured call — same call count, no added latency. This closes the
    deeper gap noted above: voters previously had only a frozen pre-debate proposal and a
    lossy prose transcript, with no structured record of what anyone actually landed on *after*
    debate. See `docs/mechanics/game_mechanics.md` Phase 2/3 for the full mechanism.
  - Side effect: `debate_node`'s per-speaker call is now a structured-output call (previously
    plain text), which exposes it to the same failure mode `ISSUE-006`/`007` already document
    for `propose_node`/`vote_node` (reasoning-budget exhaustion, parse failure). Mitigated the
    same way `cast_ballot` already handles it: a per-speaker try/except drops that speaker's
    pitch/position on failure rather than inventing a fallback value — see `ISSUE-006`.

### ISSUE-005 — No board-topology/connectivity context is ever given to the LLM

- **Status**: Fixed
- **Area**: `backend/agents.py:propose_node`, `debate_node`, `vote_node`, `backend/game_master.py`
- **Logged**: 2026-09-02
- **Description**: Every prompt (propose/debate/vote) only ever showed a flat list of *this
  turn's* legal target nodes as bare node IDs — no adjacency, no geography, no sense of which
  nodes are near Mr. X's last-known location beyond the number itself. The model had no way to
  reason spatially about "cutting off routes" the game's own psychology prompts ask it to reason
  about.
- **Evidence**: `llm_io_log_full_round_e2e.txt` CALL #3 OUTPUT explicitly surfaced the gap ("we
  don't have full map... Need know Scotland Yard board? Node numbers and connections") and was
  the likely trigger for ISSUE-003's `get_node_info` tool-call attempts.
- **Fix**: Went with option (2) from the original analysis (server-side precompute, no extra LLM
  round-trip), refined after a scale check against `docs/map/map.json` showed the naive version
  would have been counterproductive: at 4 hops, Mr. X's "possible zone" can cover up to 84% of
  the 199-node board, and a full candidate-move × zone-node distance matrix would have run to
  ~7,500 numbers in the worst case — exactly the kind of prompt bloat likely to make ISSUE-002/
  006/007 worse, not better. Implemented instead as:
  - `game_master.py:compute_mrx_zone` — BFS from Mr. X's last-known node, hop-capped at
    `min(turns_since_surfacing, 4)`, blocked through currently-occupied detective nodes per
    rules.md. Ticket-blind by deliberate choice — see ISSUE-015.
  - `game_master.py:compute_distances_to_zone` — one multi-source BFS from the whole zone,
    giving every board node's hop-distance to the nearest zone node in a single pass instead of
    one BFS per candidate.
  - `agents.py:compute_mrx_zone_context`/`format_mrx_zone_block` — renders the zone as a
    literal node list only when it's ≤20 nodes (`MRX_ZONE_LIST_THRESHOLD`); above that, a count
    only, since a huge list carries essentially no narrowing-down value. Returns `None` (handled
    explicitly) during rounds 1-2, before Mr. X has ever surfaced.
  - Every legal-move candidate in `propose_node`/`vote_node`, every already-proposed destination
    in `debate_node`, and each detective's own current position now carry a single
    `distance_to_mrx_zone` integer — not a full matrix. Measured real-data cost: ~304 tokens
    added per propose/vote call in a typical (count-only zone) case.
  - `get_psychology_prompt` (shared by all three nodes) also gained an explicit "use only the
    data given to you in this prompt, do not seek outside information" instruction, which
    doubles as a partial mitigation for ISSUE-003 (still open — see that entry).
- **Update (2026-09-05)**: `compute_mrx_zone_context` was being called fresh from each of
  propose/debate/vote on every debate-loop iteration - up to 9 redundant BFS runs per round,
  since its inputs (`last_known_node`/`last_known_round`, `round_number`, occupied detective
  nodes) are identical across all of them within one round. Added
  `agents.py:get_mrx_zone_context`, which memoizes the result on a new `state["mrx_zone_context"]`
  field (key presence, not truthiness, marks it computed - `None` is a legitimate pre-reveal
  value) so it's computed once per round instead of up to 9 times. `game_master.py:get_node_info`
  was also switched from an O(n) linear scan of `map_data` to an O(1) dict lookup
  (`node_index`), which this BFS calls once per node expanded.

### ISSUE-015 — Mr. X's possible-zone computation is ticket-blind

- **Status**: Won't Fix (deliberate, confirmed decision)
- **Area**: `backend/game_master.py:compute_mrx_zone`
- **Logged**: 2026-09-03
- **Description**: `compute_mrx_zone`'s BFS treats every board edge as traversable regardless of
  Mr. X's actual remaining ticket inventory — e.g. it won't prune a metro-only path 3 hops away
  even if he's already spent his last metro and black tickets. This means the "possible zone"
  shown to detectives is a strict superset of his true reachable set; it can never wrongly
  exclude his real location, but it can include nodes he genuinely couldn't reach.
- **Fix**: Not planned — explicitly decided against during the ISSUE-005 design discussion, to
  avoid modeling hypothetical multi-hop ticket-spending paths (materially more complex, and the
  set of tickets that could validly reach a given node only grows harder to reason about as Mr.
  X's inventory shrinks over the game). Revisit only if the zone's accuracy becomes a real
  gameplay problem in practice.

### ISSUE-016 — Nearest-zone-node-per-detective field considered, not implemented

- **Status**: Won't Fix (deliberate, deferred decision)
- **Area**: `backend/agents.py:compute_mrx_zone_context`, `get_mrx_zone_context`
- **Logged**: 2026-09-05
- **Description**: Proposal was to extend `current_distances_to_zone` (currently one hop-count
  scalar per still-undecided detective) with the specific zone node ID that achieves that
  minimum distance, on the theory that a concrete node reference would let detectives reason
  more precisely and coordinate a triangulation strategy. Feasible cheaply: `_bfs_from` would
  need one extra `nearest_source[node] = source_node_id` dict tracked alongside `visited`,
  seeded per zone node, no added asymptotic cost.
- **Decision**: Not implemented. Risks outweighed the unproven benefit:
  - **Tie ambiguity**: multiple zone nodes frequently share the minimum distance; surfacing one
    arbitrary winner (BFS visitation order) implies false precision.
  - **Convergence, not triangulation**: each detective's own nearest zone node tends to be
    whichever zone-boundary node faces the detectives' side of the board — several detectives
    would likely compute the *same* nearest node, pulling them toward one point rather than
    spreading coverage across the zone's different approach vectors, which is the opposite of
    what triangulation needs.
  - **Cost on an already-constrained model**: added prompt surface for a model already showing
    reasoning-budget/latency problems (ISSUE-006/007), for a signal whose strategic value was
    not empirically validated.
- **Alternative on the table if revisited**: a nearest-*detective*-per-zone-node assignment
  (Voronoi-style partition of the zone) instead of nearest-zone-node-per-detective — this
  directly surfaces non-overlapping coverage ("detective_1 covers this side of the zone,
  detective_2 covers that side") rather than pulling detectives toward a shared point. More
  expensive to compute/render than the scalar-per-detective version, and only meaningful while
  the zone-list-threshold path (`MRX_ZONE_LIST_THRESHOLD`, ≤20 nodes) is active, since the zone
  itself isn't shown to the model above that size either.

---

## Group B: LLM Provider / Model Behavior (`deepseek/deepseek-v4-flash-0731` via OpenRouter)

### ISSUE-006 — Uncontrolled reasoning-token budget causes high, highly variable latency and outright failures

- **Status**: Fixed (2026-09-05) — `reasoning.enabled=False`, validated against real traffic across 4 configurations; see final Fix entry below
- **Area**: `backend/mcp_client.py:_build_chat_llm` (shared by `get_detective_llm`, `get_debate_llm`)
- **Logged**: 2026-09-02
- **Update (2026-09-05)**: As part of the ISSUE-004 fix, `debate_node`'s per-speaker call is now
  a `with_structured_output` call too (previously plain text) — it's exposed to this same
  reasoning-budget failure mode going forward, mitigated the same way `cast_ballot` already
  handles it (drop that speaker's contribution on failure, no fallback value invented).
- **Description**: `deepseek/deepseek-v4-flash-0731` is a hybrid reasoning model, and nothing in
  `mcp_client.py`'s `ChatOpenAI(...)` config caps or disables its reasoning-token budget. Two
  observed consequences: (1) per-call latency for structurally identical propose calls ranged
  from 33s (an incorrect/rambling answer, see ISSUE-002) to 11m 27s, and vote calls ranged
  ~3.5-8.5 min; (2) on one call the model spent its entire completion budget on hidden reasoning
  and never emitted the structured answer at all, hard-failing the call.
- **Evidence**: `llm_io_log_full_round_e2e.txt` — CALL #1/#2/#4/#5 timestamps (2m21s / 9m27s /
  8m51s / 11m27s); CALL #12/#14/#15/#16 timestamps (3m30s / 4m7s / 3m23s / 8m25s); CALL #13
  ERROR (see ISSUE-007).
- **Proposed Fix**: Cap or disable the reasoning budget via OpenRouter's unified `reasoning`
  request parameter (effort level or explicit token cap, or `exclude: true`), or switch to a
  non-reasoning route/model — see ISSUE-008 for candidates.
- **Fix (2026-09-05)**: `_build_chat_llm()` now sets `max_tokens=4000` (native `ChatOpenAI`
  field) and `extra_body={"reasoning": {"max_tokens": 2000}}` (OpenRouter's extension, passed
  via `extra_body` since it isn't part of the standard OpenAI schema — a typed field wouldn't
  carry it). Both getters build on this helper, so the cap applies uniformly.
  - **Derivation**: `CALL #13`'s failure (see Evidence below) gives an empirical throughput of
    ~80 tokens/sec for this route (32768 completion tokens / 416s). `reasoning.max_tokens=2000`
    = a 45s latency target (matching the `timeout=45` above) × 80 tok/s, minus a ~1000-token
    reserve for the actual structured answer, rounded down for margin — about 15x below the
    33,933 reasoning tokens that call actually consumed, so that specific failure becomes
    structurally impossible rather than just less likely. `max_tokens=4000` is a second,
    independent guard (reasoning cap + answer reserve + margin) so the total completion ceiling
    itself can't be fully consumed by reasoning even if the reasoning cap is ever ignored.
  - **Not done**: the adaptive-retry option discussed alongside this (retry once with
    `reasoning: {exclude: true}` if the static cap still isn't enough) was deliberately deferred
    — static cap only, for now. Revisit if live traffic still shows failures/high variance at
    this cap.
  - **Not yet done**: empirical validation against real game traffic (confirm no
    truncation/parse failures and actual p95 latency lands near the 45s target). Status will
    move to Fixed once that's checked; if the cap proves insufficient, see ISSUE-008.
- **Root Cause Analysis (2026-09-05)**: The static cap above was validated against real traffic
  (a partial `test_full_round_e2e.py` run) and found NOT to work — 10 of 30 calls
  (~33%) failed with `LengthFinishReasonError`, up from one isolated failure across an entire
  prior full-round run. Root-caused via raw, client-bypassing calls directly against the
  OpenRouter API:
  - The client-side mechanism is correct — `ChatOpenAI._default_params()` merges `extra_body`
    into the request cleanly (no collision with the native, unset `reasoning` field), and the
    actual bound runnable for `debate_node` was confirmed to send
    `extra_body={"reasoning": {"max_tokens": 2000}}` exactly as configured.
  - OpenRouter serves `deepseek/deepseek-v4-flash-0731` via **self-hosted vLLM backends**
    (`system_fingerprint` values `vllm-dev-ep-4997cd02` and `vllm-0.26.0-dp4-ep-86dc62bb`
    observed across 4 identical replayed requests). **Neither backend honors
    `reasoning.max_tokens` at all** — reasoning ran to 2652/3618/4000/4000 tokens across those 4
    runs, never once stopping near the requested 2000-token cap. It is a silently-accepted
    no-op for this route, not a client bug.
  - `max_tokens` (the native, standard field) generally IS enforced as a hard ceiling — 3 of
    those 4 runs hit exactly `completion_tokens=4000` and failed; the 4th happened to finish
    reasoning at 2652 tokens and had enough of the 4000 budget left to still succeed. One
    earlier isolated replay of a real failing prompt even exceeded `max_tokens=4000` itself
    (`completion_tokens=7059`, `finish_reason=stop`), suggesting at least one further backend
    variant in the routing pool doesn't strictly enforce the outer ceiling either.
  - **Why the fix made things worse**: previously `max_tokens` was unset (defaulting to
    ~32768). Reasoning ran uncapped then too, but with that much headroom an overrun rarely
    consumed the entire budget. Cutting the ceiling to 4000 without a working reasoning
    sub-cap removed that headroom — any reasoning run past ~3500-4000 tokens (routine, per
    every measurement here) now exhausts the whole budget. Whether a given call succeeds is
    effectively down to which backend replica and how much it happens to reason that call.
  - **Conclusion**: the `reasoning` request parameter is not a usable lever for this specific
    OpenRouter-served model/route, for BOUNDING a budget. Options going forward: (a) raise
    `max_tokens` back up substantially (or remove it) to restore headroom, accepting the
    original latency variance over the new higher failure rate, or (b) treat ISSUE-008's
    non-reasoning candidate models as the only lever that structurally removes this failure
    class, since the budget-capping lever is now confirmed non-functional here rather than just
    unvalidated. (c), tried next and confirmed the fix — see final Fix entry below.
- **Fix (2026-09-05, final)**: Two further configurations were tested on real traffic (one full
  propose/debate/vote loop each, 15 calls/run) after the Root Cause Analysis above, landing on
  `reasoning.enabled=False`:
  - `reasoning.effort="low"`: 3/15 calls failed (20%), `reasoning_tokens` pinned at 3996-4000 on
    every failure — the ~800-token ("low" ≈ 20% of `max_tokens=4000`) target was ignored exactly
    like the explicit cap. Total loop wall-clock ~361s.
  - `reasoning.effort="minimal"` (not an OpenRouter-documented value for this route's effort
    enum): 2/15 failed (13%), `reasoning_tokens=4000` on every failure — same ignored-cap
    pattern, and the slowest run of all four (~557s total).
  - `reasoning.enabled=False`: 0/15 failed (0%), `reasoning_tokens` confirmed 0 on every call,
    ~30s total loop wall-clock — 12-19x faster than either effort-level attempt, and the only
    configuration with zero failures.
  - **Conclusion**: across four configurations (explicit token cap, `effort="low"`,
    `effort="minimal"`, `enabled=False`), every attempt to BOUND the reasoning budget failed
    identically — reasoning consistently ran to the full `max_tokens` ceiling regardless of the
    requested cap or effort level. Only the boolean `enabled=False` actually took effect,
    presumably because it skips the reasoning code path entirely rather than trying to constrain
    it. `_build_chat_llm()` is now set to `reasoning.enabled=False` with `max_tokens=4000` kept
    as an independent guard. Status: Fixed.
  - **Not done**: the adaptive-retry option and the "retry a dropped ballot once" idea (see
    ISSUE-007) remain unimplemented — `enabled=False`'s 0% failure rate across the tested traffic
    made them unnecessary for now; revisit if failures reappear under broader/longer traffic.

### ISSUE-007 — `LengthFinishReasonError`: model exhausts its token budget on reasoning and never answers

- **Status**: Fixed (2026-09-05) — via ISSUE-006's final fix (`reasoning.enabled=False`); see that entry
- **Area**: `backend/agents.py:vote_node` (`cast_ballot`), `backend/mcp_client.py:_build_chat_llm`
- **Logged**: 2026-09-02
- **Description**: A vote call failed outright with `LengthFinishReasonError`:
  `completion_tokens=32768`, `reasoning_tokens=33933` — the model burned its whole completion
  budget on hidden reasoning and never produced the structured vote JSON, so
  `with_structured_output`'s parser raised when `finish_reason == "length"`. Currently this
  detective's vote is just silently dropped (see `vote_node`'s `cast_ballot` try/except) rather
  than retried, so the round loses a full ballot whenever this happens.
- **Evidence**: `llm_io_log_full_round_e2e.txt` CALL #13 | DETECTIVE_2 | ERROR.
- **Proposed Fix**: Same lever as ISSUE-006 (cap/disable reasoning tokens) should prevent this
  outright; independently, consider whether a dropped ballot from this failure mode should be
  retried once rather than silently discarded, the way `propose_node` already retries once on a
  conflict.
- **Fix attempted (2026-09-05)**: Applied via ISSUE-006's `reasoning.max_tokens=2000`/
  `max_tokens=4000` cap. The independent "retry a dropped ballot once" idea was deliberately
  not implemented alongside it (static cap only); `cast_ballot` still silently drops a ballot
  that fails this way.
- **Root Cause Analysis (2026-09-05)**: The cap does not work — see ISSUE-006's Root Cause
  Analysis entry for the full investigation. Root cause is provider-side: OpenRouter's
  self-hosted vLLM backends for `deepseek/deepseek-v4-flash-0731` don't honor
  `reasoning.max_tokens` at all, so this exact failure (reasoning consuming the whole
  completion budget) remains possible — this issue's failure mode is not resolved. Reopened to
  Open; the "retry a dropped ballot once" idea from the original Proposed Fix is now the more
  relevant near-term mitigation, independent of whichever ISSUE-006 direction is chosen.
- **Fix (2026-09-05, final)**: ISSUE-006's final fix (`reasoning.enabled=False`, confirmed
  across a 15-call real-traffic run with 0 reasoning tokens and 0 failures) removes this
  failure's root cause directly — with reasoning disabled, `LengthFinishReasonError` from
  reasoning exhaustion cannot occur. The "retry a dropped ballot once" idea remains
  unimplemented (not needed given the 0% observed failure rate); `cast_ballot` still has no
  retry path if this or any other cause produces a dropped ballot in the future.

### ISSUE-008 — Candidate models to evaluate if reasoning-budget control on DeepSeek v4 isn't enough

- **Status**: Open (informational / not yet a decision)
- **Area**: `backend/mcp_client.py` (model selection)
- **Logged**: 2026-09-02
- **Description**: If ISSUE-006/007 aren't fully resolved by capping DeepSeek v4's reasoning
  budget, candidates in a similar OpenRouter cost tier with strong structured-output/tool-use
  reliability and no hidden, uncapped chain-of-thought by default: DeepSeek's own non-reasoning
  "chat" endpoint (same family, same cost tier, no runaway-reasoning failure mode); Qwen3
  Instruct variants (not "Thinking"); Kimi K2 (Moonshot, built for agentic/tool-use workloads);
  GLM-4.5-Air / GLM-4.6 (Zhipu); Llama 4 Maverick/Scout or Mistral Small/Large (classic non-CoT
  chat models — most predictable latency of the group).
- **Note**: This list reflects model knowledge current to ~January 2026; verify current OpenRouter
  pricing/availability before acting on it, since the field moves quickly.
- **Update (2026-09-05)**: ISSUE-006/007 are now Fixed via `reasoning.enabled=False` (fully
  disabling reasoning, not capping it) — confirmed 0% failures and ~30s loop latency on real
  traffic, after `reasoning.max_tokens`, `effort="low"`, and `effort="minimal"` all failed
  identically. This list is no longer an urgent fallback, but stays relevant as a possible
  further improvement: DeepSeek v4 flash with reasoning *disabled* is not the same as a model
  that was never a hybrid-reasoning model to begin with, and the candidates here may still offer
  better non-reasoning quality/latency/cost than DeepSeek v4 flash running reasoning-off.

### ISSUE-009 — `timeout=45` in `mcp_client.py` does not bound observed call latency; its own comment is stale

- **Status**: Open
- **Area**: `backend/mcp_client.py:get_detective_llm`
- **Logged**: 2026-09-02
- **Description**: The `ChatOpenAI(..., timeout=45, ...)` call site's own comment claims 45s
  "comfortably clears every observed successful call's latency" — but calls up to 11m 27s were
  observed to succeed without ever hitting that timeout (see ISSUE-006's evidence). The timeout
  is evidently being enforced as an idle/no-data-gap timeout by the underlying HTTP client
  (reset by each streamed chunk), not a hard wall-clock deadline, so a slow-but-still-streaming
  call sails through it. The comment should either be corrected to describe this behavior
  accurately, or the timeout strategy revisited if a real wall-clock cap is actually wanted.
- **Evidence**: same as ISSUE-006.
- **Proposed Fix**: Update the comment to describe the timeout's actual (idle-gap) semantics;
  separately decide whether a real hard deadline is wanted for this workload and add one
  explicitly if so.

---

## Group C: Round Resolution, Mr. X's Turn, and the API Layer

See `docs/mechanics/game_mechanics.md` §2 for how this mechanic works. These predate the
2026-09-02 session and were migrated here verbatim from that file's own Known Limitations
subsection.

### ISSUE-010 — No cross-voter, cross-tally collision detection within a single vote loop

- **Status**: Open
- **Area**: `backend/agents.py:vote_node`
- **Logged**: 2026-09-02 (migrated from `game_mechanics.md` §1)
- **Description**: `vote_node` does not check whether two *different* detectives' tallies both
  reach majority for the same node in the same loop — each detective's tally is computed
  independently. This is a cross-voter, cross-tally case, distinct from the within-one-ballot
  duplicate check `vote_node` already performs.
- **Proposed Fix**: Worth revisiting alongside ISSUE-013 below (both are about the same
  underlying gap: nothing currently guarantees two *locked* moves can't collide, only that a
  single proposal or a single ballot can't).

### ISSUE-011 — React/Phaser frontend does not exist yet

- **Status**: Fixed
- **Area**: Phase 4 (frontend)
- **Logged**: 2026-09-02 (migrated from `game_mechanics.md` §2)
- **Description**: The mechanic described in `game_mechanics.md` §2 was built specifically so
  frontend work can target a real, live backend from day one — not the other way around. This was
  an intentional non-goal at that stage, not a bug.
- **Fix (2026-09-11)**: Phase 4 built the full React + Phaser frontend across 5 milestones (board
  render, game/session wiring, Mr. X move UI, live AI debate visualization, travel log/win-loss)
  — see `CLAUDE.md` §3 for the architecture. Verified end-to-end against a real, unmocked backend
  including a full natural 14-round game (detectives won via Mr. X's ticket exhaustion, not
  capture) with a double-move and a surfacing-round reveal both exercised along the way.

### ISSUE-012 — `docs/map/node_positions.json` has never been visually spot-checked

- **Status**: Fixed/Verified
- **Area**: `docs/map/node_positions.json`, `tools/extract_node_positions.py`,
  `tools/node_coordinate_picker.html`
- **Logged**: 2026-09-02 (migrated from `game_mechanics.md` §2)
- **Description**: `node_positions.json` (per-node board pixel coordinates, needed for the
  frontend's board rendering) was generated programmatically from `board.svg`'s circle-marker +
  text-label geometry. `tools/node_coordinate_picker.html` exists to visually spot-check / hand-
  correct it, but no human has actually run that verification pass yet.
- **Verified (2026-09-05)**: Human visual spot-check performed via `tools/node_coordinate_picker.html`
  (all 199 markers + adjacency lines overlaid on `board.svg`) ahead of Phase 4 frontend work. No
  misplaced nodes found; no corrections needed to `node_positions.json`.

### ISSUE-013 — `test_full_round_stream_manual` has never actually been run

- **Status**: Fixed/Verified
- **Area**: `backend/test_api_smoke.py`
- **Logged**: 2026-09-02 (migrated from `game_mechanics.md` §2)
- **Description**: The one test that exercises a real, full `run_detective_loop` through the API
  has never been run — the Groq API key in `.env` was found expired while testing §1's hardening
  work, and refreshing it was a prerequisite. Note: the project has since moved to OpenRouter/
  DeepSeek as the configured LLM provider (see `CLAUDE.md`), so this note may now be
  stale as written — re-check which key/provider this test actually needs before acting on it.
- **Verified (2026-09-05)**: Confirmed the stale-key note — `round_resolver.py:run_detective_loop`
  calls `detective_graph` (`graph.py`), which is entirely `mcp_client.py`/OpenRouter-based; no
  Groq dependency exists anywhere in the current codebase. Corrected the docstring/comments in
  `test_api_smoke.py` from `GROQ_API_KEY` to `OPENROUTER_API_KEY`. Ran
  `test_full_round_stream_manual()` by hand ahead of Phase 4 frontend work: **PASSED**, event
  sequence `['proposal', 'debate', 'vote_tally', 'proposal', 'debate', 'vote_tally',
  'round_finalized', 'round_result']` (needed 2 loops to reach consensus - normal game flow, not
  a failure).

### ISSUE-017 — `test_full_round_e2e.py` crashed on a `UnicodeEncodeError` printing a detective's LLM output

- **Status**: Fixed
- **Area**: `backend/test_full_round_e2e.py`
- **Logged**: 2026-09-05
- **Description**: `debate_node`'s own console output (`print(f"[{det_id.upper()}]: {pitch}")`,
  `agents.py:515`) crashed the entire test process with `UnicodeEncodeError: 'charmap' codec
  can't encode characters...` when a detective's LLM-generated pitch contained a character
  outside Windows' default console codepage (`cp1252`) — an em-dash or curly quote is enough to
  trigger it. One earlier print in the same run partially garbled a character into `�` without
  crashing; a later one raised outright and killed the process before any assertion ran,
  discovered while comparing `reasoning.effort="low"` against `reasoning.enabled=False` (see
  ISSUE-006) on real traffic.
- **Fix (2026-09-05)**: Added `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` near
  the top of `test_full_round_e2e.py`, so any character outside stdout's default codepage is
  replaced rather than crashing the run, regardless of which model/content produced it.
- **Note**: This is a test-harness fix only. `agents.py`'s own `print()` calls (used by every
  node for console visibility during a real game run, not just this test) have the same latent
  exposure if ever run under a non-UTF-8 console — not fixed here, since this fix was scoped to
  unblocking the reasoning-comparison test specifically.

### ISSUE-014 — No defensive check in `resolve_round` for two detectives' final moves colliding

- **Status**: Open
- **Area**: `backend/round_resolver.py:resolve_round`
- **Logged**: 2026-09-02 (migrated from `game_mechanics.md` §2)
- **Description**: No detection exists for two *different* detectives' final moves colliding on
  the same node. This would only be possible if §1's own proposal/vote uniqueness enforcement
  had a bug, since `final_moves` is supposed to already be collision-free by construction —
  `resolve_round` does not add an extra defensive check for this. Related to ISSUE-010.
