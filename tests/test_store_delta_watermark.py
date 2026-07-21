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
    assert store.get_watermark("m1") is None
    store.set_watermark("m1", 42)
    assert store.get_watermark("m1") == 42


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
    rx = _run(store, tid, ["https://x.com/z"], monitor="m2")  # different monitor
    assert store.ack_monitor_run("m1", rx)["ok"] is False     # foreign monitor
    assert store.ack_monitor_run("m1", 999999)["ok"] is False  # nonexistent
    assert store.ack_monitor_run("m1", r1)["ok"] is True       # valid
    assert store.ack_monitor_run("m1", r1)["ok"] is False      # non-monotonic (<=)


def test_missing_previous_when_watermark_run_gone(store):
    # remove_topic deletes runs but leaves the watermark setting; the watermark
    # then points at a run that no longer exists.
    tid = store.add_topic("Alpha")["id"]
    store.set_watermark("m1", 999999)  # points at a missing run
    r2 = _run(store, tid, ["https://x.com/b"], monitor="m1")
    delta = store.compute_monitor_delta("m1", tid, r2)
    assert delta["status"] == "missing_previous"
    assert delta["new_findings"] == []
