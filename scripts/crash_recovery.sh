#!/usr/bin/env bash
# Demonstrate controller restart against a simulator that remains alive.
set -euo pipefail
SIM_URL="${1:?usage: scripts/crash_recovery.sh <SIM_URL> [database] [log]}"
DB_PATH="${2:-runs/crash-recovery.sqlite3}"
LOG_PATH="${3:-reports/crash_recovery.log}"
mkdir -p "$(dirname "$DB_PATH")" "$(dirname "$LOG_PATH")"

CONTROLLER_DB="$DB_PATH" ./run.sh "$SIM_URL" >"$LOG_PATH" 2>&1 &
controller_pid=$!
sleep 1
kill -9 "$controller_pid" 2>/dev/null || true
wait "$controller_pid" 2>/dev/null || true
echo "killed controller pid=$controller_pid after 1s" >>"$LOG_PATH"
if ! curl -fsS "$SIM_URL/sim/state" | python3 -c 'import json,sys; s=json.load(sys.stdin); print("simulator finished at kill:", s.get("finished")); sys.exit(1 if s.get("finished") else 0)' >>"$LOG_PATH"; then
  echo "simulator had already finished; kill was not mid-run" | tee -a "$LOG_PATH" >&2
  exit 1
fi

CONTROLLER_DB="$DB_PATH" ./run.sh "$SIM_URL" >>"$LOG_PATH" 2>&1
echo "restart completed" >>"$LOG_PATH"
python3 - "$DB_PATH" <<'PY' >>"$LOG_PATH"
import collections, json, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
rows = db.execute("SELECT action,evidence FROM decisions WHERE action IN ('change_applied','apply_reconciled')").fetchall()
approval_ids = [json.loads(evidence).get("approval_id") for _, evidence in rows]
approval_ids = [aid for aid in approval_ids if aid]
duplicates = sum(n - 1 for n in collections.Counter(approval_ids).values() if n > 1)
print(f"apply records={len(rows)} unique approval IDs={len(set(approval_ids))} duplicate applies={duplicates}")
PY
