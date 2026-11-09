import json

from controller import Store, two_proportion_pvalue


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
