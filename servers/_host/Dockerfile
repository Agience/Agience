# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Disable Python output buffering so logs appear immediately in Docker
ENV PYTHONUNBUFFERED=1

# ── System deps ──────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        git \
        ca-certificates \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# ── GitHub CLI (for nexus Copilot tools) ─────────────────────────────────────
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ───────────────────────────────────────────────────────
WORKDIR /app
COPY servers/_host/requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# ── Shared auth module ────────────────────────────────────────────────────────
COPY servers/_shared/agience_server_auth.py agience_server_auth.py
COPY servers/_shared/artifact_helpers.py artifact_helpers.py

# ── Server modules ────────────────────────────────────────────────────────────
COPY servers/aria/server.py            aria_server.py
COPY servers/sage/server.py           sage_server.py
COPY servers/sage/azure_search.py     azure_search.py
COPY servers/atlas/server.py           atlas_server.py
COPY servers/nexus/server.py           nexus_server.py
COPY servers/ophan/server.py           ophan_server.py
COPY servers/seraph/server.py          seraph_server.py
COPY servers/verso/server.py           verso_server.py
COPY servers/astra/server.py           astra_server.py
COPY servers/astra/stream_routes.py    stream_routes.py

# ── UI resource views (served via ui:// MCP resources) ────────────────────────
# Each server's resource handler reads views via:
#   pathlib.Path(__file__).parent / "ui" / "application" / "<type>" / "view.html"
# In Docker flat layout, __file__ = /app/<name>_server.py, so we need
# /app/ui/application/ to hold ALL type folders from all servers.
RUN mkdir -p ui/application
COPY servers/aria/ui/application/      ui/application/
COPY servers/astra/ui/application/     ui/application/
COPY servers/sage/ui/application/     ui/application/
COPY servers/nexus/ui/application/     ui/application/
COPY servers/ophan/ui/application/     ui/application/
COPY servers/seraph/ui/application/    ui/application/
COPY servers/verso/ui/application/     ui/application/

# ── Host entrypoint ───────────────────────────────────────────────────────────
COPY servers/_host/server.py server.py

# ── Optional: GitHub Copilot CLI extension for nexus ─────────────────────────
# Set GH_TOKEN as a build arg to enable nexus Copilot tools.
ARG GH_TOKEN
RUN if [ -n "$GH_TOKEN" ]; then \
        gh extension install github/gh-copilot --force \
        && ln -s "$(find /root/.local/share/gh/extensions -name 'gh-copilot' -type f 2>/dev/null | head -1)" \
                 /usr/local/bin/copilot 2>/dev/null || true; \
    else \
        echo "GH_TOKEN not set — skipping GitHub Copilot extension install"; \
    fi

# ── Runtime config ────────────────────────────────────────────────────────────
ENV MCP_HOST=0.0.0.0 \
    MCP_PORT=8082 \
    LOG_LEVEL=INFO \
    COPILOT_CWD=/workspace

EXPOSE 8082

# Mount host project directory here for nexus/Copilot to operate on
VOLUME ["/workspace"]

CMD ["python", "server.py"]
