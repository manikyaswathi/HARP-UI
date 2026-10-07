#!/usr/bin/env bash
# Run the HARP server on your own laptop (macOS or Linux), for you only.
#
#   ./harp_server/scripts/run_local.sh          then open http://localhost:8000
#
# It listens on 127.0.0.1 only, so nobody else on the network can reach it, and
# logins are accepted over plain http because they come from this machine.
# First run creates harp_server/.venv and installs fastapi, uvicorn and tapipy, plus
# pandas, scikit-learn and TensorFlow for building estimators (HARP_NO_BUILD=1 skips those).
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
# The build phase (pandas, scikit-learn, TensorFlow) is large; skip it with HARP_NO_BUILD=1.
if [ "${HARP_NO_BUILD:-0}" != "1" ] && ! .venv/bin/python -c "import tensorflow, sklearn, pandas" 2>/dev/null; then
  echo "Installing the build phase's packages (pandas, scikit-learn, TensorFlow); this takes a few minutes once"
  .venv/bin/pip install --quiet -r requirements-build.txt
fi

echo "HARP server on http://localhost:$PORT  (Ctrl+C to stop; sweeps resume when you start it again)"
exec .venv/bin/python -m uvicorn harp_server.app:app --host 127.0.0.1 --port "$PORT"
