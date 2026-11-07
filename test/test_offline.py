"""trace_daemon fs mode on a recorded trace (test/data: demo_nodes_cpp talker + listener, ~5 s)."""
import subprocess

import trace_stats as ts
from conftest import FIXTURE


def one(rows, topic, direction, node):
    hit = [r for r in rows if (r["topic"], r["dir"], r["node"]) == (topic, direction, node)]
    assert len(hit) == 1, hit
    return hit[0]


def test_check(daemon):
    r = subprocess.run([daemon, "check"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "ok"


def test_chatter_counts(daemon):
    snap = ts.load_trace(FIXTURE)
    assert snap["discarded"] == 0 and snap["discarded_meta"] == 0
    pub = one(snap["endpoints"], "/chatter", "pub", "/talker")
    sub = one(snap["endpoints"], "/chatter", "sub", "/listener")
    assert pub["proc"] == "talker" and sub["proc"] == "listener"
    assert 4 <= pub["count"] <= 6
    assert 3 <= sub["count"] <= pub["count"]
    assert abs(pub["avg_rate"] - 1.0) < 0.05
    assert pub["from_start"] and pub["tracked"]


def test_window(daemon):
    full = one(ts.load_trace(FIXTURE)["endpoints"], "/chatter", "pub", "/talker")
    short = one(ts.load_trace(FIXTURE, window=2)["endpoints"], "/chatter", "pub", "/talker")
    assert short["count"] == full["count"]          # count is not windowed
    assert 0.4 <= short["rate"] <= 1.6


def test_filter_rows(daemon):
    rows = ts.load_trace(FIXTURE)["endpoints"]
    assert all(not ts.INTERNAL_TOPICS.search(r["topic"]) for r in ts.filter_rows(rows))
    assert {r["node"] for r in ts.filter_rows(rows, topics=["/chat*"], direction="sub")} == {"/listener"}
    assert ts.filter_rows(rows, nodes=["/nobody"]) == []
    assert any(r["topic"] == "/rosout" for r in ts.filter_rows(rows, internal=True))


def test_bad_trace(daemon, tmp_path):
    try:
        ts.load_trace(str(tmp_path))
    except ts.DaemonError:
        return
    raise AssertionError("expected DaemonError")
