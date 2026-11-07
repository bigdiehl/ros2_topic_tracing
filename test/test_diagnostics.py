"""Check.evaluate on synthetic rows. Needs rclpy + diagnostic_msgs (ROS sourced)."""
import pytest

pytest.importorskip("rclpy")
pytest.importorskip("diagnostic_msgs")
from topic_diagnostics import ERROR, OK, STALE, Check  # noqa: E402


def row(node, topic, direction, rate, tracked=True):
    return dict(node=node, topic=topic, dir=direction, rate=rate, tracked=tracked,
                last_age=0.1 if rate else None)


def test_pub_summed():
    c = Check({"topic": "/scan", "dir": "pub", "min_rate": 8, "max_rate": 12})
    rows = [row("/a", "/scan", "pub", 5), row("/b", "/scan", "pub", 5)]
    assert c.evaluate(rows).level == OK
    assert c.evaluate(rows[:1]).level == ERROR


def test_sub_worst_wins():
    c = Check({"topic": "/cmd", "dir": "sub", "min_rate": 8})
    st = c.evaluate([row("/a", "/cmd", "sub", 10), row("/b", "/cmd", "sub", 0)])
    assert st.level == ERROR and "/b" in st.message


def test_missing_and_stale():
    c = Check({"topic": "/x", "dir": "pub"})
    assert c.evaluate([]).level == ERROR
    assert c.evaluate([row("/a", "/x", "pub", 0, tracked=False)]).level == STALE
    assert c.evaluate([row("/a", "/x", "pub", 0)]).level == ERROR


def test_bad_dir():
    with pytest.raises(ValueError):
        Check({"topic": "/x", "dir": "both"})
