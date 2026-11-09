#!/usr/bin/env bash
# Start a fresh public_01 simulator, kill the controller at approval lifecycle
# checkpoints, restart it with the same SQLite database, then verify completion.
set -euo pipefail

LOG_PATH="${1:-reports/crash_recovery.log}"
mkdir -p "$(dirname "$LOG_PATH")" runs
TMP_DIR="$(mktemp -d runs/crash_recovery.XXXXXX)"
DB_PATH="$TMP_DIR/controller.sqlite3"
SIM_RUN_DIR="$TMP_DIR/simulator"
PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
SIM_URL="http://127.0.0.1:$PORT"
SIM_PID=""
CONTROLLER_PID=""

cleanup() {
  if [[ -n "$CONTROLLER_PID" ]] && kill -0 "$CONTROLLER_PID" 2>/dev/null; then
    kill "$CONTROLLER_PID" 2>/dev/null || true
    wait "$CONTROLLER_PID" 2>/dev/null || true
  fi
  if [[ -n "$SIM_PID" ]] && kill -0 "$SIM_PID" 2>/dev/null; then
    kill "$SIM_PID" 2>/dev/null || true
    wait "$SIM_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

: > "$LOG_PATH"
echo "Starting public_01 simulator at $SIM_URL; run_dir=$SIM_RUN_DIR" >> "$LOG_PATH"
python3 -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir "$SIM_RUN_DIR" --port "$PORT" >"$TMP_DIR/simulator.log" 2>&1 &
SIM_PID=$!
for _ in $(seq 1 300); do
  if curl -fsS "$SIM_URL/health" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$SIM_PID" 2>/dev/null; then
    cat "$TMP_DIR/simulator.log" >> "$LOG_PATH"
    echo "FAIL: simulator exited before becoming healthy" >> "$LOG_PATH"
    exit 1
  fi
  sleep 0.1
done
if ! curl -fsS "$SIM_URL/health" >/dev/null 2>&1; then
  echo "FAIL: simulator did not become healthy" >> "$LOG_PATH"
  exit 1
fi

start_controller() {
  CONTROLLER_DB="$DB_PATH" ./run.sh "$SIM_URL" >> "$LOG_PATH" 2>&1 &
  CONTROLLER_PID=$!
}

db_snapshot() {
  python3 - "$DB_PATH" "$1" <<'PY' >> "$LOG_PATH"
import json, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
rows = db.execute("SELECT status,count(*) FROM proposals GROUP BY status ORDER BY status").fetchall()
cursor = db.execute("SELECT value FROM state WHERE key='chunk_cursor'").fetchone()
print(f"{sys.argv[2]}: cursor={json.loads(cursor[0]) if cursor else None}, proposals={dict(rows)}")
PY
}

wait_for_db_status() {
  wanted="$1"
  for _ in $(seq 1 600); do
    [[ -f "$DB_PATH" ]] && found="$(python3 - "$DB_PATH" "$wanted" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
try:
    row = db.execute("SELECT proposal_id,approval_id FROM proposals WHERE status=? AND approval_id IS NOT NULL ORDER BY created_at LIMIT 1", (sys.argv[2],)).fetchone()
    print("\t".join(str(v or "") for v in row) if row else "")
except sqlite3.OperationalError:
    print("")
PY
)" || found=""
    if [[ -n "${found:-}" ]]; then printf '%s' "$found"; return 0; fi
    if [[ -n "$CONTROLLER_PID" ]] && ! kill -0 "$CONTROLLER_PID" 2>/dev/null; then return 1; fi
    sleep 0.05
  done
  return 1
}

start_controller
if ! first="$(wait_for_db_status pending)"; then
  echo "FAIL: no pending approval proposal was created" >> "$LOG_PATH"
  exit 1
fi
IFS=$'\t' read -r first_pid first_approval <<< "$first"
db_snapshot "kill point 1 before SIGKILL (pending proposal=$first_pid approval=$first_approval)"
kill -9 "$CONTROLLER_PID"
wait "$CONTROLLER_PID" 2>/dev/null || true
CONTROLLER_PID=""
echo "kill point 1: controller killed while approval was pending" >> "$LOG_PATH"

cursor_before_restart="$(python3 - "$DB_PATH" <<'PY'
import json, sqlite3, sys
db=sqlite3.connect(sys.argv[1]); row=db.execute("SELECT value FROM state WHERE key='chunk_cursor'").fetchone()
print(json.loads(row[0]) if row else -1)
PY
)"
start_controller
cursor_at_restart="$(python3 - "$DB_PATH" <<'PY'
import json, sqlite3, sys
db=sqlite3.connect(sys.argv[1]); row=db.execute("SELECT value FROM state WHERE key='chunk_cursor'").fetchone()
print(json.loads(row[0]) if row else -1)
PY
)"
if (( cursor_at_restart < cursor_before_restart )); then
  echo "FAIL: cursor moved backward at restart: $cursor_before_restart -> $cursor_at_restart" >> "$LOG_PATH"
  exit 1
fi
echo "cursor at restart: $cursor_before_restart -> $cursor_at_restart" >> "$LOG_PATH"

# The simulator changes pending approvals to approved during advance. Catch the
# small interval before the controller applies the approval, if public_01 allows it.
second_reached=0
for _ in $(seq 1 1200); do
  if [[ -n "$CONTROLLER_PID" ]] && ! kill -0 "$CONTROLLER_PID" 2>/dev/null; then break; fi
  candidate="$(python3 - "$DB_PATH" <<'PY'
import sqlite3, sys
db=sqlite3.connect(sys.argv[1])
try:
 row=db.execute("SELECT approval_id,workflow_id,base_version FROM proposals WHERE status='pending' AND approval_id IS NOT NULL ORDER BY created_at LIMIT 1").fetchone()
 print("\t".join(str(x) for x in row) if row else "")
except sqlite3.OperationalError:
 print("")
PY
)"
  if [[ -n "$candidate" ]]; then
    IFS=$'\t' read -r aid wid base <<< "$candidate"
    approval_status="$(curl -fsS "$SIM_URL/approvals/$aid" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))' 2>/dev/null || true)"
    if [[ "$approval_status" == "approved" ]]; then
      config_version="$(curl -fsS "$SIM_URL/workflows/$wid/config" | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
      if (( config_version <= base )); then
        db_snapshot "kill point 2 before SIGKILL (approved, unapplied approval=$aid)"
        kill -9 "$CONTROLLER_PID"
        wait "$CONTROLLER_PID" 2>/dev/null || true
        CONTROLLER_PID=""
        echo "kill point 2: controller killed after approval $aid was approved but before apply" >> "$LOG_PATH"
        second_reached=1
        break
      fi
    fi
  fi
  sleep 0.01
done

if (( second_reached )); then
  cursor_after_kill="$(python3 - "$DB_PATH" <<'PY'
import json, sqlite3, sys
db=sqlite3.connect(sys.argv[1]); row=db.execute("SELECT value FROM state WHERE key='chunk_cursor'").fetchone()
print(json.loads(row[0]) if row else -1)
PY
)"
  if (( cursor_after_kill < cursor_before_restart )); then
    echo "FAIL: cursor moved backward across restart: $cursor_before_restart -> $cursor_after_kill" >> "$LOG_PATH"
    exit 1
  fi
  echo "cursor monotonic check: $cursor_before_restart -> $cursor_after_kill" >> "$LOG_PATH"
  start_controller
else
  echo "kill point 2 unreachable on public_01: no approved-but-unapplied approval was observed before the controller finished" >> "$LOG_PATH"
fi

if [[ -n "$CONTROLLER_PID" ]]; then
  set +e
  wait "$CONTROLLER_PID"
  controller_status=$?
  set -e
  CONTROLLER_PID=""
else
  controller_status=0
fi
if (( controller_status != 0 )); then
  echo "FAIL: final controller process exited with status $controller_status" >> "$LOG_PATH"
  exit 1
fi

final_state="$(curl -fsS "$SIM_URL/sim/state")"
python3 - "$DB_PATH" "$final_state" "$cursor_before_restart" <<'PY' >> "$LOG_PATH"
import collections, json, sqlite3, sys
db=sqlite3.connect(sys.argv[1]); state=json.loads(sys.argv[2]); cursor_before=int(sys.argv[3])
cursor_row=db.execute("SELECT value FROM state WHERE key='chunk_cursor'").fetchone()
cursor=json.loads(cursor_row[0]) if cursor_row else -1
if cursor < cursor_before: raise SystemExit(f"FAIL: cursor moved backward: {cursor_before} -> {cursor}")
rows=db.execute("SELECT status,count(*) FROM proposals GROUP BY status").fetchall()
counts=dict(rows); print(f"final cursor={cursor}; proposal states={counts}")
nonterminal=set(counts)-{"applied","rejected","expired","lapsed","abandoned_conflict","superseded","stale","error"}
if nonterminal: raise SystemExit(f"FAIL: nonterminal proposals remain: {sorted(nonterminal)}")
applies=db.execute("SELECT evidence FROM decisions WHERE action IN ('change_applied','apply_reconciled')").fetchall()
ids=[json.loads(e).get("approval_id") for (e,) in applies]
ids=[x for x in ids if x]
duplicates=sum(n-1 for n in collections.Counter(ids).values() if n>1)
print(f"apply records={len(ids)} unique approval IDs={len(set(ids))} duplicate applies={duplicates}")
if duplicates: raise SystemExit("FAIL: duplicate applies recorded for an approval ID")
if not state.get("finished"): raise SystemExit("FAIL: simulator did not finish")
print(f"final simulator state: finished={state.get('finished')} sim_minute={state.get('sim_minute')}")
PY
echo "PASS: crash recovery assertions completed" >> "$LOG_PATH"
