"""Client wire format against a fake daemon."""
import json
import socket
import threading

import pytest

import trace_stats as ts


@pytest.fixture
def server(tmp_path):
    path = str(tmp_path / "s.sock")
    lsock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    lsock.bind(path)
    lsock.listen(1)
    seen = []

    def serve():
        conn, _ = lsock.accept()
        with conn, conn.makefile("rwb") as f:
            for line in f:
                req = line.decode().rstrip("\n")
                seen.append(req)
                reply = {"error": "bad"} if req.startswith("fail") else {"ok": True, "id": 7}
                f.write((json.dumps(reply) + "\n").encode())
                f.flush()

    threading.Thread(target=serve, daemon=True).start()
    yield path, seen
    lsock.close()


def test_request_encoding(server):
    path, seen = server
    with ts.Client(path) as c:
        c.stats(window=2, topics=["/a", "/b"])
        assert c.add_rule(node="/n*", dir="sub", persist=True) == 7
    assert seen == ["stats window=2 topic=/a topic=/b", "rule add node=/n* dir=sub persist=1"]


def test_error_reply(server):
    with ts.Client(server[0]) as c, pytest.raises(ts.DaemonError, match="bad"):
        c.request("fail")


def test_rejects_whitespace(server):
    with ts.Client(server[0]) as c, pytest.raises(ValueError):
        c.request("stats", topic="a b")


def test_not_running(tmp_path):
    with pytest.raises(ts.DaemonError, match="not running"):
        ts.Client(str(tmp_path / "none.sock"))


def test_rules_for():
    assert ts.rules_for() == [dict(node=None, topic=None, dir=None)]
    assert len(ts.rules_for(["/a", "/b"], ["/x", "/y", "/z"], "pub")) == 6
