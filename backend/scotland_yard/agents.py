"""
One detective's turn, as driven by graph.py's `turn` node.

Play is turn-wise (ADR-0009): Mr. X moves, then the five detectives take their turns one at a
time in DETECTIVE_IDS order. A single turn is three phases and six LLM calls:

    1. The mover proposes a destination for itself and broadcasts it.          (1 call)
    2. Every other detective responds once, sequentially, in cyclic order      (4 calls)
       starting from the mover's immediate successor. Each response is
       ADVISORY - a responder states its own current preference but reserves
       nothing and is free to decide differently on its own turn.
    3. The mover, having heard all four, commits its final destination.        (1 call)

The move is then APPLIED immediately - the detective physically moves, its ticket transfers to
Mr. X, and capture is checked - so the next detective deliberates against a board that already
reflects it (rules.md section 2: "A full Round consists of Mr. X moving first, followed by
Detectives 1 through 5 moving in sequential order", and "At the start of each turn, the active
AI agent receives the current board state"). The turn then waits for the client to finish
animating that pawn before returning, so the next detective does not start deliberating over a
board that is still visibly rearranging itself. See ADR-0010.

This module is now just the turn's ORCHESTRATION - the sequence above, and the move application
that ends it. The three things a turn is assembled from each live in their own module, because
each changes for a different reason and on a different cadence:

    candidates.py  what a detective's options are, and the deterministic numbers scoring them.
    prompts.py     the Mr. X zone intel and the psychology/guidance text built from it.
    llm_calls.py   the response schemas, the bounded call, and the legality enforcement.

Nothing here trusts the model's output: every node an LLM names is re-checked against a legal
move set this application computed itself, and a failed check is resolved deterministically
rather than retried indefinitely (llm_calls.py).
"""
import json
import logging

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer

from .candidates import (
    annotate_onward_options,
    annotate_revisits,
    annotate_zone_distances,
    annotate_zone_shrink,
    fetch_legal_moves,
    other_detective_nodes,
    sort_candidates,
)
from .llm_calls import MoveChoice, TurnResponseChoice, _choose_move, _invoke
from .llm_client import get_debate_llm, get_detective_llm
from .prompts import (
    compute_mrx_zone_context,
    format_board_block,
    format_mrx_zone_block,
    format_options_block,
    get_psychology_prompt,
)
from .rules_constants import (
    AGENT_DISPLAY_NAMES,
    DETECTIVE_IDS,
    NUM_DETECTIVES,
    TURN_ACK_TIMEOUT_SECONDS,
)
from .state import ScotlandYardState
from .transport import determine_move_transport

logger = logging.getLogger(__name__)


def responders_for(mover_id: str) -> list[str]:
    """
    The order the other four detectives respond in during `mover_id`'s turn: cyclic
    DETECTIVE_IDS order starting from the mover's immediate successor, so Agent Green's turn is
    answered by Orange, Purple, Red, Blue. Every non-mover responds exactly once.
    """
    start = DETECTIVE_IDS.index(mover_id)
    return [DETECTIVE_IDS[(start + offset) % NUM_DETECTIVES] for offset in range(1, NUM_DETECTIVES)]


def apply_detective_move(state: ScotlandYardState, det_id: str, target_node: int) -> dict:
    """
    Physically moves one detective and pays for it, returning the fields to merge into state.

    Applied the moment a turn ends rather than batched at the end of the round (ADR-0010), so
    the next detective reasons about - and the board shows - a position that is already real.
    rules.md section 2 describes exactly this ordering ("Detectives 1 through 5 moving in
    sequential order"), and section 3's occupancy rule then falls out for free: the node this
    detective vacates is genuinely unoccupied for whoever moves next.

    Legality is re-derived here rather than trusted from the turn that chose it - the same
    posture resolve_round has always taken toward the graph's output. A target that is not
    actually reachable is a forfeit (rules.md section 3: no ticket spent, no ticket
    transferred), not a crash, and is logged at WARNING because it always means an upstream bug.

    Returns {"detectives", "mr_x", "from_node", "transport", "captured"}.
    """
    detective = dict(state["detectives"][det_id])
    mr_x = dict(state["mr_x"])
    from_node = detective["node_id"]

    transport = None
    if target_node != from_node:
        transport = determine_move_transport(
            detective, target_node, other_detective_nodes(state, det_id))
        if transport is None:
            logger.warning(
                "%s could not legally move from Node %s to Node %s - forfeiting the turn.",
                det_id, from_node, target_node)
        else:
            ticket_key = f"{transport}_tickets"
            detective[ticket_key] -= 1
            mr_x[ticket_key] += 1  # Rules: a detective's spent ticket transfers to Mr. X.
            detective["node_id"] = target_node

    return {
        "detectives": {**state["detectives"], det_id: detective},
        "mr_x": mr_x,
        "from_node": from_node,
        "transport": transport,
        # Checked here, the instant this detective lands, because the rules end the game at
        # that moment - the detectives still to move this round never get their turn.
        "captured": detective["node_id"] == mr_x["current_node"],
    }


# --- THE TURN NODE ---

async def turn_node(state: ScotlandYardState, config: RunnableConfig) -> dict:
    """
    Runs the whole turn for whichever detective `state["turn_index"]` points at, applies its
    move, and waits for the board to finish animating that move before returning.

    Emits a custom stream event per LLM call rather than one per node. Under the old design a
    stage's five calls landed on the client as a single blob once the last of them finished;
    here each of the six calls is surfaced the moment it completes, which is what lets the
    Chat Log read as a conversation unfolding rather than three bursts per loop.

    `config` carries the GameSession under "configurable"/"session" (round_resolver passes it),
    used only for the end-of-turn pawn-animation handshake. A session is absent whenever the
    graph is driven directly (tests, the -m llm runners), and the turn then simply does not
    wait - the handshake is presentation timing, never correctness.

    The RunnableConfig annotation is load-bearing, not decoration: LangGraph decides whether to
    hand a node the config by inspecting that annotation, and a plain `dict` hint silently gets
    nothing passed at all rather than failing.
    """
    mover = DETECTIVE_IDS[state["turn_index"]]
    mover_name = AGENT_DISPLAY_NAMES[mover]
    committed = dict(state.get("committed_moves", {}))
    round_number = state["round_number"]
    logger.info("--- ROUND %d: %s'S TURN (%d/%d) ---",
                round_number, mover_name.upper(), state["turn_index"] + 1, NUM_DETECTIVES)

    writer = get_stream_writer()
    session = (config or {}).get("configurable", {}).get("session")

    # Recomputed per turn, not memoized per round: detectives physically move as their turns
    # end, so the occupancy this BFS is blocked through genuinely differs between turns.
    zone_context = compute_mrx_zone_context(state)
    zone_block = format_mrx_zone_block(zone_context)
    distances_to_zone = zone_context["distances_to_zone"] if zone_context else {}

    # Options are needed for the mover AND for every responder that has not moved yet, since a
    # responder argues about its own next move as well as the mover's. Detectives that already
    # moved are excluded: they have no move left to make this round, so computing options for
    # them would be both wasted work and actively misleading to put in front of them.
    # One pass, so all six calls see the same board.
    still_to_move = [det_id for det_id in DETECTIVE_IDS if det_id not in committed]
    legal_moves_context, legal_move_sets = fetch_legal_moves(state, still_to_move)
    annotate_zone_distances(legal_moves_context, distances_to_zone)
    zone_nodes = zone_context["zone_nodes"] if zone_context else []
    has_revisit = {}
    for det_id in still_to_move:
        annotate_onward_options(state, det_id, legal_moves_context)
        annotate_zone_shrink(state, det_id, legal_moves_context, zone_nodes)
        has_revisit[det_id] = annotate_revisits(state, det_id, legal_moves_context)
    # Last, so it orders on the finished annotations rather than a partially-built candidate.
    sort_candidates(legal_moves_context)

    board_block = format_board_block(state, zone_block, committed)
    mover_options = format_options_block(
        mover, state, legal_moves_context, distances_to_zone, has_revisit.get(mover, False))

    # --- PHASE 1: the mover proposes, and broadcasts it ---
    writer({"event": "turn_started", "detective": mover})
    detective_llm = await get_detective_llm()
    move_llm = detective_llm.with_structured_output(MoveChoice)

    proposal_prompt = f"""
        You are {mover_name}. It is YOUR turn to move this round.
        {get_psychology_prompt(round_number, mover)}
        {board_block}
        {mover_options}

        Task: Choose the destination YOU will move to, and explain why in 2-3 sentences. This
        is a PROPOSAL you are broadcasting to the rest of the team - they will each respond to
        it, and you will get to revise it afterwards. Weigh the annotations exactly as the
        HOW TO READ YOUR OPTIONS block above tells you to.
        YOU MUST CHOOSE FROM THE LEGAL DESTINATIONS LISTED ABOVE.
    """
    proposed_node, proposal_rationale = await _choose_move(
        move_llm, proposal_prompt, mover, legal_move_sets[mover], state, "proposal")
    logger.info("[%s PROPOSAL] Node %s - %s", mover.upper(), proposed_node, proposal_rationale)
    writer({
        "event": "turn_proposal", "detective": mover,
        "target_node": proposed_node, "rationale": proposal_rationale,
    })

    # --- PHASE 2: the other four respond, sequentially ---
    debate_llm = await get_debate_llm()
    response_llm = debate_llm.with_structured_output(TurnResponseChoice)
    responses = []
    transcript = []

    for responder in responders_for(mover):
        responder_name = AGENT_DISPLAY_NAMES[responder]
        # A responder that already took its turn this round has nothing left to decide. Showing
        # it a menu of destinations and asking what it "would take on its own turn" would be
        # straightforwardly false - its turn is over and its node is locked in. It still gets a
        # voice on where the mover goes, which is the point of it being asked at all.
        has_moved = responder in committed
        if has_moved:
            own_position_block = (
                f"        You have ALREADY taken your turn this round and moved to Node "
                f"{committed[responder]}. You are standing there now and have no move left to "
                f"make this round. Argue from where you actually are."
            )
            preference_task = (
                f"Answer with Node {committed[responder]} - the node you are standing on - as "
                "your preferred_node, since your move this round is already made."
            )
        else:
            own_position_block = format_options_block(
                responder, state, legal_moves_context, distances_to_zone,
                has_revisit.get(responder, False))
            preference_task = (
                "Then state which node YOU would take on your own turn, chosen from YOUR legal "
                "destinations above. Your answer is advisory: it reserves nothing and you may "
                "decide differently when your own turn comes."
            )
        # Only what was said EARLIER IN THIS TURN (ADR-0009): each turn's transcript starts
        # fresh, so the last responder's prompt is no larger than the first's and per-round
        # token cost stays flat across all five turns. What earlier turns decided is still
        # visible - as committed moves on the board, which is the part that actually binds.
        transcript_so_far = "\n".join(transcript) if transcript else "You are the first to respond."

        response_prompt = f"""
        You are {responder_name}. It is {mover_name}'s turn to move, not yours - you are
        responding to their proposal.
        {get_psychology_prompt(round_number, responder)}
        {board_block}

        {mover_name.upper()}'S PROPOSAL: move to Node {proposed_node}.
        Their reasoning: {proposal_rationale}
        The full set of destinations {mover_name} could have chosen from:
        {json.dumps(legal_moves_context.get(mover, []), indent=2)}

        YOUR OWN POSITION (for deciding what YOU would do next):
        {own_position_block}

        Responses already given to {mover_name} this turn:
        {transcript_so_far}

        Task: Respond to {mover_name}'s proposal in 2-3 sentences - back it, or say concretely
        why a different destination from their list would serve the team (or you) better. Do
        not simply repeat what an earlier responder said. {preference_task}
        """

        answer = await _invoke(response_llm, response_prompt, f"{responder} response to {mover}")
        if answer is None:
            text = "(no response - the call failed)"
            preferred = None
        else:
            text = answer.response
            preferred = answer.preferred_node
            if has_moved:
                # Nothing for it to state - its move is already settled. Its own committed node
                # is the truth regardless of what it answered, so use that rather than trusting
                # the model to echo it back correctly.
                preferred = committed[responder]
            elif preferred not in legal_move_sets.get(responder, set()):
                # An advisory preference is discarded rather than reassigned when it is illegal.
                # The old vote tally took the same line with a bad ballot entry: rewriting it to
                # the lowest-numbered legal node would manufacture an opinion the detective never
                # held, and that invented node would then feed into the mover's decision prompt.
                logger.warning(
                    "[VALIDATION] %s's stated preference (Node %s) is not one of its legal "
                    "moves and was dropped from the transcript.", responder, preferred)
                preferred = None

        responses.append({"responder": responder, "response": text, "preferred_node": preferred})
        if preferred is None:
            intent = ""
        elif has_moved:
            intent = f" (already committed to Node {preferred})"
        else:
            intent = f" (would take Node {preferred} itself)"
        transcript.append(f"[{responder_name}]: {text}{intent}")
        logger.info("[%s -> %s] %s%s", responder.upper(), mover.upper(), text, intent)
        writer({
            "event": "turn_response", "detective": responder, "responding_to": mover,
            "response": text, "preferred_node": preferred,
        })

    # --- PHASE 3: the mover decides ---
    decision_prompt = f"""
        You are {mover_name}. It is YOUR turn to move, and you must now commit.
        {get_psychology_prompt(round_number, mover)}
        {board_block}
        {mover_options}

        YOUR OWN PROPOSAL WAS: Node {proposed_node} - {proposal_rationale}

        WHAT THE REST OF THE TEAM SAID ABOUT IT:
        {chr(10).join(transcript)}

        Task: Commit to the destination you will actually move to, and explain why in 2-3
        sentences. Keep your original proposal or change it - apply your COLLABORATION
        TENDENCY above when deciding how much weight the responses deserve against your own
        read. This decision is FINAL and takes effect immediately; the detectives who have not
        moved yet will have to work around it.
        YOU MUST CHOOSE FROM THE LEGAL DESTINATIONS LISTED ABOVE.
    """
    committed_node, decision_rationale = await _choose_move(
        move_llm, decision_prompt, mover, legal_move_sets[mover], state, "final decision")

    # --- APPLY: the detective physically moves, and pays for it ---
    applied = apply_detective_move(state, mover, committed_node)
    from_node = applied["from_node"]
    transport = applied["transport"]
    final_node = applied["detectives"][mover]["node_id"]
    logger.info("[%s MOVED] Node %s -> Node %s via %s - %s",
                mover.upper(), from_node, final_node, transport, decision_rationale)
    if applied["captured"]:
        logger.info("[%s CAPTURED MR. X] at Node %s - the round ends here.", mover.upper(), final_node)

    # Arm the handshake BEFORE the client can possibly answer it: the ack is a reply to the
    # event emitted on the very next line, and arming afterwards would race a fast client.
    if session is not None:
        session.expect_pawn_ack(round_number, mover)
    writer({
        "event": "turn_decision", "detective": mover,
        "from_node": from_node, "target_node": final_node,
        "transport": transport, "rationale": decision_rationale,
        "captured": applied["captured"],
    })

    # Hold the turn open until that pawn has finished moving on the board, so the next
    # detective does not start deliberating over a board that is still rearranging itself.
    # Bounded (ADR-0010): nobody may be watching, so this must never be able to stall a round.
    if session is not None:
        acked = await session.await_pawn_settled(TURN_ACK_TIMEOUT_SECONDS)
        if not acked:
            logger.info(
                "No pawn-settled ack for %s within %.1fs - continuing without it (no client "
                "watching, or its tweens are throttled).", mover, TURN_ACK_TIMEOUT_SECONDS)

    turn_record = {
        "proposed_node": proposed_node,
        "proposal_rationale": proposal_rationale,
        "responses": responses,
        "from_node": from_node,
        "committed_node": final_node,
        "transport": transport,
        "decision_rationale": decision_rationale,
    }
    turn_summary = "\n".join(
        [f"[{mover_name} proposes Node {proposed_node}]: {proposal_rationale}"]
        + transcript
        + [f"[{mover_name} moves to Node {final_node}]: {decision_rationale}"]
    )

    # The node just vacated, kept for two rounds so annotate_revisits can see a shuttle forming.
    # Only recorded when the detective actually went somewhere: a forfeited turn leaves it
    # standing where it was, and "recently vacated" would then flag the node it is still on.
    previously_at = list((state.get("recent_positions") or {}).get(mover, []))
    if final_node != from_node:
        previously_at = (previously_at + [from_node])[-2:]

    return {
        "detectives": applied["detectives"],
        "mr_x": applied["mr_x"],
        "committed_moves": {mover: final_node},
        "recent_positions": {mover: previously_at},
        "turn_records": {mover: turn_record},
        "turn_index": state["turn_index"] + 1,
        "captured_by": mover if applied["captured"] else None,
        "messages": [AIMessage(content=turn_summary)],
    }
