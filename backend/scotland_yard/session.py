import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Literal, Optional

from .rules_constants import (
    DETECTIVE_IDS,
    DETECTIVE_STARTING_TICKETS,
    MR_X_STARTING_TICKETS,
    STARTING_NODE_POOL,
)
from .state import ScotlandYardState

logger = logging.getLogger(__name__)

# How long an untouched game survives before `create_game` sweeps it out of GAMES. Games are
# in-memory by design (ADR-0005), but "no persistence" and "no eviction" are separate
# decisions: without a TTL every abandoned game - including one created by a page refresh
# that was never played - leaks for the lifetime of the process. Two hours comfortably
# exceeds any realistic single sitting (a 24-round game is minutes of wall time).
SESSION_TTL_SECONDS = 2 * 60 * 60


@dataclass
class GameSession:
    """
    Owns one in-progress game's ScotlandYardState plus the orchestration bookkeeping that
    doesn't belong inside LangGraph state (whose turn it is, whether the game has ended, and a
    lock serializing concurrent submissions against this one game). No persistence/TTL - games
    live only as long as this process does, matching "a page refresh discards the game".
    """
    game_id: str
    state: ScotlandYardState
    status: Literal["awaiting_mr_x_move", "detective_loop_running", "game_over"] = "awaiting_mr_x_move"
    winner: Optional[Literal["detectives", "mr_x"]] = None
    # Which detective physically caught Mr. X, if that's how the game ended - None for every
    # other ending (Mr. X survives to round 24, all detectives are stranded, or Mr. X runs out
    # of legal moves without ever actually being landed on). Set once, in round_resolver.py's
    # _game_over, from state["captured_by"] (agents.py:apply_detective_move).
    winning_detective: Optional[str] = None
    # The browser that created this game, as an opaque bearer token also held in that browser's
    # HttpOnly cookie. This is the whole of the access control: a shared URL carries the game id
    # but not the cookie, so the recipient cannot act on someone else's game. There are no
    # accounts, so ownership is per-browser and ends when the cookie does.
    owner_token: Optional[str] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Monotonic timestamp of the last request that touched this game, maintained by
    # touch(). Drives TTL eviction in create_game; see SESSION_TTL_SECONDS.
    last_touched_at: float = field(default_factory=time.monotonic)

    # --- Pawn-animation handshake (ADR-0010) ---------------------------------------------
    # A detective's turn ends by physically moving its pawn, and the NEXT detective must not
    # start deliberating until that move has finished playing out on the board. The board is in
    # the browser, so the only way the turn loop can know is for the client to say so: it POSTs
    # /turn-ack when the tween completes, and `await_pawn_settled` releases.
    #
    # `awaiting_ack` names the exact move being waited on, as (round_number, detective_id). An
    # ack that does not match it is ignored rather than trusted - a late ack for the previous
    # turn must never release the current one, and a client is not something this code takes on
    # faith any more than an LLM is.
    awaiting_ack: Optional[tuple] = None
    _ack_event: asyncio.Event = field(default_factory=asyncio.Event)

    def touch(self) -> None:
        """Mark this game as still in use, deferring its TTL eviction."""
        self.last_touched_at = time.monotonic()

    def expect_pawn_ack(self, round_number: int, det_id: str) -> None:
        """Arms the handshake for one specific pawn move. Call BEFORE emitting turn_decision."""
        self.awaiting_ack = (round_number, det_id)
        self._ack_event.clear()

    def acknowledge_pawn_settled(self, round_number: int, det_id: str) -> bool:
        """
        Records that the client finished animating one pawn move. Returns whether the ack
        matched what the turn loop is actually waiting on - a mismatch is a no-op, not an error.
        """
        if self.awaiting_ack != (round_number, det_id):
            return False
        self._ack_event.set()
        return True

    async def await_pawn_settled(self, timeout: float) -> bool:
        """
        Blocks until the armed pawn move is acked, or `timeout` elapses. Returns whether a real
        ack arrived.

        Timing out is a normal outcome, not a failure: nobody may be watching (the round runs
        whether or not a client is listening - ISSUE-027), the tab may be backgrounded with its
        tweens throttled, or the connection may have dropped. The round must continue in every
        one of those cases, so this degrades to a plain bounded wait rather than stalling.
        """
        if self.awaiting_ack is None:
            return True
        try:
            await asyncio.wait_for(self._ack_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self.awaiting_ack = None
            self._ack_event.clear()


# Plain in-memory store - no database, deliberately not durable across restarts (ADR-0005).
# Bounded only by TTL sweeping, not by count; see _evict_stale_games.
GAMES: Dict[str, GameSession] = {}


def _evict_stale_games(now: Optional[float] = None) -> int:
    """
    Drops every game untouched for longer than SESSION_TTL_SECONDS. Swept inline - on each
    create_game, and on each active_game_count() - rather than from a background task, which
    keeps the module free of any background-task lifecycle to manage.

    Returns how many games were evicted (for logging/tests).
    """
    now = time.monotonic() if now is None else now
    stale = [
        game_id for game_id, session in GAMES.items()
        if now - session.last_touched_at > SESSION_TTL_SECONDS
    ]
    for game_id in stale:
        del GAMES[game_id]
    if stale:
        logger.info("Evicted %d game(s) idle for over %ds", len(stale), SESSION_TTL_SECONDS)
    return len(stale)


def active_game_count() -> int:
    """
    How many games are genuinely still alive - stale ones swept first.

    The sweep here is not an optimization, it is the entire point of the function. server.py's
    new-game cap (limits.MAX_ACTIVE_GAMES) is checked against this number, and eviction used to
    happen ONLY inside create_game - which sits downstream of that check. So a store that had
    filled up with abandoned games refused every new game with a 429 and, because the refusal
    returned before create_game ever ran, never swept the games causing the refusal. The service
    stayed wedged that way until the process restarted, taking every live game with it.

    Counting through this function is what makes the cap a bound on LIVE games rather than on
    accumulated litter. Anything checking capacity must ask here, never `len(GAMES)`.
    """
    _evict_stale_games()
    return len(GAMES)


def create_game(seed_positions: Optional[Dict[str, int]] = None) -> GameSession:
    """
    Starts a brand-new game: randomly (or, for tests, deterministically via seed_positions)
    assigns each of Mr. X and the 5 detectives a unique starting node from the rules' pool, and
    gives everyone their rules-mandated starting ticket inventory.

    seed_positions, when given, must map "mr_x" and each of DETECTIVE_IDS to a node id and
    bypasses the random draw entirely - lets tests construct a fixed, reproducible board.
    """
    if seed_positions is not None:
        positions = dict(seed_positions)
    else:
        drawn = random.sample(STARTING_NODE_POOL, 6)
        positions = {"mr_x": drawn[0]}
        for det_id, node in zip(DETECTIVE_IDS, drawn[1:]):
            positions[det_id] = node

    state: ScotlandYardState = {
        "round_number": 1,
        "turn_index": 0,
        "mr_x": {
            "current_node": positions["mr_x"],
            "last_known_node": None,
            "last_known_round": None,
            "transport_history": [],
            **MR_X_STARTING_TICKETS,
        },
        "detectives": {
            det_id: {"node_id": positions[det_id], **DETECTIVE_STARTING_TICKETS}
            for det_id in DETECTIVE_IDS
        },
        "messages": [],
        "committed_moves": {},
        "turn_records": {},
        "final_moves": {},
        "final_move_details": {},
        "recent_positions": {},
    }

    _evict_stale_games()
    session = GameSession(game_id=str(uuid.uuid4()), state=state)
    GAMES[session.game_id] = session
    logger.info("Created game %s (%d active)", session.game_id, len(GAMES))
    return session
