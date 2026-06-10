#!/usr/bin/env bash
# Launch Options Sensi.
#
# The FastAPI backend serves the dashboard too, so one process covers both
# front end and back end. Picks the first free port from $BASE_PORT (default
# 8000) upward so it never collides with anything else you have running.
#
# Usage:
#   ./run.sh                 # auto-pick a port, open the dashboard
#   PORT=9000 ./run.sh       # force a specific port
#   NO_OPEN=1 ./run.sh       # don't auto-open the browser
#   ./run.sh --reload        # extra args are passed through to uvicorn
set -euo pipefail
cd "$(dirname "$0")"

if [ -z "${PORT:-}" ]; then
  PORT=$(python3 - "${BASE_PORT:-8000}" <<'EOF'
import socket, sys
base = int(sys.argv[1])
for port in range(base, base + 200):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            continue
        print(port)
        break
else:  # everything in range taken; let the OS assign one
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        print(s.getsockname()[1])
EOF
)
fi

echo "Options Sensi → http://localhost:${PORT}"

if [ -z "${NO_OPEN:-}" ] && command -v open >/dev/null; then
  (sleep 1.5 && open "http://localhost:${PORT}") &
fi

# uv run syncs the environment from pyproject.toml/uv.lock before starting
exec uv run uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" "$@"
