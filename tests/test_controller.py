import json

from controller import Controller, Store, two_proportion_pvalue


def test_two_proportion_test_needs_a_real_population():
    assert two_proportion_pvalue(1, 2, 2, 2) == 1.0
    assert two_proportion_pvalue(350, 500, 250, 500) < 0.05
    assert two_proportion_pvalue(250, 500, 250, 500) > 0.9


def test_event_ingestion_is_idempotent_and_cursor_is_durable(tmp_path):
    db = tmp_path / "controller.sqlite3"
    line = json.dumps({"type": "call", "workflow_id": "wf", "event_id": 17, "connected": True})
    store = Store(str(db))
    store.add_chunk(4, [line])
    store.add_chunk(4, [line])
    assert store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    store.db.close()

    restarted = Store(str(db))
    assert restarted.get("chunk_cursor") == 4
    assert len(restarted.event_rows("wf")) == 1


def test_pending_approval_survives_store_reopen(tmp_path):
    db = tmp_path / "controller.sqlite3"
    store = Store(str(db))
    body = {"proposal_id": "stable-id", "workflow_id": "wf", "base_version": 1}
    store.db.execute("INSERT INTO proposals(proposal_id,workflow_id,base_version,body,approval_id,status,created_at) VALUES(?,?,?,?,?,?,?)",
                     ("stable-id", "wf", 1, json.dumps(body), "apr_1", "pending", "D0 10:00"))
    store.db.commit()
    store.db.close()

    restarted = Store(str(db))
    row = restarted.proposal_rows("pending")[0]
    assert row["proposal_id"] == "stable-id"
    assert row["approval_id"] == "apr_1"


def test_proposal_audit_records_submitted_impact_score(tmp_path):
    controller = Controller("http://simulator.invalid", str(tmp_path / "controller.sqlite3"))
    controller.last_ts = "2026-01-01T10:00:00+05:30"
    posted = {}

    class Response:
        status_code = 201
        is_error = False

        @staticmethod
        def json():
            return {"approval_id": "apr_1"}

    def fake_post(path, body):
        posted["path"] = path
        posted["body"] = body
        return Response()

    controller.post = fake_post
    wf = {"workflow_id": "wf", "daily_batch": {"size": 100},
          "governance": {"approver_hours": [10, 19]}}
    current = {"version": 1, "config": {"allocations": {"default": {"voice": {"V1": 1.0}}}}}
    proposed = {"allocations": {"default": {"voice": {"V2": 1.0}}}}
    evidence = {"p_value": 0.03, "arms": {
        "V1": {"primary_success": 50, "primary_n": 100},
        "V2": {"primary_success": 70, "primary_n": 100},
    }}

    controller.create_proposal(wf, current, proposed, "promote V2", "measured winner", evidence)

    audit = controller.store.db.execute(
        "SELECT evidence FROM decisions WHERE action='proposal_created'").fetchone()
    audited_evidence = json.loads(audit[0])
    assert posted["path"] == "/approvals"
    assert posted["body"]["impact_score"] == 1.0
    assert audited_evidence["impact_score"] == posted["body"]["impact_score"]


def test_reconcile_does_not_recreate_pending_proposal_after_reopen(tmp_path):
    db = tmp_path / "controller.sqlite3"
    store = Store(str(db))
    body = {"proposal_id": "stable-id", "workflow_id": "wf", "base_version": 1}
    store.db.execute(
        "INSERT INTO proposals(proposal_id,workflow_id,base_version,body,approval_id,status,created_at) VALUES(?,?,?,?,?,?,?)",
        ("stable-id", "wf", 1, json.dumps(body), "apr_1", "pending", "2026-01-01T10:00:00+05:30"))
    store.db.commit()
    store.db.close()

    controller = Controller("http://simulator.invalid", str(db))
    controller.get = lambda path, **kwargs: {"status": "pending"}
    controller.reconcile_proposals([{"workflow_id": "wf"}])

    rows = controller.store.proposal_rows()
    assert len(rows) == 1
    assert rows[0]["proposal_id"] == "stable-id"
    assert rows[0]["approval_id"] == "apr_1"
    assert rows[0]["status"] == "pending"


def _controller_with_experiment(tmp_path, v1_connected, v1_engaged, v2_connected, v2_engaged):
    controller = Controller("http://simulator.invalid", str(tmp_path / "controller.sqlite3"))
    controller.last_ts = "2026-01-01T12:00:00+05:30"
    controller.store.set("experiment:wf", {
        "phase": "running", "incumbent": "V1", "candidate": "V2", "start_version": 2,
        "base_version": 1, "proposal_id": "split-proposal",
    })
    events = []
    event_id = 0
    for voice, connected_n, engaged_n in (
        ("V1", v1_connected, v1_engaged), ("V2", v2_connected, v2_engaged)
    ):
        for i in range(max(1000, connected_n)):
            events.append(json.dumps({
                "event_id": event_id, "type": "call", "workflow_id": "wf",
                "cohort_id": "default", "config_version": 2,
                "variant": {"voice": voice}, "connected": i < connected_n,
                "engaged": i < engaged_n,
            }))
            event_id += 1
    controller.store.add_chunk(1, events)
    controller.current_config = lambda wid: {
        "version": 2,
        "config": {"allocations": {"default": {"voice": {"V1": 0.5, "V2": 0.5}}}},
    }
    captured = {}

    def fake_create_proposal(wf, current, proposed, summary, why, evidence):
        captured.update({"proposed": proposed, "summary": summary, "why": why, "evidence": evidence})
        return "rollback-proposal"

    controller.create_proposal = fake_create_proposal
    wf = {"workflow_id": "wf", "objective": {
        "primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}
    }}
    return controller, wf, captured


def _assert_rollback_requested(controller, wf, captured, expected_reason):
    controller.evaluate_experiment(wf)
    exp = controller.store.get("experiment:wf")
    assert exp["phase"] == "rollback_pending"
    assert exp["proposal_id"] == "rollback-proposal"
    assert captured["proposed"]["allocations"]["default"]["voice"] == {"V1": 1.0}
    row = controller.store.db.execute(
        "SELECT evidence FROM decisions WHERE action='experiment_no_winner_rollback'").fetchone()
    assert expected_reason in json.loads(row[0])["no_winner_reason"]


def test_inconclusive_experiment_proposes_incumbent_rollback(tmp_path):
    controller, wf, captured = _controller_with_experiment(
        tmp_path, v1_connected=500, v1_engaged=250, v2_connected=500, v2_engaged=250)
    _assert_rollback_requested(controller, wf, captured, "primary_not_significant")


def test_guardrail_failure_proposes_incumbent_rollback(tmp_path):
    controller, wf, captured = _controller_with_experiment(
        tmp_path, v1_connected=900, v1_engaged=540, v2_connected=700, v2_engaged=600)
    _assert_rollback_requested(controller, wf, captured, "guardrail_bound_failed")
