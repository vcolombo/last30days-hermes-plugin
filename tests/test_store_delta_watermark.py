"""Per-monitor delivery watermark + union-of-unreported delta (never-lose
scheduled monitoring)."""

import pytest

import store as store_mod


@pytest.fixture()
def store(tmp_path):
    # conftest.py already puts scripts/ on sys.path, so `import store` works.
    with store_mod.scoped_db(tmp_path / "research.db"):
        store_mod.init_db()
        yield store_mod


def _run(store, topic_id, urls, monitor=None):
    run_id = store.record_run(topic_id, source_mode="test",
                              status="running", monitor=monitor)
    findings = [
        {"title": f"t-{u}", "url": u, "source": "x",
         "engagement_score": 1.0, "relevance_score": 1.0, "excerpt": ""}
        for u in urls
    ]
    store.store_findings(run_id, topic_id, findings)
    store.update_run(run_id, status="completed")
    return run_id


def _new_urls(delta):
    return {f["source_url"] for f in delta["new_findings"]}


def test_watermark_roundtrip_and_default_none(store):
    tid = store.add_topic("Alpha")["id"]
    assert store.get_watermark("m1", tid) is None
    store.set_watermark("m1", tid, 42)
    assert store.get_watermark("m1", tid) == 42


def test_cross_topic_monitor_label_isolated(store):
    ta = store.add_topic("Alpha")["id"]
    tb = store.add_topic("Beta")["id"]
    ra = _run(store, ta, ["https://x.com/a"], monitor="m1")
    assert store.ack_monitor_run("m1", ra)["ok"] is True  # advances (m1, Alpha)
    # Beta reuses the same monitor label -> independent watermark, still baseline.
    rb = _run(store, tb, ["https://x.com/b"], monitor="m1")
    assert store.compute_monitor_delta("m1", tb, rb)["status"] == "baseline"
    assert store.get_watermark("m1", ta) == ra
    assert store.get_watermark("m1", tb) is None


def test_baseline_when_no_watermark(store):
    tid = store.add_topic("Alpha")["id"]
    r = _run(store, tid, ["https://x.com/a"], monitor="m1")
    delta = store.compute_monitor_delta("m1", tid, r)
    assert delta["status"] == "baseline"
    assert delta["counts"] == {"new": 0, "continued": 0, "dropped": 0}
    assert delta["new_findings"] == []


def test_union_never_loses_an_unacked_finding(store):
    """The core guarantee: a finding surfaced in an undelivered (unacked) run
    and gone by the next run must still be reported — it stays in the union
    until a run that contains it is acked."""
    tid = store.add_topic("Alpha")["id"]
    r1 = _run(store, tid, ["https://x.com/a", "https://x.com/b"], monitor="m1")
    assert store.ack_monitor_run("m1", r1)["ok"] is True   # r1 delivered

    # r2 surfaces c, but its delivery FAILS -> not acked.
    r2 = _run(store, tid, ["https://x.com/a", "https://x.com/c"], monitor="m1")
    assert _new_urls(store.compute_monitor_delta("m1", tid, r2)) == {"https://x.com/c"}

    # r3 no longer surfaces c. Union of unreported runs (r2, r3) vs watermark r1
    # still contains c -> not lost, alongside the new d.
    r3 = _run(store, tid, ["https://x.com/a", "https://x.com/d"], monitor="m1")
    assert _new_urls(store.compute_monitor_delta("m1", tid, r3)) == {
        "https://x.com/c", "https://x.com/d"}


def test_interactive_run_not_in_union(store):
    tid = store.add_topic("Alpha")["id"]
    r1 = _run(store, tid, ["https://x.com/a"], monitor="m1")
    store.ack_monitor_run("m1", r1)
    _run(store, tid, ["https://x.com/zzz"], monitor=None)  # interactive, untagged
    r2 = _run(store, tid, ["https://x.com/b"], monitor="m1")
    assert _new_urls(store.compute_monitor_delta("m1", tid, r2)) == {"https://x.com/b"}


def test_ack_validation_rejects_foreign_nonexistent_and_nonmonotonic(store):
    tid = store.add_topic("Alpha")["id"]
    r1 = _run(store, tid, ["https://x.com/a"], monitor="m1")
    r2 = _run(store, tid, ["https://x.com/b"], monitor="m1")
    rx = _run(store, tid, ["https://x.com/z"], monitor="m2")  # different monitor
    assert store.ack_monitor_run("m1", rx)["ok"] is False     # foreign monitor
    assert store.ack_monitor_run("m1", 999999)["ok"] is False  # nonexistent
    assert store.ack_monitor_run("m1", r2)["ok"] is True       # advances to r2
    assert store.ack_monitor_run("m1", r2)["ok"] is True       # idempotent re-ack
    assert store.ack_monitor_run("m1", r1)["ok"] is False      # regression (r1 < r2)
    assert store.get_watermark("m1", tid) == r2                # watermark held


def test_missing_previous_when_watermark_run_gone(store):
    # remove_topic deletes runs but leaves the watermark setting; the watermark
    # then points at a run that no longer exists.
    tid = store.add_topic("Alpha")["id"]
    store.set_watermark("m1", tid, 999999)  # points at a missing run
    r2 = _run(store, tid, ["https://x.com/b"], monitor="m1")
    delta = store.compute_monitor_delta("m1", tid, r2)
    assert delta["status"] == "missing_previous"
    assert delta["new_findings"] == []


def test_lease_serializes_same_monitor_topic(store):
    assert store.acquire_lease("m1", "Alpha", "owner-a") is True
    assert store.acquire_lease("m1", "Alpha", "owner-b") is False   # busy
    store.release_lease("m1", "Alpha", "owner-a")
    assert store.acquire_lease("m1", "Alpha", "owner-b") is True     # freed


def test_lease_reclaims_stale(store):
    assert store.acquire_lease("m1", "Alpha", "owner-a", ttl_seconds=0) is True
    # ttl 0 -> already stale -> a different owner reclaims it.
    assert store.acquire_lease("m1", "Alpha", "owner-b", ttl_seconds=0) is True


def test_lease_different_topic_independent(store):
    assert store.acquire_lease("m1", "Alpha", "a") is True
    assert store.acquire_lease("m1", "Beta", "b") is True   # different topic


def test_reset_only_clears_when_watermark_still_missing(store):
    """Straggler-reset race: reset must NOT delete a watermark that has since been
    re-baselined to a real run. It clears only a still-missing watermark."""
    tid = store.add_topic("Alpha")["id"]
    # Case A: watermark points at a missing run -> reset clears it.
    store.set_watermark("m1", tid, 999999)
    store.reset_watermark("m1", tid)
    assert store.get_watermark("m1", tid) is None
    # Case B: a real run got acked in between -> a late reset is a no-op.
    r = _run(store, tid, ["https://x.com/a"], monitor="m1")
    store.ack_monitor_run("m1", r)
    store.reset_watermark("m1", tid)                 # straggler
    assert store.get_watermark("m1", tid) == r       # preserved


def test_set_watermark_if_unset_anchors_once(store):
    tid = store.add_topic("Alpha")["id"]
    assert store.set_watermark_if_unset("m1", tid, 5) is True
    assert store.get_watermark("m1", tid) == 5
    assert store.set_watermark_if_unset("m1", tid, 9) is False   # already set
    assert store.get_watermark("m1", tid) == 5                   # not overwritten


def test_lease_held_by_reflects_ownership_and_reclaim(store):
    assert store.acquire_lease("m1", "Alpha", "a") is True
    assert store.lease_held_by("m1", "Alpha", "a") is True
    assert store.lease_held_by("m1", "Alpha", "b") is False      # not the holder
    # TTL 0 -> a's lease is stale -> b reclaims -> a no longer holds it (fence).
    assert store.acquire_lease("m1", "Alpha", "b", ttl_seconds=0) is True
    assert store.lease_held_by("m1", "Alpha", "a") is False


def test_complete_monitor_run_fences_reclaimed_lease(store):
    """The atomic fence (#2): a run completes only while its owner holds the
    lease. A run whose lease was reclaimed can't flip to completed (which would
    strand it below the reclaimer's watermark)."""
    tid = store.add_topic("Alpha")["id"]
    assert store.acquire_lease("m1", "Alpha", "a") is True
    r1 = store.record_run(tid, source_mode="t", status="running", monitor="m1")
    assert store.complete_monitor_run("m1", "Alpha", "a", r1,
                                      findings_new=0, findings_updated=0) is True
    assert store._run_row(r1)["status"] == "completed"
    # b reclaims the lease (ttl 0 -> a's is stale); a's next run can't complete.
    r2 = store.record_run(tid, source_mode="t", status="running", monitor="m1")
    assert store.acquire_lease("m1", "Alpha", "b", ttl_seconds=0) is True
    assert store.complete_monitor_run("m1", "Alpha", "a", r2,
                                      findings_new=0, findings_updated=0) is False
    assert store._run_row(r2)["status"] == "running"     # fenced, not completed


def test_migration_reapply_recreates_missing_index(store):
    """#4: a crash between the v3 ALTER and its CREATE INDEX must self-heal on the
    next migration — the caught duplicate-column on the ALTER must NOT skip the
    following CREATE INDEX (which executescript would have)."""
    conn = store._connect()
    try:
        # Simulate the crash aftermath: monitor column exists (from init) but the
        # index is gone and the version is rewound to before v3.
        conn.execute("DROP INDEX IF EXISTS idx_research_runs_monitor")
        conn.execute("DELETE FROM schema_version WHERE version >= 3")
        conn.commit()
        store._run_migrations(conn)   # ALTER -> duplicate column (caught)
        conn.commit()
        idx = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_research_runs_monitor'").fetchone()
        assert idx is not None        # index recreated despite the caught ALTER
    finally:
        conn.close()


def test_run_migrations_idempotent_on_reapply(store):
    """Concurrent first-migration safety: the losing racer re-runs migrations
    against a DB whose columns/tables already exist and whose version rows are
    already recorded. Duplicate-column is caught and the version insert is
    OR IGNORE, so _run_migrations must be a clean no-op instead of raising."""
    conn = store._connect()
    try:
        # Pretend this process only knows about v1, though the schema is fully
        # migrated (the state a second starter sees after the first migrated).
        conn.execute("DELETE FROM schema_version WHERE version > 1")
        conn.commit()
        store._run_migrations(conn)          # must not raise
        conn.commit()
        v = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert v == max(store.MIGRATIONS.keys())
    finally:
        conn.close()
