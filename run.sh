#!/usr/bin/env bash
# Runner script for WSJT-X QSO Analyzer

set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

MODE="${1:-local}"

if [ "$MODE" = "podman" ]; then
    echo "=== Building and running via Podman ==="
    podman build -t wsjtx-qso-analyzer .
    podman rm -f wsjtx-qso-analyzer 2>/dev/null || true
    podman run -d --name wsjtx-qso-analyzer --network host \
        -v "$DIR/patterns:/app/patterns:ro" \
        -v "$HOME/.local/share/WSJT-X:/wsjtx-data:ro" \
        wsjtx-qso-analyzer
    echo "Container running! Open http://localhost:8080"
    echo "View logs with: podman logs -f wsjtx-qso-analyzer"

elif [ "$MODE" = "docker" ]; then
    echo "=== Building and running via Docker ==="
    docker build -t wsjtx-qso-analyzer .
    docker rm -f wsjtx-qso-analyzer 2>/dev/null || true
    docker run -d --name wsjtx-qso-analyzer --network host \
        -v "$DIR/patterns:/app/patterns:ro" \
        -v "$HOME/.local/share/WSJT-X:/wsjtx-data:ro" \
        wsjtx-qso-analyzer
    echo "Container running! Open http://localhost:8080"
    echo "View logs with: docker logs -f wsjtx-qso-analyzer"

else
    echo "=== Running locally with Python ==="
    echo "Open your browser at: http://localhost:8080"
    python3 app.py
fi
