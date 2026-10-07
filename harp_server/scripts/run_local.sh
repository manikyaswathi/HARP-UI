#!/usr/bin/env bash
# Run the HARP server on your own laptop (macOS or Linux), for you only.
#
#   ./harp_server/scripts/run_local.sh          then open http://localhost:8000
#
# It listens on 127.0.0.1 only, so nobody else on the network can reach it, and
# logins are accepted over plain http because they come from this machine.
# First run creates harp_server/.venv and installs fastapi, uvicorn and tapipy.
set -euo pipefail
cd "$(dirname "$0")/.."            # harp_server/
PY="${PYTHON:-python3}"
PORT="${HARP_PORT:-8000}"

if [ ! -x .venv/bin/python ]; then
  echo "Creating .venv with $($PY --version)"
  "$PY" -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

echo "HARP server on http://localhost:$PORT  (Ctrl+C to stop; sweeps resume when you start it again)"
exec .venv/bin/python -m uvicorn harp_server.app:app --host 127.0.0.1 --port "$PORT"
