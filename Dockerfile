# The whole app - SPA and API - as one image, for a Hugging Face Docker Space.
#
# ONE container, never scaled past one replica. Games live in process memory (ADR-0005) and a
# round's /turn-ack must reach the very coroutine awaiting it (ADR-0010), so a second replica
# would strand every ack on the wrong process and stall every turn until its 3s timeout.
#
# The build context is the REPO ROOT, not backend/: game_master.py resolves BASE_DIR three
# parents up from itself and reads data/board/map.json at import, so the image has to mirror the
# repository's own layout for that path to resolve.

# --- Stage 1: build the SPA ------------------------------------------------------------------
FROM node:22-alpine AS web
WORKDIR /build

# Dependencies first, so a source-only edit does not reinstall node_modules.
COPY frontend/package.json frontend/package-lock.json frontend/
RUN cd frontend && npm ci

COPY frontend/ frontend/
# scripts/sync-assets.mjs copies the board/pawn/ticket/how-to art out of data/ at prebuild time,
# so data/ has to exist in THIS stage too, not just the runtime one.
COPY data/ data/

# Empty base URL = same origin. The SPA is served by the API itself below, which is what lets the
# ownership cookie ride along on the EventSource connection (EventSource cannot set headers, so a
# cookie is the only practical way to authenticate the round stream).
ENV VITE_API_BASE_URL=""
RUN cd frontend && npm run build

# --- Stage 2: runtime ------------------------------------------------------------------------
FROM python:3.12-slim

# Spaces run as uid 1000; installing and running as that user keeps file ownership sane.
RUN useradd --create-home --uid 1000 user
USER user
ENV PATH="/home/user/.local/bin:${PATH}"
WORKDIR /home/user/app

COPY --chown=user backend/ backend/
COPY --chown=user data/ data/
# Only game_master's MCP read_rules tool reads this, but it is a few KB and its absence would
# turn a working tool into a runtime error for anyone who wires the MCP server up.
COPY --chown=user docs/rules/ docs/rules/
COPY --from=web --chown=user /build/frontend/dist frontend/dist

# Install the DEPENDENCIES from pyproject (the single source of the exact pins), then drop the
# package itself. We deliberately run from the source tree rather than an installed copy:
# game_master.py derives BASE_DIR three parents up from its own __file__, so an installed
# scotland_yard under site-packages would look for data/ next to site-packages and find nothing.
# Leaving an installed copy in place would also shadow-trap anyone who ran from another CWD.
RUN pip install --no-cache-dir --user ./backend \
 && pip uninstall --yes scotland-yard-backend

# `python -m` puts the CWD first on sys.path, so this working directory is what makes
# `scotland_yard` importable AND keeps BASE_DIR resolving to /home/user/app.
WORKDIR /home/user/app/backend

# 7860 is the port a Docker Space expects unless its README front-matter says otherwise.
ENV HOST=0.0.0.0 \
    PORT=7860 \
    PYTHONUNBUFFERED=1
EXPOSE 7860

CMD ["python", "-m", "scotland_yard.server"]
