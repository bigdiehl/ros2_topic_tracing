"""End to end: trace_ctl start, talker + listener, topic_stats -s, diagnostics rules, stop.

Needs ROS sourced, lttng-tools, a built trace_daemon. Uses the fixed session names
ros2mon_meta/ros2mon_hot, so it is opt-in: ROS2MON_LIVE_TEST=1.
"""
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

from conftest import ROOT

pytestmark = pytest.mark.skipif(os.environ.get("ROS2MON_LIVE_TEST") != "1",
                                reason="set ROS2MON_LIVE_TEST=1")


def py(*args, **kw):
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True,
                          timeout=60, **kw)


@pytest.fixture
def tracing(daemon, tmp_path, monkeypatch):
    assert shutil.which("lttng") and shutil.which("ros2")
    monkeypatch.setenv("ROS2MON_SOCKET", str(tmp_path / "ros2mon.sock"))
    # keep other hosts' /chatter out of the counts
    monkeypatch.setenv("ROS_DOMAIN_ID", str(100 + os.getpid() % 100))
    monkeypatch.setenv("ROS_AUTOMATIC_DISCOVERY_RANGE", "LOCALHOST")
    r = py("trace_ctl.py", "start")
    assert r.returncode == 0, r.stderr
    # the executables, not `ros2 run` (its wrapper doesn't pass SIGINT on promptly)
    lib = subprocess.check_output(["ros2", "pkg", "prefix", "demo_nodes_cpp"], text=True).strip()
    procs = [subprocess.Popen([f"{lib}/lib/demo_nodes_cpp/{n}"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL) for n in ("talker", "listener")]
    time.sleep(2)
    try:
        yield
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(10)
        r = py("trace_ctl.py", "stop")
        assert r.returncode == 0, r.stderr


def rows_for(out, direction):
    return [e for e in json.loads(out)["endpoints"]
            if e["topic"] == "/chatter" and e["dir"] == direction]


def test_sample(tracing):
    # names only: nothing traced yet
    r = py("topic_stats.py", "-f", "json", "--known", "-t", "/chatter")
    assert r.returncode == 0, r.stderr
    pub = rows_for(r.stdout, "pub")
    assert pub and not pub[0]["tracked"]

    r = py("topic_stats.py", "-s", "3", "-f", "json", "-t", "/chatter")
    assert r.returncode == 0, r.stderr
    pub, sub = rows_for(r.stdout, "pub"), rows_for(r.stdout, "sub")
    assert pub[0]["node"] == "/talker" and sub[0]["node"] == "/listener"
    assert pub[0]["count"] >= 2 and sub[0]["count"] >= 2
    assert 0.5 <= pub[0]["rate"] <= 1.5

    # the sample's rules went away with its connection
    status = json.loads(py("trace_ctl.py", "status", "-f", "json").stdout)
    assert status["rules"] == []


def test_persistent_rule(tracing):
    assert py("trace_ctl.py", "track", "-n", "/talker").returncode == 0
    time.sleep(3)
    r = py("topic_stats.py", "-f", "json", "-t", "/chatter")
    pub = rows_for(r.stdout, "pub")
    assert pub and pub[0]["tracked"] and pub[0]["count"] >= 1
    assert not rows_for(r.stdout, "sub")        # listener's process is not traced
