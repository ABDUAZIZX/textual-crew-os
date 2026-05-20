# Platform-only image. Connects to an Ollama running on the host
# (systemd service). It does NOT bundle Ollama or the GPU runtime.
#
# Build:  docker build -t textual-crew-os .
# Run:    see docker-compose.yml (uses host networking for loopback access)

FROM python:3.11-slim AS base

# bubblewrap is needed for the Coder agent's sandboxed execution.
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Run as a non-root user (least privilege).
RUN useradd --create-home --uid 10001 crew
WORKDIR /app

# Install dependencies first for layer caching.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Drop privileges.
USER crew
ENV CREW_DATA_DIR=/data \
    CREW_OLLAMA_HOST=http://127.0.0.1:11434 \
    CREW_WEB_HOST=127.0.0.1 \
    CREW_WEB_PORT=8765

VOLUME ["/data"]
EXPOSE 8765

# Default: serve the dashboard. Override with e.g. `crew run "..."`.
ENTRYPOINT ["crew"]
CMD ["dashboard", "--no-browser"]
