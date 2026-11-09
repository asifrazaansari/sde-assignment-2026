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
        """)
        self.db.commit()

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
                cur = self.db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?)",
                                      (eid, chunk, event.get("workflow_id"), event["type"], event.get("ts"),
                                       json.dumps(event, sort_keys=True)))
                inserted += cur.rowcount
            old = self.get("chunk_cursor", -1)
            self.db.execute("INSERT INTO state VALUES('chunk_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (json.dumps(max(old, chunk)),))
        return inserted

    def event_rows(self, workflow, kind="call"):
        return self.db.execute("SELECT body FROM events WHERE workflow_id=? AND type=? ORDER BY event_id",
                               (workflow, kind)).fetchall()

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
                         {"proposal_id": proposal_id, "approval_id": approval_id, "changed_dims": changed, "impact_score": 0.1}, self.last_ts)
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
                    self.create_proposal(wf, fresh, proposed, old_body["change_summary"], old_body["rationale"], {})
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
        guard_delta = (better["guardrail_success"] / better["guardrail_n"] -
                       worse["guardrail_success"] / worse["guardrail_n"])
        guard_rate_a = better["guardrail_success"] / better["guardrail_n"]
        guard_rate_b = worse["guardrail_success"] / worse["guardrail_n"]
        guard_se = math.sqrt(guard_rate_a * (1 - guard_rate_a) / better["guardrail_n"] +
                             guard_rate_b * (1 - guard_rate_b) / worse["guardrail_n"])
        # Require the full confidence bound to clear the business margin, not just a noisy point estimate.
        guardrail_lower_bound = guard_delta - GUARDRAIL_Z * guard_se
        threshold = ALPHA / max(1, family_size)
        evidence = {"arms": {a: ra, b: rb}, "primary": primary, "guardrail": guardrail,
                    "p_value": round(pvalue, 6), "alpha": threshold, "family_size": family_size,
                    "minimum_per_arm": MIN_ARM,
                    "guardrail_delta": round(guard_delta, 4),
                    "guardrail_lower_bound_95pct_one_sided": round(guardrail_lower_bound, 4),
                    "guardrail_floor": -guardrail_drop}
        if pvalue >= threshold or guardrail_lower_bound < -guardrail_drop:
            # An inconclusive result is not evidence that the incumbent is better.
            # Keep the already-approved balanced split live; never manufacture a winner.
            exp["phase"] = "complete_inconclusive"
            self.store.set(f"experiment:{wid}", exp)
            self.store.audit(wid, "experiment_no_winner",
                             "No arm cleared both the fixed significance threshold and guardrail; the approved 50/50 split remains live without promotion.",
                             evidence, self.last_ts, outcome="inconclusive_split_retained")
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
                    for wf in workflows:
                        self.evaluate_experiment(wf, family_size=len(workflows))
                        self.maybe_start_experiment(wf)
                else:
                    time.sleep(1)
                continue
            self.ingest()
            self.process_config_events()
            workflows = self.workflow_list()
            self.reconcile_proposals(workflows)
            self.submit_queued(workflows)
            for wf in workflows:
                self.evaluate_experiment(wf, family_size=len(workflows))
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
