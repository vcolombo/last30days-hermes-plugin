"""Per-monitor watermark + pinned-run delta (scheduled monitoring)."""

import pytest

import store as store_mod


@pytest.fixture()
def store(tmp_path):
    # conftest.py already puts scripts/ on sys.path, so `import store` works.
    # scoped_db isolates this test's writes to a throwaway db.
    with store_mod.scoped_db(tmp_path / "research.db"):
        store_mod.init_db()
        yield store_mod


def _run_with_urls(store, topic_id, urls):
    run_id = store.record_run(topic_id, source_mode="test", status="running")
    findings = [
        {"title": f"t-{u}", "url": u, "source": "x",
         "engagement_score": 1.0, "relevance_score": 1.0, "excerpt": ""}
        for u in urls
    ]
    store.store_findings(run_id, topic_id, findings)
    store.update_run(run_id, status="completed")
    return run_id


def test_watermark_roundtrip_and_default_none(store):
    assert store.get_watermark("m1") is None
    store.set_watermark("m1", 42)
    assert store.get_watermark("m1") == 42


def test_baseline_when_no_watermark(store):
    tid = store.add_topic("Alpha")["id"]
    run = _run_with_urls(store, tid, ["https://x.com/a"])
    delta = store.compute_delta_since_run(tid, run, None)
    assert delta["status"] == "baseline"
    assert delta["counts"] == {"new": 0, "continued": 0, "dropped": 0}
    assert delta["new_findings"] == []


def test_delta_new_continued_dropped_vs_pinned_run(store):
    tid = store.add_topic("Alpha")["id"]
    r1 = _run_with_urls(store, tid, ["https://x.com/a", "https://x.com/b"])
    r2 = _run_with_urls(store, tid, ["https://x.com/b", "https://x.com/c"])
    delta = store.compute_delta_since_run(tid, r2, r1)
    assert delta["status"] == "ok"
    assert delta["counts"] == {"new": 1, "continued": 1, "dropped": 1}
    assert [f["source_url"] for f in delta["new_findings"]] == ["https://x.com/c"]


def test_interactive_run_between_does_not_contaminate(store):
    # r1 is the watermark; an unrelated run happens; the monitor's r2 still
    # diffs against the PINNED r1, not "latest two".
    tid = store.add_topic("Alpha")["id"]
    r1 = _run_with_urls(store, tid, ["https://x.com/a"])
    _run_with_urls(store, tid, ["https://x.com/zzz"])  # interactive noise
    r2 = _run_with_urls(store, tid, ["https://x.com/a", "https://x.com/b"])
    delta = store.compute_delta_since_run(tid, r2, r1)
    assert [f["source_url"] for f in delta["new_findings"]] == ["https://x.com/b"]
