"""
Logging setup for the backend.

The application previously narrated itself entirely through `print()`. That made the one
signal worth monitoring - the `[VALIDATION]` lines, which fire whenever deterministic
enforcement had to override an LLM's output - unfilterable, unroutable, and indistinguishable
from ordinary progress chatter. They now log at WARNING, so `LOG_LEVEL=WARNING` surfaces
exactly the "an agent produced something illegal" events and nothing else.

Concurrent games are separated by a game-id field injected via a contextvar rather than
threaded through every call signature: the detective loop is deeply nested async code, and
passing a logger or an id down through LangGraph nodes would mean changing every node's
interface for a logging concern.
"""
import contextvars
import logging
import os
import sys

# Set for the duration of one request/round by `game_log_context`; rendered into every log
# line emitted while it is set. Defaults to "-" so module-level and startup logs still format.
current_game_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_game_id", default="-")


class GameIdFilter(logging.Filter):
    """Injects the current game id into every record so the formatter can render it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.game_id = current_game_id.get()
        return True


class game_log_context:
    """
    Context manager binding a game id to every log line emitted inside it.

    Used by the routes so a line from the middle of a debate loop can be attributed to the
    game that produced it, which is otherwise impossible once two games run concurrently.
    """

    def __init__(self, game_id: str) -> None:
        self.game_id = game_id
        self._token = None

    def __enter__(self) -> "game_log_context":
        self._token = current_game_id.set(self.game_id)
        return self

    def __exit__(self, *exc_info) -> None:
        if self._token is not None:
            current_game_id.reset(self._token)


def configure_logging(level: str | None = None) -> None:
    """
    Configures root logging once, at application startup.

    Output goes to **stderr**, never stdout. game_master.py can be run as an MCP server whose
    stdout carries the JSON-RPC stream, and keeping every logger in this package on stderr
    means importing any module can never corrupt that stream.

    Level comes from the LOG_LEVEL environment variable (default INFO).
    """
    resolved = (level or os.getenv("LOG_LEVEL") or "INFO").upper()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s [%(game_id)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    handler.addFilter(GameIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(resolved)

    # These are extremely chatty at DEBUG and drown out this application's own output.
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(max(logging.INFO, root.level))
