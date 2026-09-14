"""
Spend and capacity caps for a public, account-free deployment.

The link is meant to be shareable and loginless, which means anyone who finds it can start
games - and a single completed game is roughly 720 LLM calls (CALLS_PER_TURN x 5 detectives x
up to 24 rounds). Without caps, "very low usage" is an assumption rather than a property.

None of this is a substitute for a hard credit limit on the OpenRouter key itself. That is the
only control that survives a bug in this file; these exist so the service degrades politely
long before the key's limit turns every call into an error mid-game.

In-process counters, deliberately: the deployment is a single container (games live in memory
and /turn-ack must reach the process holding the round - see ADR-0005/ADR-0010), so there is
nowhere else for shared state to live and no second replica to disagree with.
"""
import logging
import os
import time
from collections import deque
from typing import Deque, Dict, Optional

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    """An int from the environment, falling back rather than crashing on a malformed value."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring malformed %s=%r; using %d.", name, raw, default)
        return default


# Simultaneous games. Each one is mostly idle (waiting on sequential LLM calls), so this bounds
# spend and memory more than CPU.
MAX_ACTIVE_GAMES = _int_env("MAX_ACTIVE_GAMES", 20)

# New games per client IP per hour. Enough for a person who restarts a few times; not enough to
# loop game creation from a script.
MAX_GAMES_PER_IP_PER_HOUR = _int_env("MAX_GAMES_PER_IP_PER_HOUR", 6)

# The backstop: LLM calls allowed per rolling day. ~5000 is roughly seven complete games.
# Checked before a ROUND starts rather than before each call, so the cap can never strand a
# game halfway through a round with half its detectives moved.
DAILY_LLM_CALL_BUDGET = _int_env("DAILY_LLM_CALL_BUDGET", 5000)

_DAY_SECONDS = 24 * 60 * 60
_HOUR_SECONDS = 60 * 60

_game_creations: Dict[str, Deque[float]] = {}
_llm_calls: Deque[float] = deque()


def _prune(timestamps: Deque[float], window: float, now: float) -> None:
    while timestamps and now - timestamps[0] > window:
        timestamps.popleft()


def client_ip(request) -> str:
    """
    The caller's address, honouring X-Forwarded-For because the deployment sits behind a proxy.

    Takes the FIRST entry, which is the original client; everything after it is the chain of
    proxies. This is spoofable by a client that sets the header itself, so it is a speed bump
    against casual scripted abuse, not an identity - the budget below is what actually bounds
    the damage.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_new_game(ip: str, active_games: int, now: Optional[float] = None) -> Optional[str]:
    """
    Whether a new game may start. Returns None to allow, or a client-facing refusal message.

    Does not record the attempt - call `record_new_game` once the game actually exists, so a
    refused request never counts against the caller.
    """
    now = time.monotonic() if now is None else now
    if active_games >= MAX_ACTIVE_GAMES:
        return ("Too many games are running right now. Please try again in a few minutes.")

    seen = _game_creations.get(ip)
    if seen is not None:
        _prune(seen, _HOUR_SECONDS, now)
        if len(seen) >= MAX_GAMES_PER_IP_PER_HOUR:
            return ("You have started a lot of games recently. Please wait a little while "
                    "before starting another.")
    return None


def record_new_game(ip: str, now: Optional[float] = None) -> None:
    now = time.monotonic() if now is None else now
    seen = _game_creations.setdefault(ip, deque())
    _prune(seen, _HOUR_SECONDS, now)
    seen.append(now)


def llm_calls_today(now: Optional[float] = None) -> int:
    now = time.monotonic() if now is None else now
    _prune(_llm_calls, _DAY_SECONDS, now)
    return len(_llm_calls)


def check_round_budget(now: Optional[float] = None) -> Optional[str]:
    """
    Whether there is budget to start another round. Returns None to allow, or a refusal message.
    """
    if llm_calls_today(now) >= DAILY_LLM_CALL_BUDGET:
        return ("The detectives have hit today's thinking limit. Please come back tomorrow - "
                "this keeps a free, open game from running up a bill.")
    return None


def record_llm_call(now: Optional[float] = None) -> None:
    """Counts one call against the daily budget. Called for every attempt, failed ones included."""
    now = time.monotonic() if now is None else now
    _prune(_llm_calls, _DAY_SECONDS, now)
    _llm_calls.append(now)


def reset_for_tests() -> None:
    _game_creations.clear()
    _llm_calls.clear()
