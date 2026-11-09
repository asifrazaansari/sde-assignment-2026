#!/usr/bin/env bash
# Requires Python 3.11+ and the packages in requirements.txt. Runs locally, no Docker required.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
exec python3 "$(dirname "$0")/controller.py" --url "$SIM_URL"
