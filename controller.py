#!/usr/bin/env python3
"""Small, restartable HTTP controller for the Formi simulator."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

MIN_ARM = 500
ALPHA = 0.05
GUARDRAIL_DROP = 0.02
# 1.645 is the one-sided 95% normal quantile for a non-inferiority lower bound.
GUARDRAIL_Z = 1.645
CALLER_ID_MIN_DIALS = 300
CALLER_ID_WINDOW = 500
CALLER_ID_MIN_GAP = 0.10
CALLER_ID_EXPLORATION_SHARE = 0.10


def two_proportion_pvalue(x1: int, n1: int, x2: int, n2: int) -> float:
    """Two-sided pooled z-test; returns 1 when there is not enough information."""
    if min(n1, n2) < MIN_ARM or min(x1, x2) < 0 or x1 > n1 or x2 > n2:
        return 1.0
    p = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = abs(x1 / n1 - x2 / n2) / se
    # Normal-tail approximation, adequate for the fixed, conservative threshold.
    return math.erfc(z / math.sqrt(2))


def one_sided_two_proportion_pvalue(x_lower: int, n_lower: int,
                                    x_higher: int, n_higher: int) -> float:
    """P-value for the directional alternative that the second rate is higher."""
    if min(n_lower, n_higher) < CALLER_ID_MIN_DIALS:
        return 1.0
    if min(x_lower, x_higher) < 0 or x_lower > n_lower or x_higher > n_higher:
        return 1.0
    if x_higher / n_higher <= x_lower / n_lower:
        return 1.0
    pooled = (x_lower + x_higher) / (n_lower + n_higher)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n_lower + 1 / n_higher))
    if se == 0:
        return 1.0
    z = (x_higher / n_higher - x_lower / n_lower) / se
    return 0.5 * math.erfc(z / math.sqrt(2))


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
              event_id INTEGER PRIMARY KEY, chunk INTEGER NOT NULL, workflow_id TEXT,
              type TEXT NOT NULL, ts TEXT, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_workflow ON events(workflow_id,type);
            CREATE TABLE IF NOT EXISTS decisions (
              id INTEGER PRIMARY KEY, ts TEXT NOT NULL, workflow_id TEXT, action TEXT NOT NULL,
              why TEXT NOT NULL, evidence TEXT NOT NULL, approver TEXT, outcome TEXT);
            CREATE TABLE IF NOT EXISTS proposals (
              proposal_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, base_version INTEGER NOT NULL,
              body TEXT NOT NULL, approval_id TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_workflow_event ON events(workflow_id,type,event_id DESC);
        """)
        self.db.commit()
        self._caller_ids_cache = {}

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value, sort_keys=True)))
        self.db.commit()

    def audit(self, workflow, action, why, evidence, ts="unknown", approver=None, outcome=None):
        self.db.execute("INSERT INTO decisions(ts,workflow_id,action,why,evidence,approver,outcome) VALUES(?,?,?,?,?,?,?)",
                        (ts, workflow, action, why, json.dumps(evidence, sort_keys=True), approver, outcome))
        self.db.commit()

    def add_chunk(self, chunk, lines):
        # Event rows and the durable cursor move atomically, so a restart replays safely.
        inserted = 0
        with self.db:
            for raw in lines:
                if not raw.strip():
                    continue
                event = json.loads(raw)
                eid = event.get("event_id")
                if eid is None:
                    continue
                wid = event.get("workflow_id")
                caller_id = event.get("variant", {}).get("caller_id")
                if wid in self._caller_ids_cache and event.get("type") == "call" and caller_id is not None:
                    self._caller_ids_cache[wid].add(str(caller_id))
                cur = self.db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?)",
                                      (eid, chunk, wid, event["type"], event.get("ts"),
                                       json.dumps(event, sort_keys=True)))
                inserted += cur.rowcount
            old = self.get("chunk_cursor", -1)
            self.db.execute("INSERT INTO state VALUES('chunk_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (json.dumps(max(old, chunk)),))
        return inserted

    def event_rows(self, workflow, kind="call"):
        return self.db.execute("SELECT body FROM events WHERE workflow_id=? AND type=? ORDER BY event_id",
                               (workflow, kind)).fetchall()

    def caller_ids(self, workflow):
        if workflow not in self._caller_ids_cache:
            self._caller_ids_cache[workflow] = {str(row[0]) for row in self.db.execute(
                "SELECT DISTINCT json_extract(body,'$.variant.caller_id') FROM events "
                "WHERE workflow_id=? AND type='call' AND json_extract(body,'$.variant.caller_id') IS NOT NULL",
                (workflow,)).fetchall()}
        return sorted(self._caller_ids_cache[workflow])

    def caller_id_windows(self, workflow, caller_id, window_size=CALLER_ID_WINDOW):
        rows = self.db.execute(
            "SELECT body FROM events WHERE workflow_id=? AND type='call' "
            "AND json_extract(body,'$.variant.caller_id')=? ORDER BY event_id DESC LIMIT ?",
            (workflow, caller_id, window_size * 2)).fetchall()
        recent = [json.loads(row[0]) for row in rows]
        recent.reverse()
        split = max(0, len(recent) - window_size)
        return recent[split:], recent[max(0, split - window_size):split]

    def last_event_id(self, workflow):
        row = self.db.execute("SELECT max(event_id) FROM events WHERE workflow_id=?", (workflow,)).fetchone()
        return row[0] if row and row[0] is not None else -1

    def version_pickup_since(self, workflow, version, caller_id, after_event_id):
        if caller_id is None:
            row = self.db.execute(
                "SELECT count(*),coalesce(sum(CASE WHEN json_extract(body,'$.connected') THEN 1 ELSE 0 END),0),"
                "coalesce(max(event_id),?) FROM events WHERE workflow_id=? AND type='call' AND event_id>? "
                "AND json_extract(body,'$.config_version')=?",
                (after_event_id, workflow, after_event_id, version)).fetchone()
        else:
            row = self.db.execute(
                "SELECT count(*),coalesce(sum(CASE WHEN json_extract(body,'$.connected') THEN 1 ELSE 0 END),0),"
                "coalesce(max(event_id),?) FROM events WHERE workflow_id=? AND type='call' AND event_id>? "
                "AND json_extract(body,'$.config_version')=? "
                "AND json_extract(body,'$.variant.caller_id')=?",
                (after_event_id, workflow, after_event_id, version, caller_id)).fetchone()
        return {"dials": row[0], "connected": row[1], "last_event_id": row[2]}

    def proposal_rows(self, status=None):
        if status:
            return self.db.execute("SELECT * FROM proposals WHERE status=?", (status,)).fetchall()
        return self.db.execute("SELECT * FROM proposals ORDER BY created_at").fetchall()


class Controller:
    def __init__(self, url: str, db_path: str):
        self.api = httpx.Client(base_url=url.rstrip("/"), timeout=60)
        self.store = Store(db_path)
        self.last_ts = "unknown"
        self.credits_seen = None
        self.started_wall = time.perf_counter()
        self.ingest_wall = 0.0
        self.ingested_events = 0
        self.max_event_lag_minutes = 0.0

    def get(self, path, **kwargs):
        delay = 0.25
        while True:
            try:
                response = self.api.get(path, **kwargs)
                if response.status_code >= 500:
                    time.sleep(delay)
                    delay = min(delay * 2, 5)
                    continue
                response.raise_for_status()
                return response.json()
            except (httpx.TimeoutException, httpx.NetworkError):
                time.sleep(delay)
                delay = min(delay * 2, 5)

    def post(self, path, body):
        delay = 0.25
        while True:
            try:
                response = self.api.post(path, json=body)
                # Advance is not idempotent. Its uncertain response is reconciled against /sim/state.
                if path != "/sim/advance" and response.status_code >= 500:
                    time.sleep(delay)
                    delay = min(delay * 2, 5)
                    continue
                return response
            except (httpx.TimeoutException, httpx.NetworkError):
                if path == "/sim/advance":
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 5)

    def bootstrap_cursor(self):
        if self.store.get("chunk_cursor") is not None:
            return
        history = self.get("/events/chunks", params={"kind": "history"})
        last = max((x.get("chunk", -1) for x in history), default=-1)
        self.store.set("chunk_cursor", last)
        self.store.audit(None, "bootstrap", "Started at the first live event chunk; pre-D0 history is intentionally excluded.",
                         {"ignored_history_through_chunk": last}, self.last_ts)

    def ingest(self):
        started = time.perf_counter()
        cursor = self.store.get("chunk_cursor", -1)
        chunks = self.get("/events/chunks", params={"kind": "events", "after": cursor})
        for item in chunks:
            number = item["chunk"]
            response = self.api.get(f"/events/chunks/{number}")
            response.raise_for_status()
            lines = response.text.splitlines()
            for raw in lines:
                if not raw.strip():
                    continue
                event = json.loads(raw)
                if event.get("ts") and self.last_ts != "unknown":
                    try:
                        lag = (datetime.fromisoformat(self.last_ts) - datetime.fromisoformat(event["ts"])).total_seconds() / 60
                        self.max_event_lag_minutes = max(self.max_event_lag_minutes, lag)
                    except ValueError:
                        pass
            self.ingested_events += self.store.add_chunk(number, lines)
        self.ingest_wall += time.perf_counter() - started

    def workflow_list(self):
        result = self.get("/workflows")
        return result if isinstance(result, list) else result.get("workflows", [])

    def current_config(self, workflow):
        return self.get(f"/workflows/{workflow}/config")

    @staticmethod
    def apply_dimensions(base, target, dimensions):
        result = copy.deepcopy(base)
        for dim in dimensions:
            if dim in ("welcome_message", "voice", "caller_id"):
                result.setdefault("allocations", {}).setdefault("default", {})[dim] = copy.deepcopy(
                    target.get("allocations", {}).get("default", {}).get(dim))
            else:
                result[dim] = copy.deepcopy(target.get(dim))
        return result

    def in_approval_window(self, wf):
        try:
            hour = datetime.fromisoformat(self.last_ts).hour
        except ValueError:
            return False
        hours = wf.get("governance", {}).get("approver_hours", [10, 19])
        earliest = max(hours[0], 11 if wf.get("tenant_id") == "kestrel_finance" else hours[0])
        # Submit early enough to leave a full business window for the approver and apply step.
        return earliest <= hour < min(hours[1], earliest + 2)

    def process_config_events(self):
        for row in self.store.db.execute("SELECT body FROM events WHERE type='config_changed' ORDER BY event_id"):
            ev = json.loads(row[0])
            key = f"config_event:{ev.get('event_id')}"
            if self.store.get(key):
                continue
            self.store.set(key, True)
            if ev.get("actor") == "supervisor":
                exp = self.store.get(f"experiment:{ev['workflow_id']}")
                if exp and ev.get("version", 0) > exp.get("base_version", 0):
                    exp["phase"] = "invalidated"
                    self.store.set(f"experiment:{ev['workflow_id']}", exp)
                    self.store.audit(ev["workflow_id"], "respect_supervisor_edit",
                                     "Supervisor changed the workflow while an experiment was active; its evidence is no longer used.",
                                     ev, ev.get("ts", self.last_ts), outcome="experiment_invalidated")

    def create_proposal(self, wf, config_result, proposed, summary, why, evidence):
        current = config_result["config"]
        if current == proposed:
            return None
        version = config_result["version"]
        generation = self.store.db.execute("SELECT count(*) FROM proposals WHERE workflow_id=?",
                                           (wf["workflow_id"],)).fetchone()[0]
        keytext = json.dumps([wf["workflow_id"], version, proposed, generation], sort_keys=True)
        proposal_id = "ctl-" + hashlib.sha256(keytext.encode()).hexdigest()[:24]
        old = self.store.db.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        if old:
            return proposal_id
        changed = []
        for dim in ("cohorts", "prompt_version", "schedule"):
            if current.get(dim) != proposed.get(dim):
                changed.append(dim)
        for dim in ("welcome_message", "voice", "caller_id"):
            before = current.get("allocations", {}).get("default", {}).get(dim)
            after = proposed.get("allocations", {}).get("default", {}).get(dim)
            if before != after:
                changed.append(dim)
        body = {"proposal_id": proposal_id, "workflow_id": wf["workflow_id"], "base_version": version,
                "proposed_config": proposed, "change_summary": summary,
                "diff": {k: {"from": (current.get("allocations", {}).get("default", {}).get(k)
                                        if k in ("welcome_message", "voice", "caller_id") else current.get(k)),
                             "to": (proposed.get("allocations", {}).get("default", {}).get(k)
                                    if k in ("welcome_message", "voice", "caller_id") else proposed.get(k))}
                         for k in changed},
                "affected_leads": int(wf.get("daily_batch", {}).get("size", 0)),
                "dry_run": {"method": "Observed API events only; population estimate, not a causal replay.",
                            "observed_calls": self.store.db.execute("SELECT count(*) FROM events WHERE workflow_id=? AND type='call'", (wf["workflow_id"],)).fetchone()[0]},
                "impact_score": self.impact_estimate(evidence),
                "rationale": why, "expires_in_hours": min(12, wf.get("governance", {}).get("proposal_ttl_hours", 12))}
        if not self.in_approval_window(wf):
            self.store.db.execute("INSERT INTO proposals(proposal_id,workflow_id,base_version,body,status,created_at) VALUES(?,?,?,?,?,?)",
                                  (proposal_id, wf["workflow_id"], version, json.dumps(body, sort_keys=True), "queued", self.last_ts))
            self.store.db.commit()
            self.store.audit(wf["workflow_id"], "proposal_queued", "Queued for the start of the approver's working window.",
                             {"proposal_id": proposal_id, "changed_dims": changed}, self.last_ts)
            return proposal_id
        self.store.db.execute("INSERT INTO proposals(proposal_id,workflow_id,base_version,body,status,created_at) VALUES(?,?,?,?,?,?)",
                              (proposal_id, wf["workflow_id"], version, json.dumps(body, sort_keys=True), "submitting", self.last_ts))
        self.store.db.commit()
        response = self.post("/approvals", body)
        if response.status_code == 409 and "stale_base_version" in response.text:
            self.store.db.execute("UPDATE proposals SET status='stale' WHERE proposal_id=?", (proposal_id,))
            self.store.db.commit()
            self.store.audit(wf["workflow_id"], "reproposal_needed", "Config base became stale before proposal; refresh and rebase the intended change.",
                             {"proposal_id": proposal_id, "response": response.text}, self.last_ts)
            return self.create_proposal(wf, self.current_config(wf["workflow_id"]), proposed, summary, why, evidence)
        if response.status_code == 409 and "duplicate_proposal_id" in response.text:
            # Reconcile by the durable idempotency key rather than creating a new change.
            approvals = self.get("/approvals", params={"workflow_id": wf["workflow_id"]})
            match = next((a for a in approvals if a.get("proposal_id") == proposal_id), None)
            if match:
                approval_id = match["approval_id"]
            else:
                raise RuntimeError("duplicate proposal ID cannot be reconciled")
        elif response.is_error:
            self.store.db.execute("UPDATE proposals SET status='error' WHERE proposal_id=?", (proposal_id,))
            self.store.db.commit()
            self.store.audit(wf["workflow_id"], "proposal_rejected_by_api", response.text,
                             {"proposal_id": proposal_id}, self.last_ts)
            return None
        else:
            approval_id = response.json()["approval_id"]
        self.store.db.execute("UPDATE proposals SET approval_id=?,status='pending' WHERE proposal_id=?", (approval_id, proposal_id))
        self.store.db.commit()
        self.store.audit(wf["workflow_id"], "proposal_created", why,
                         {"proposal_id": proposal_id, "approval_id": approval_id, "changed_dims": changed,
                          "impact_score": body["impact_score"]}, self.last_ts)
        return proposal_id

    @staticmethod
    def impact_estimate(evidence):
        """Rank from observed arm-rate difference only; an unmeasured test gets zero."""
        arms = evidence.get("arms", {})
        if len(arms) != 2:
            return 0.0
        rates = []
        for arm in arms.values():
            n = arm.get("primary_n", 0)
            rates.append(arm.get("primary_success", 0) / n if n else 0.0)
        confidence = max(0.0, 1.0 - evidence.get("p_value", 1.0))
        return round(min(1.0, abs(rates[0] - rates[1]) * confidence * 10), 3)

    @staticmethod
    def caller_id_state_key(workflow, caller_id):
        return f"caller_id_health:{workflow}:{caller_id}"

    @staticmethod
    def pickup_summary(events):
        n = len(events)
        successes = sum(bool(ev.get("connected")) for ev in events)
        return {"dials": n, "connected": successes, "pickup_rate": successes / n if n else None}

    def replace_caller_id_proposal(self, workflow, old_id, new_id):
        if not new_id:
            return
        prefix = f"caller_id_health:{workflow}:"
        for key, value in self.store.db.execute("SELECT key,value FROM state WHERE key LIKE ?", (prefix + "%",)).fetchall():
            state = json.loads(value)
            if state.get("proposal_id") == old_id:
                state["proposal_id"] = new_id
                self.store.set(key, state)

    def caller_id_family_size(self, workflows):
        return max(1, sum(len(self.store.caller_ids(w["workflow_id"])) for w in workflows))

    @staticmethod
    def total_variation(before, after):
        keys = set(before) | set(after)
        return 0.5 * sum(abs(float(before.get(k, 0)) - float(after.get(k, 0))) for k in keys)

    def _auto_apply_caller_id_allowed(self, wf, before, after):
        policy = wf.get("governance", {}).get("auto_apply", {})
        return (policy.get("enabled") is True and "caller_id" in policy.get("allowed_dims", []) and
                self.total_variation(before, after) <= float(policy.get("max_shift", 0)))

    def _caller_id_supervisor_conflict(self, workflow, base_version):
        for row in self.store.db.execute(
                "SELECT body FROM events WHERE workflow_id=? AND type='config_changed'", (workflow,)):
            ev = json.loads(row[0])
            if (ev.get("actor") == "supervisor" and ev.get("version", 0) > base_version and
                    "caller_id" in ev.get("changed_dims", [])):
                return ev
        return None

    def _mark_caller_id_applied(self, state, workflow, version):
        if state.get("operation") == "shift":
            state.update(status="active", applied_version=version, proposal_id=None,
                         verify_logged=False, verify_after_event_id=self.store.last_event_id(workflow),
                         verify_id_dials=0, verify_id_connected=0,
                         verify_workflow_dials=0, verify_workflow_connected=0)
        else:
            state.update(status="healthy", applied_version=version, proposal_id=None)
            state.pop("original_weights", None)
        self.store.set(self.caller_id_state_key(workflow, state["caller_id"]), state)

    def _caller_id_change(self, wf, caller_id, current, target_weights, operation, state, evidence):
        wid = wf["workflow_id"]
        key = self.caller_id_state_key(wid, caller_id)
        proposed = copy.deepcopy(current["config"])
        proposed.setdefault("allocations", {}).setdefault("default", {})["caller_id"] = target_weights
        if proposed == current["config"]:
            state.update(status="healthy" if operation == "restore" else "blocked", proposal_id=None)
            self.store.set(key, state)
            return None

        summary = (f"Restore caller ID {caller_id} to its prior allocation after pickup recovery." if operation == "restore"
                   else f"Reduce caller ID {caller_id} after a population pickup drop; retain a 10% exploration share.")
        rationale = "Population pickup evidence met the pre-set volume, effect, and corrected significance thresholds."
        version = current["version"]
        identity = json.dumps([wid, caller_id, operation, version, target_weights], sort_keys=True)
        proposal_id = "ctl-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        state.update(status=f"{operation}_pending", operation=operation, proposal_id=proposal_id)
        self.store.set(key, state)

        before_weights = current["config"].get("allocations", {}).get("default", {}).get("caller_id", {})
        if self._auto_apply_caller_id_allowed(wf, before_weights, target_weights):
            body = {"proposal_id": proposal_id, "base_version": version, "proposed_config": proposed,
                    "rationale": rationale, "evidence": evidence}
            response = None
            try:
                response = self.api.put(f"/workflows/{wid}/config", json=body)
            except httpx.HTTPError:
                # Resolve an uncertain PUT by reading the authoritative live config.
                pass
            fresh = self.current_config(wid)
            if fresh["config"] == proposed:
                state["operation"] = operation
                self._mark_caller_id_applied(state, wid, fresh["version"])
                self.store.audit(wid, f"caller_id_{operation}_auto_applied",
                                 "Caller-ID allocation changed under the workflow's permitted auto-apply policy.",
                                 {**evidence, "proposal_id": proposal_id, "version": fresh["version"]}, self.last_ts,
                                 outcome="applied")
                return proposal_id
            if response is not None and response.status_code == 409:
                conflict = self._caller_id_supervisor_conflict(wid, version)
                if conflict:
                    state.update(status="shift_conflict" if operation == "shift" else "active",
                                 proposal_id=None)
                    self.store.set(key, state)
                    self.store.audit(wid, "caller_id_supervisor_conflict",
                                     "A supervisor edited caller ID after the auto-apply base; the controller did not overwrite that edit.",
                                     {**evidence, "supervisor_event": conflict}, self.last_ts, outcome="abandoned")
                    return None
                proposed = self.apply_dimensions(fresh["config"], proposed, {"caller_id"})
                current = fresh
            elif response is not None and response.status_code != 403 and response.status_code < 500:
                state.update(status="shift_blocked" if operation == "shift" else "active", proposal_id=None)
                self.store.set(key, state)
                self.store.audit(wid, "caller_id_auto_apply_rejected", response.text,
                                 {**evidence, "proposal_id": proposal_id, "status_code": response.status_code},
                                 self.last_ts, outcome="not_applied")
                return None
            else:
                self.store.audit(wid, "caller_id_auto_apply_fallback",
                                 "Auto-apply was unavailable or stale; submit through the standard approval path.",
                                 {**evidence, "proposal_id": proposal_id}, self.last_ts)

        new_id = self.create_proposal(wf, current, proposed, summary, rationale, {})
        if new_id:
            state.update(status=f"{operation}_pending", proposal_id=new_id)
        else:
            state.update(status="shift_blocked" if operation == "shift" else "active", proposal_id=None)
        self.store.set(key, state)
        return new_id

    def sync_caller_id_proposals(self, workflows):
        rows = self.store.db.execute("SELECT key,value FROM state WHERE key LIKE 'caller_id_health:%'").fetchall()
        for key, raw in rows:
            state = json.loads(raw)
            proposal_id = state.get("proposal_id")
            if not proposal_id:
                continue
            proposal = self.store.db.execute("SELECT status FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if not proposal:
                continue
            status = proposal["status"]
            if status == "applied":
                wf_id = state["workflow_id"]
                live = self.current_config(wf_id)
                self._mark_caller_id_applied(state, wf_id, live["version"])
            elif status in {"rejected", "expired", "lapsed", "abandoned_conflict", "error", "stale"}:
                state["status"] = "shift_rejected" if state.get("operation") == "shift" else "active_restore_rejected"
                state["proposal_id"] = None
                self.store.set(key, state)

    def verify_caller_id_shifts(self, workflows, family_size):
        by_id = {w["workflow_id"]: w for w in workflows}
        rows = self.store.db.execute("SELECT key,value FROM state WHERE key LIKE 'caller_id_health:%'").fetchall()
        for key, raw in rows:
            state = json.loads(raw)
            if (state.get("status") != "active" or state.get("operation") != "shift" or
                    state.get("verify_logged") or not state.get("applied_version")):
                continue
            wid, caller_id = state["workflow_id"], state["caller_id"]
            after_id = self.store.version_pickup_since(
                wid, state["applied_version"], caller_id, state["verify_after_event_id"])
            after_workflow = self.store.version_pickup_since(
                wid, state["applied_version"], None, state["verify_after_event_id"])
            state["verify_after_event_id"] = max(after_id["last_event_id"], after_workflow["last_event_id"])
            state["verify_id_dials"] += after_id["dials"]
            state["verify_id_connected"] += after_id["connected"]
            state["verify_workflow_dials"] += after_workflow["dials"]
            state["verify_workflow_connected"] += after_workflow["connected"]
            if min(state["verify_id_dials"], state["verify_workflow_dials"]) < CALLER_ID_MIN_DIALS:
                self.store.set(key, state)
                continue
            id_rate = state["verify_id_connected"] / state["verify_id_dials"]
            wf_rate = state["verify_workflow_connected"] / state["verify_workflow_dials"]
            healthy = [other for other in self.store.caller_ids(wid) if other != caller_id and
                       self.store.get(self.caller_id_state_key(wid, other), {}).get("status") not in
                       {"active", "shift_pending", "shift_rejected", "shift_conflict"}]
            other_events = []
            for other in healthy:
                other_events.extend(self.store.caller_id_windows(wid, other)[0])
            rest = self.pickup_summary(other_events)
            recovered = (rest["dials"] >= CALLER_ID_MIN_DIALS and
                         id_rate >= rest["pickup_rate"] - CALLER_ID_MIN_GAP and
                         wf_rate >= state["pre_workflow_rate"] - CALLER_ID_MIN_GAP)
            self.store.audit(wid, "caller_id_verified" if recovered else "caller_id_not_recovered",
                             "Recorded caller-ID and workflow pickup after the governed allocation change.",
                             {"caller_id": caller_id, "before_caller_id": state["pre_caller_rate"],
                              "after_caller_id": id_rate, "before_workflow": state["pre_workflow_rate"],
                              "after_workflow": wf_rate, "after_dials": state["verify_id_dials"],
                              "comparison_rest": rest, "applied_version": state["applied_version"]},
                             self.last_ts, outcome="verified" if recovered else "not_recovered")
            state["verify_logged"] = True
            self.store.set(key, state)

    def evaluate_caller_id_health(self, wf, family_size):
        wid = wf["workflow_id"]
        caller_ids = self.store.caller_ids(wid)
        if len(caller_ids) < 2:
            return
        all_windows = {cid: self.store.caller_id_windows(wid, cid) for cid in caller_ids}
        states = {cid: self.store.get(self.caller_id_state_key(wid, cid),
                                      {"workflow_id": wid, "caller_id": cid, "status": "healthy"})
                  for cid in caller_ids}
        excluded = {cid for cid, state in states.items() if state.get("status") in
                    {"active", "shift_pending", "shift_rejected", "shift_conflict", "shift_blocked",
                     "restore_pending", "active_restore_rejected"}}
        alpha = ALPHA / max(1, family_size)
        for caller_id in caller_ids:
            state = states[caller_id]
            if state.get("status") not in {"healthy", "recovered", "active"}:
                continue
            current, previous = all_windows[caller_id]
            current_stats = self.pickup_summary(current)
            previous_stats = self.pickup_summary(previous)
            if min(current_stats["dials"], previous_stats["dials"]) < CALLER_ID_MIN_DIALS:
                continue
            rest_events = []
            for other in caller_ids:
                if other != caller_id and other not in excluded:
                    rest_events.extend(all_windows[other][0])
            rest_stats = self.pickup_summary(rest_events)
            if rest_stats["dials"] < CALLER_ID_MIN_DIALS:
                continue
            p_current_vs_rest_lower = one_sided_two_proportion_pvalue(
                current_stats["connected"], current_stats["dials"],
                rest_stats["connected"], rest_stats["dials"])
            p_current_vs_previous_lower = one_sided_two_proportion_pvalue(
                current_stats["connected"], current_stats["dials"],
                previous_stats["connected"], previous_stats["dials"])
            p_previous_vs_current_higher = one_sided_two_proportion_pvalue(
                previous_stats["connected"], previous_stats["dials"],
                current_stats["connected"], current_stats["dials"])
            current_rate = current_stats["pickup_rate"]
            previous_rate = previous_stats["pickup_rate"]
            rest_rate = rest_stats["pickup_rate"]
            evidence = {"caller_id": caller_id, "current_window": current_stats,
                        "previous_window": previous_stats, "pooled_other_ids": rest_stats,
                        "min_dials_per_window": CALLER_ID_MIN_DIALS,
                        "window_size_per_id": CALLER_ID_WINDOW, "minimum_gap": CALLER_ID_MIN_GAP,
                        "family_size_ids_across_workflows": family_size, "corrected_alpha": alpha,
                        "p_current_vs_rest": p_current_vs_rest_lower,
                        "p_current_vs_previous": p_current_vs_previous_lower}

            if state.get("status") == "active":
                if (previous_rate is not None and current_rate - previous_rate > CALLER_ID_MIN_GAP and
                        p_previous_vs_current_higher < alpha and
                        current_rate >= rest_rate - CALLER_ID_MIN_GAP):
                    self.store.audit(wid, "caller_id_recovery_detected",
                                     "The flagged ID's pickup recovered against its earlier window and is within the rest group's minimum-effect margin.",
                                     evidence, self.last_ts, outcome="restore_requested")
                    current_config = self.current_config(wid)
                    prior = state.get("original_weights") or {}
                    self._caller_id_change(wf, caller_id, current_config, prior, "restore", state, evidence)
                continue

            sudden_drop = (previous_rate - current_rate > CALLER_ID_MIN_GAP and
                           p_current_vs_previous_lower < alpha)
            population_drop = (rest_rate - current_rate > CALLER_ID_MIN_GAP and
                               p_current_vs_rest_lower < alpha)
            if not (sudden_drop and population_drop):
                continue

            current_config = self.current_config(wid)
            allocations = current_config["config"].get("allocations", {}).get("default", {}).get("caller_id", {})
            original_weights = {str(k): float(v) for k, v in allocations.items()}
            flagged_share = original_weights.get(caller_id, 0.0)
            healthy_weights = {k: v for k, v in original_weights.items() if k != caller_id and k not in excluded and v > 0}
            healthy_total = sum(healthy_weights.values())
            exploration = min(flagged_share, CALLER_ID_EXPLORATION_SHARE)
            if flagged_share <= exploration or healthy_total <= 0:
                continue
            target = {caller_id: exploration}
            target.update({k: (1 - exploration) * v / healthy_total for k, v in healthy_weights.items()})
            target = {k: round(v, 6) for k, v in target.items() if v > 0}
            all_calls = [ev for calls, _ in all_windows.values() for ev in calls]
            workflow_before = self.pickup_summary(all_calls)
            state.update({"workflow_id": wid, "caller_id": caller_id, "status": "healthy",
                          "original_weights": original_weights,
                          "pre_caller_rate": current_rate,
                          "pre_workflow_rate": workflow_before["pickup_rate"]})
            self.store.audit(wid, "caller_id_flag_detected",
                             "Recent pickup fell significantly below both the pooled other IDs and this ID's own earlier window.",
                             evidence, self.last_ts, outcome="shift_requested")
            self._caller_id_change(wf, caller_id, current_config, target, "shift", state, evidence)

    def submit_queued(self, workflows):
        by_id = {w["workflow_id"]: w for w in workflows}
        for row in self.store.proposal_rows("queued"):
            wf = by_id[row["workflow_id"]]
            if not self.in_approval_window(wf):
                continue
            old = json.loads(row["body"])
            fresh = self.current_config(row["workflow_id"])
            changed = set(old["diff"])
            edits = [json.loads(r[0]) for r in self.store.db.execute(
                "SELECT body FROM events WHERE workflow_id=? AND type='config_changed'", (row["workflow_id"],))]
            conflicts = [e for e in edits if e.get("actor") == "supervisor" and e.get("version", 0) > row["base_version"]
                         and set(e.get("changed_dims", [])) & changed]
            if conflicts:
                self.store.db.execute("UPDATE proposals SET status='abandoned_conflict' WHERE proposal_id=?", (row["proposal_id"],))
                self.store.db.commit()
                exp = self.store.get(f"experiment:{row['workflow_id']}")
                if exp:
                    exp["phase"] = "invalidated"
                    self.store.set(f"experiment:{row['workflow_id']}", exp)
                self.store.audit(row["workflow_id"], "queued_conflict_abandoned",
                                 "A supervisor edited a proposed dimension while it waited; no change was submitted.",
                                 {"supervisor_events": conflicts}, self.last_ts, outcome="abandoned")
                continue
            proposed = self.apply_dimensions(fresh["config"], old["proposed_config"], changed)
            self.store.db.execute("UPDATE proposals SET status='superseded' WHERE proposal_id=?", (row["proposal_id"],))
            self.store.db.commit()
            new_id = self.create_proposal(wf, fresh, proposed, old["change_summary"], old["rationale"], {})
            self.replace_caller_id_proposal(row["workflow_id"], row["proposal_id"], new_id)
            exp = self.store.get(f"experiment:{row['workflow_id']}")
            if exp and exp.get("proposal_id") == row["proposal_id"] and new_id:
                exp["proposal_id"] = new_id
                exp["phase"] = "proposal_pending" if exp.get("phase") != "rollback_pending" else "rollback_pending"
                self.store.set(f"experiment:{row['workflow_id']}", exp)

    def reconcile_proposals(self, workflows):
        by_id = {w["workflow_id"]: w for w in workflows}
        for row in self.store.proposal_rows("submitting"):
            # A crash after the request but before saving its response is reconciled by idempotency key.
            approvals = self.get("/approvals", params={"workflow_id": row["workflow_id"]})
            found = next((a for a in approvals if a.get("proposal_id") == row["proposal_id"]), None)
            if found:
                self.store.db.execute("UPDATE proposals SET approval_id=?,status=? WHERE proposal_id=?",
                                      (found["approval_id"], found["status"], row["proposal_id"]))
                self.store.db.commit()
        for row in self.store.proposal_rows("pending"):
            approval = self.get(f"/approvals/{row['approval_id']}")
            status = approval.get("status")
            if status == "approved":
                observed = self.current_config(row["workflow_id"])
                approved_body = json.loads(row["body"])
                if (observed["version"] > row["base_version"] and
                        observed["config"] == approved_body["proposed_config"]):
                    status = "applied"
                    exp = self.store.get(f"experiment:{row['workflow_id']}")
                    if exp and exp.get("proposal_id") == row["proposal_id"]:
                        if exp["phase"] == "promotion_pending":
                            exp["phase"] = "complete_promoted"
                        elif exp["phase"] == "rollback_pending":
                            exp["phase"] = "complete_rolled_back"
                        else:
                            exp["phase"] = "running"
                        exp["start_version"] = observed["version"]
                        self.store.set(f"experiment:{row['workflow_id']}", exp)
                    self.store.audit(row["workflow_id"], "apply_reconciled",
                                     "The approved config is already live; reconciled the successful apply after restart.",
                                     {"approval_id": row["approval_id"], "version": observed["version"]},
                                     self.last_ts, approver="simulated human approver", outcome="applied")
                    self.store.db.execute("UPDATE proposals SET status='applied' WHERE proposal_id=?", (row["proposal_id"],))
                    self.store.db.commit()
                    continue
                response = self.post(f"/workflows/{row['workflow_id']}/apply", {"approval_id": row["approval_id"]})
                if response.status_code == 409 and "stale_base_version" in response.text:
                    self.store.db.execute("UPDATE proposals SET status='stale' WHERE proposal_id=?", (row["proposal_id"],))
                    self.store.db.commit()
                    old_body = json.loads(row["body"])
                    wf = by_id[row["workflow_id"]]
                    fresh = self.current_config(row["workflow_id"])
                    # Preserve edits from the new supervisor base while replaying only our intended dimensions.
                    changed = old_body["diff"]
                    edits = [json.loads(r[0]) for r in self.store.db.execute(
                        "SELECT body FROM events WHERE workflow_id=? AND type='config_changed'",
                        (row["workflow_id"],))]
                    conflicts = [e for e in edits if e.get("actor") == "supervisor" and
                                 e.get("version", 0) > row["base_version"] and
                                 set(e.get("changed_dims", [])) & set(changed)]
                    if conflicts:
                        self.store.audit(row["workflow_id"], "stale_conflict_abandoned",
                                         "Supervisor edited a dimension this experiment also proposed; no re-proposal was made.",
                                         {"approval_id": row["approval_id"], "supervisor_events": conflicts}, self.last_ts,
                                         outcome="abandoned")
                        exp = self.store.get(f"experiment:{row['workflow_id']}")
                        if exp:
                            exp["phase"] = "invalidated"
                            self.store.set(f"experiment:{row['workflow_id']}", exp)
                        self.store.db.execute("UPDATE proposals SET status='abandoned_conflict' WHERE proposal_id=?", (row["proposal_id"],))
                        self.store.db.commit()
                        continue
                    proposed = self.apply_dimensions(fresh["config"], old_body["proposed_config"], changed)
                    self.store.audit(row["workflow_id"], "stale_approval", "Approval was based on an old version; re-proposed the same dimensions on the supervisor's current config.",
                                     {"approval_id": row["approval_id"], "new_base": fresh["version"]}, self.last_ts)
                    new_id = self.create_proposal(wf, fresh, proposed, old_body["change_summary"], old_body["rationale"], {})
                    self.replace_caller_id_proposal(row["workflow_id"], row["proposal_id"], new_id)
                    exp = self.store.get(f"experiment:{row['workflow_id']}")
                    if exp and exp.get("proposal_id") == row["proposal_id"] and new_id:
                        exp["proposal_id"] = new_id
                        self.store.set(f"experiment:{row['workflow_id']}", exp)
                    continue
                if response.status_code == 410:
                    status = "lapsed"
                elif response.is_error:
                    self.store.audit(row["workflow_id"], "apply_error", response.text, {"approval_id": row["approval_id"]}, self.last_ts)
                    continue
                else:
                    status = "applied"
                    old = approved_body
                    exp = self.store.get(f"experiment:{row['workflow_id']}")
                    if exp and exp.get("proposal_id") == row["proposal_id"]:
                        if exp["phase"] == "promotion_pending":
                            exp["phase"] = "complete_promoted"
                        elif exp["phase"] == "rollback_pending":
                            exp["phase"] = "complete_rolled_back"
                        else:
                            exp["phase"] = "running"
                        exp["start_version"] = response.json().get("version")
                        self.store.set(f"experiment:{row['workflow_id']}", exp)
                    self.store.audit(row["workflow_id"], "change_applied", old["change_summary"],
                                     {"approval_id": row["approval_id"], "proposal_id": row["proposal_id"], "response": response.json()},
                                     self.last_ts, approver="simulated human approver", outcome="applied")
            elif status in ("rejected", "expired", "lapsed"):
                final_status = status
                self.store.audit(row["workflow_id"], "proposal_" + status, "Approval lifecycle completed without application.",
                                 {"approval_id": row["approval_id"], "reasons": approval.get("reasons", [])}, self.last_ts,
                                 approver="simulated human approver", outcome=status)
                if status in ("expired", "lapsed"):
                    old_body = json.loads(row["body"])
                    wf = by_id[row["workflow_id"]]
                    fresh = self.current_config(row["workflow_id"])
                    changed = set(old_body["diff"])
                    edits = [json.loads(r[0]) for r in self.store.db.execute(
                        "SELECT body FROM events WHERE workflow_id=? AND type='config_changed'", (row["workflow_id"],))]
                    conflict = any(e.get("actor") == "supervisor" and e.get("version", 0) > row["base_version"]
                                   and set(e.get("changed_dims", [])) & changed for e in edits)
                    if conflict:
                        self.store.audit(row["workflow_id"], "expired_conflict_abandoned",
                                         "The proposal expired after a supervisor changed one of its dimensions; it was not re-queued.",
                                         {"approval_id": row["approval_id"]}, self.last_ts, outcome="abandoned")
                    else:
                        proposed = self.apply_dimensions(fresh["config"], old_body["proposed_config"], changed)
                        new_id = self.create_proposal(wf, fresh, proposed, old_body["change_summary"],
                                                      old_body["rationale"] + " Fresh proposal after prior expiry.", {})
                        self.replace_caller_id_proposal(row["workflow_id"], row["proposal_id"], new_id)
                        exp = self.store.get(f"experiment:{row['workflow_id']}")
                        if exp and exp.get("proposal_id") == row["proposal_id"] and new_id:
                            exp["proposal_id"] = new_id
                            self.store.set(f"experiment:{row['workflow_id']}", exp)
                status = final_status
            if status != "pending":
                self.store.db.execute("UPDATE proposals SET status=? WHERE proposal_id=?", (status, row["proposal_id"]))
                self.store.db.commit()

    @staticmethod
    def _rates(rows, voice, primary, guardrail, config_version):
        n = p = gn = g = 0
        for row in rows:
            ev = json.loads(row[0])
            if ev.get("cohort_id", "default") != "default" or ev.get("variant", {}).get("voice") != voice:
                continue
            if ev.get("config_version") != config_version:
                continue
            connected, engaged = bool(ev.get("connected")), bool(ev.get("engaged"))
            if primary == "pickup_rate":
                n += 1; p += int(connected)
            else:
                if connected:
                    n += 1; p += int(engaged)
            if guardrail == "pickup_rate":
                gn += 1; g += int(connected)
            elif connected:
                gn += 1; g += int(engaged)
        return {"primary_success": p, "primary_n": n, "guardrail_success": g, "guardrail_n": gn}

    def evaluate_experiment(self, wf, family_size=1):
        wid = wf["workflow_id"]
        exp = self.store.get(f"experiment:{wid}")
        if not exp or exp.get("phase") != "running":
            return
        rows = self.store.event_rows(wid)
        a, b = exp["incumbent"], exp["candidate"]
        primary = wf.get("objective", {}).get("primary", "engagement_rate")
        guardrail = "pickup_rate" if primary == "engagement_rate" else "engagement_rate"
        guardrail_drop = wf.get("objective", {}).get("guardrail", {}).get(
            f"{guardrail}_max_drop", GUARDRAIL_DROP)
        ra = self._rates(rows, a, primary, guardrail, exp.get("start_version"))
        rb = self._rates(rows, b, primary, guardrail, exp.get("start_version"))
        if min(ra["primary_n"], rb["primary_n"], ra["guardrail_n"], rb["guardrail_n"]) < MIN_ARM:
            return
        better, worse = (rb, ra) if rb["primary_success"] / rb["primary_n"] > ra["primary_success"] / ra["primary_n"] else (ra, rb)
        pvalue = two_proportion_pvalue(better["primary_success"], better["primary_n"], worse["primary_success"], worse["primary_n"])
        guard_rate_incumbent = ra["guardrail_success"] / ra["guardrail_n"]
        guard_rate_candidate = rb["guardrail_success"] / rb["guardrail_n"]
        guard_delta = guard_rate_candidate - guard_rate_incumbent
        guard_se = math.sqrt(
            guard_rate_incumbent * (1 - guard_rate_incumbent) / ra["guardrail_n"] +
            guard_rate_candidate * (1 - guard_rate_candidate) / rb["guardrail_n"]
        )
        # Require the full confidence bound to clear the business margin, not just a noisy point estimate.
        guardrail_lower_bound = guard_delta - GUARDRAIL_Z * guard_se
        threshold = ALPHA / max(1, family_size)
        evidence = {"arms": {a: ra, b: rb}, "primary": primary, "guardrail": guardrail,
                    "p_value": round(pvalue, 6), "alpha": threshold, "family_size": family_size,
                    "minimum_per_arm": MIN_ARM,
                    "guardrail_delta": round(guard_delta, 4),
                    "guardrail_lower_bound_95pct_one_sided": round(guardrail_lower_bound, 4),
                    "guardrail_floor": -guardrail_drop}
        primary_inconclusive = pvalue >= threshold
        guardrail_failed = guardrail_lower_bound < -guardrail_drop
        rate_a = ra["primary_success"] / ra["primary_n"]
        rate_b = rb["primary_success"] / rb["primary_n"]
        candidate_significantly_worse = rate_b < rate_a and pvalue < threshold
        if guardrail_failed or candidate_significantly_worse:
            reasons = []
            if guardrail_failed:
                reasons.append("guardrail_bound_failed")
            if candidate_significantly_worse:
                reasons.append("candidate_significantly_worse")
            evidence["no_winner_reason"] = reasons
            current = self.current_config(wid)
            cfg = copy.deepcopy(current["config"])
            cfg.setdefault("allocations", {}).setdefault("default", {})["voice"] = {a: 1.0}
            result = self.create_proposal(
                wf, current, cfg,
                f"Restore incumbent voice {a} after the candidate failed the experiment decision rule.",
                "The guardrail bound failed or the candidate was significantly worse on the primary KPI; request approval to restore the incumbent voice allocation.",
                {})
            exp["phase"] = "rollback_pending" if result else "rollback_not_submitted"
            exp["proposal_id"] = result
            self.store.set(f"experiment:{wid}", exp)
            self.store.audit(wid, "experiment_no_winner_rollback",
                             "The guardrail failed or the candidate was significantly worse; the incumbent rollback was sent through the standard approval path.",
                             evidence, self.last_ts,
                             outcome="rollback_pending" if result else "rollback_not_submitted")
            return
        if primary_inconclusive:
            evidence["no_winner_reason"] = ["primary_not_significant"]
            exp["phase"] = "complete_inconclusive"
            self.store.set(f"experiment:{wid}", exp)
            self.store.audit(wid, "experiment_no_winner_split_retained",
                             "The primary result was not significant and the guardrail passed; leave the approved split in place without promoting either arm.",
                             evidence, self.last_ts, outcome="split_retained")
            return
        winner = b if better is rb else a
        current = self.current_config(wid)
        cfg = copy.deepcopy(current["config"])
        cfg.setdefault("allocations", {}).setdefault("default", {})["voice"] = {winner: 1.0}
        result = self.create_proposal(wf, current, cfg, f"Promote voice {winner} after a population experiment.",
                                      "It had the higher primary KPI, passed a two-proportion test at 0.05, and stayed within the guardrail.", evidence)
        exp["phase"] = "promotion_pending" if result else "complete_no_change"
        exp["proposal_id"] = result
        self.store.set(f"experiment:{wid}", exp)

    def maybe_start_experiment(self, wf):
        wid = wf["workflow_id"]
        if self.store.get(f"experiment:{wid}") or any(r["workflow_id"] == wid for r in self.store.proposal_rows("pending")):
            return
        opts = wf.get("options", {}).get("voice", [])
        if len(opts) < 2:
            return
        try:
            hour = datetime.fromisoformat(self.last_ts).hour
        except ValueError:
            return
        hours = wf.get("governance", {}).get("approver_hours", [10, 19])
        earliest = max(hours[0], 11 if wf.get("tenant_id") == "kestrel_finance" else hours[0])
        if not earliest <= hour < hours[1]:
            return
        current = self.current_config(wid)
        default = current["config"].get("allocations", {}).get("default", {}).get("voice", {})
        incumbent = max(default, key=default.get) if default else opts[0]
        candidate = next((v for v in opts if v != incumbent), None)
        if not candidate:
            return
        # One challenger only: this avoids splitting the population and keeps the family-wise alpha at 0.05.
        cfg = copy.deepcopy(current["config"])
        cfg.setdefault("allocations", {}).setdefault("default", {})["voice"] = {incumbent: 0.5, candidate: 0.5}
        exp = {"phase": "proposal_pending", "incumbent": incumbent, "candidate": candidate,
               "base_version": current["version"], "started_at": self.last_ts}
        self.store.set(f"experiment:{wid}", exp)
        result = self.create_proposal(wf, current, cfg, f"Test voice {candidate} against {incumbent} on the default cohort.",
                                      "Run a two-arm, 50/50 population experiment; no call-level result triggers a change.",
                                      {"design": "single challenger", "minimum_per_arm": MIN_ARM, "alpha": ALPHA})
        exp["proposal_id"] = result
        if not result:
            exp["phase"] = "blocked"
        self.store.set(f"experiment:{wid}", exp)

    def run(self):
        self.bootstrap_cursor()
        initial = self.get("/sim/state")
        self.credits_seen = initial.get("credits_remaining")
        while True:
            state = self.get("/sim/state")
            if state.get("finished"):
                break
            try:
                before_minute = state.get("sim_minute")
                response = self.post("/sim/advance", {"minutes": 15})
                if response.status_code == 409 and "finished" in response.text:
                    break
                if response.is_error and response.status_code < 500:
                    raise RuntimeError(f"simulator rejected advance: {response.status_code} {response.text}")
                response.raise_for_status()
                advanced = response.json()
                self.last_ts = advanced.get("ts", self.last_ts)
            except httpx.HTTPError as exc:
                self.store.audit(None, "advance_error", str(exc), {"before_minute": before_minute}, self.last_ts)
                recovered = self.get("/sim/state")
                if recovered.get("sim_minute", before_minute) > before_minute:
                    self.last_ts = recovered.get("ts", self.last_ts)
                    self.ingest()
                    self.process_config_events()
                    workflows = self.workflow_list()
                    self.reconcile_proposals(workflows)
                    self.submit_queued(workflows)
                    self.sync_caller_id_proposals(workflows)
                    caller_family_size = self.caller_id_family_size(workflows)
                    self.verify_caller_id_shifts(workflows, caller_family_size)
                    for wf in workflows:
                        self.evaluate_experiment(wf, family_size=len(workflows))
                        self.evaluate_caller_id_health(wf, caller_family_size)
                        self.maybe_start_experiment(wf)
                else:
                    time.sleep(1)
                continue
            self.ingest()
            self.process_config_events()
            workflows = self.workflow_list()
            self.reconcile_proposals(workflows)
            self.submit_queued(workflows)
            self.sync_caller_id_proposals(workflows)
            caller_family_size = self.caller_id_family_size(workflows)
            self.verify_caller_id_shifts(workflows, caller_family_size)
            for wf in workflows:
                self.evaluate_experiment(wf, family_size=len(workflows))
                self.evaluate_caller_id_health(wf, caller_family_size)
                # A workflow gets one exploration proposal; promotion follows from its observed data.
                self.maybe_start_experiment(wf)
            state = self.get("/sim/state")
            credits = state.get("credits_remaining")
            if credits is not None and credits != self.credits_seen:
                self.store.audit(None, "credit_change", "Observed scenario credit balance changed.",
                                 {"from": self.credits_seen, "to": credits}, self.last_ts)
                self.credits_seen = credits
        self.ingest()
        self.process_config_events()
        workflows = self.workflow_list()
        self.reconcile_proposals(workflows)
        self.sync_caller_id_proposals(workflows)
        self.verify_caller_id_shifts(workflows, self.caller_id_family_size(workflows))
        self.store.audit(None, "finished", "Simulator reports the scenario is complete.",
                         {"chunk_cursor": self.store.get("chunk_cursor"), "credits_remaining": self.credits_seen}, self.last_ts)
        print(json.dumps({"status": "finished", "timestamp": self.last_ts,
                          "chunk_cursor": self.store.get("chunk_cursor"), "credits_remaining": self.credits_seen,
                          "ingested_events": self.ingested_events,
                          "ingest_events_per_second": round(self.ingested_events / max(self.ingest_wall, 1e-9), 1),
                          "max_event_lag_sim_minutes": round(self.max_event_lag_minutes, 1),
                          "wall_seconds": round(time.perf_counter() - self.started_wall, 2)}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--db", default=os.environ.get("CONTROLLER_DB", "controller.sqlite3"))
    args = parser.parse_args()
    Controller(args.url, args.db).run()


if __name__ == "__main__":
    main()
