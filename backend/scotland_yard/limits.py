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

# How many distinct clients the per-IP tracker will hold at once.
#
# This is the bound that actually matters, because the key it tracks by is not trustworthy.
# client_ip() reads X-Forwarded-For, which the caller controls, so a script rotating that header
# mints a brand-new bucket on every request. Buckets were previously created and never removed -
# empty deques stayed in the dict forever - which made an unauthenticated endpoint an unbounded
# allocator. Sweeping reclaims them; this caps the worst case between sweeps.
#
# Generous relative to MAX_ACTIVE_GAMES: this counts everyone who STARTED a game in the last
# hour, not everyone currently playing, and refusing to track someone silently weakens the limit
# rather than announcing itself.
MAX_TRACKED_CLIENTS = _int_env("MAX_TRACKED_CLIENTS", 10_000)

# Whether to believe X-Forwarded-For at all.
#
# Defaults to TRUE, and deliberately so: the deployment sits behind Render's proxy, which is what
# sets the header. Ignoring it would collapse every visitor onto the proxy's own address - one
# shared bucket - turning MAX_GAMES_PER_IP_PER_HOUR from a per-person limit into a GLOBAL one,
# and refusing the seventh visitor of the hour. The header is spoofable and the per-IP cap is
# therefore a speed bump rather than an identity, which is exactly what it has always claimed to
# be; MAX_TRACKED_CLIENTS above bounds the damage spoofing can do, and the daily budget plus the
# key's own credit limit are what actually bound spend.
#
# Set TRUST_PROXY_HEADERS=false when the app is exposed directly, with no proxy in front of it -
# there the socket address IS the client and the header is pure attacker input.
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "true").strip().lower() not in {
    "false", "0", "no",
}

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
    The caller's address: X-Forwarded-For's first entry (the original client; everything after
    it is the chain of proxies), falling back to the socket peer.

    Spoofable by any caller that sets the header itself, so this is a speed bump against casual
    scripted abuse and never an identity. See TRUST_PROXY_HEADERS for why it is still trusted by
    default, and MAX_TRACKED_CLIENTS for what bounds the damage when it is abused.
    """
    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            # Cap the length: this string becomes a dict key, and it is attacker-controlled.
            return forwarded.split(",")[0].strip()[:64]
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


def _sweep_game_creations(now: float) -> int:
    """
    Expires every client's timestamps and drops the ones left holding nothing.

    Dropping the empties is the part that matters. `_prune` only ever emptied a deque; the KEY
    stayed in the dict for the life of the process, so the tracker grew by one entry per distinct
    client address seen - forever, on a public endpoint, keyed by a header the caller controls.

    Called on each record_new_game: new-game creation is the only way this map can grow, so it is
    also the only moment it can need sweeping - the same reasoning session.py applies to its own
    TTL eviction, and with the same caveat that the sweep must not sit downstream of a check that
    can return first (ISSUE-039).

    Returns how many clients were dropped (for logging/tests).
    """
    stale = []
    for ip, seen in _game_creations.items():
        _prune(seen, _HOUR_SECONDS, now)
        if not seen:
            stale.append(ip)
    for ip in stale:
        del _game_creations[ip]
    return len(stale)


def record_new_game(ip: str, now: Optional[float] = None) -> None:
    now = time.monotonic() if now is None else now
    _sweep_game_creations(now)

    seen = _game_creations.get(ip)
    if seen is None:
        if len(_game_creations) >= MAX_TRACKED_CLIENTS:
            # Deliberately fails OPEN for the per-IP limit rather than refusing the game. This
            # cap can only be reached by address churn, which means spoofing, which means the
            # per-IP limit was already being bypassed - refusing here would punish real players
            # for an attacker's traffic while doing nothing to the attacker. MAX_ACTIVE_GAMES
            # and the daily budget still apply, and they are what bounds the actual spend.
            logger.warning(
                "Per-client game tracking is at its %d-client cap; not tracking %s. The global "
                "caps still apply.", MAX_TRACKED_CLIENTS, ip)
            return
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
