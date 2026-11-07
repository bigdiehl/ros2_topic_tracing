"""Shared code: LTTng session setup and the client for trace_daemon (collector/trace_daemon.cpp).

Two LTTng live sessions, consumed by one trace_daemon:
  ros2mon_meta  rcl_node_init, rcl/rmw publisher/subscription init. ALL processes.
                Names every endpoint; a few events per endpoint, once.
  ros2mon_hot   rmw_publish, rmw_take (taken == 1). vpid tracker, starts empty.
                The daemon tracks processes matching rules (node/topic/proc globs).

rmw handles are used for both directions because rcl_publish's publisher_handle is
not stable for /rosout (rcl copies that publisher struct after init).
"""

import fnmatch
import glob
import itertools
import json
import os
import re
import shutil
import socket
import subprocess
import time

META_EVENTS = [
    "ros2:rcl_node_init",
    "ros2:rcl_publisher_init",
    "ros2:rcl_subscription_init",
    "ros2:rmw_publisher_init",
    "ros2:rmw_subscription_init",
]
META_SESSION = "ros2mon_meta"
HOT_SESSION = "ros2mon_hot"
INTERNAL_TOPICS = re.compile(r"^/rosout$|/parameter_events$|/_action/|^/ros_discovery_info$")

ROOT = os.path.dirname(os.path.abspath(__file__))
LIVE_TIMER_US = 200_000
LAG_MS = int(2 * LIVE_TIMER_US / 1000 + 300)  # live delivery delay allowance
BUCKET_MS = int(os.environ.get("TRACE_STATS_BUCKET_MS", "100"))
# hot channel: per-stream trace files on the relayd side are capped (ring of files)
HOT_SUBBUF, HOT_NUM_SUBBUF, HOT_TRACEFILE_SIZE, HOT_TRACEFILE_COUNT = "256K", 4, 1 << 20, 4


class DaemonError(RuntimeError):
    pass


def socket_path():
    if os.environ.get("ROS2MON_SOCKET"):
        return os.environ["ROS2MON_SOCKET"]
    run = os.environ.get("XDG_RUNTIME_DIR")
    return os.path.join(run, "ros2mon.sock") if run else f"/tmp/ros2mon-{os.getuid()}.sock"


def log_path():
    return os.path.splitext(socket_path())[0] + ".log"


def daemon_path():
    return os.environ.get("TRACE_DAEMON") or os.path.join(ROOT, "collector", "build", "trace_daemon")


# ---------------------------------------------------------------- client

class Client:
    """Connection to trace_daemon. Rules added through it are removed when it closes."""

    def __init__(self, path=None, timeout=5.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(path or socket_path())
        except OSError as e:
            self.sock.close()
            raise DaemonError(f"trace daemon not running ({e.strerror}); run trace_ctl.py start") from e
        self.buf = b""

    def request(self, *words, **params):
        """Send one request; params with list values are repeated (node=a node=b)."""
        parts = list(words)
        for k, v in params.items():
            for x in (v if isinstance(v, (list, tuple)) else [v]):
                if x is None:
                    continue
                x = str(x)
                if not x or any(c.isspace() for c in x):
                    raise ValueError(f"{k}: empty or whitespace in {x!r}")
                parts.append(f"{k}={x}")
        self.sock.sendall((" ".join(parts) + "\n").encode())
        while b"\n" not in self.buf:
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise DaemonError("trace daemon closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        reply = json.loads(line)
        if "error" in reply:
            raise DaemonError(reply["error"])
        return reply

    def stats(self, window=5.0, nodes=None, topics=None, tracked=False):
        return self.request("stats", window=window, node=nodes, topic=topics,
                            tracked=1 if tracked else None)

    def add_rule(self, node=None, topic=None, proc=None, dir=None, persist=False):
        return self.request("rule", "add", node=node, topic=topic, proc=proc, dir=dir,
                            persist=1 if persist else None)["id"]

    def status(self):
        return self.request("status")

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def rules_for(nodes=None, topics=None, direction=None):
    """Rules (kwargs for Client.add_rule) covering every node-glob x topic-glob pair."""
    return [dict(node=n, topic=t, dir=direction)
            for n, t in itertools.product(nodes or [None], topics or [None])]


def filter_rows(rows, nodes=None, topics=None, direction=None, internal=False, tracked=None):
    """Select snapshot endpoint dicts: any node glob AND any topic glob."""
    out = []
    for r in rows:
        if not internal and INTERNAL_TOPICS.search(r["topic"]):
            continue
        if direction and r["dir"] != direction:
            continue
        if tracked is not None and r["tracked"] != tracked:
            continue
        if nodes and not any(fnmatch.fnmatchcase(r["node"], g) for g in nodes):
            continue
        if topics and not any(fnmatch.fnmatchcase(r["topic"], g) for g in topics):
            continue
        out.append(r)
    return out


def load_trace(path, window=None):
    """Offline: stats snapshot of a recorded trace directory (e.g. from `ros2 trace`)."""
    cmd = [daemon_path(), "fs", path, "--bucket-ms", str(BUCKET_MS)]
    if window:
        cmd += ["--window", str(window)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise DaemonError(r.stderr.strip() or f"trace_daemon exited {r.returncode}")
    return json.loads(r.stdout)


# ---------------------------------------------------------------- lttng sessions

def _run(cmd, check=True):
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {r.stderr.strip()}")
    return r


def ensure_lttng_daemons():
    if _run(["lttng", "list"], check=False).returncode != 0:
        _run(["lttng-sessiond", "--daemonize"])
    if subprocess.run(["pgrep", "-u", str(os.getuid()), "-x", "lttng-relayd"],
                      stdout=subprocess.DEVNULL).returncode != 0:
        _run(["lttng-relayd", "--daemonize"])


def session_exists(name):
    r = subprocess.run(["lttng", "list"], capture_output=True, text=True)
    return re.search(rf"^\s*\d+\) {re.escape(name)} ", r.stdout, re.M) is not None


def live_url(name):
    return f"net://localhost/host/{socket.gethostname()}/{name}"


def create_sessions():
    """Create (not start) both sessions."""
    t = f"--live={LIVE_TIMER_US}"
    m, h = META_SESSION, HOT_SESSION
    _run(["lttng", "create", m, t])
    _run(["lttng", "enable-event", "-u", "-s", m, ",".join(META_EVENTS)])
    _run(["lttng", "add-context", "-u", "-s", m, "-t", "vpid", "-t", "procname"])

    _run(["lttng", "create", h, t])
    _run(["lttng", "enable-channel", "-u", "-s", h, "hot", f"--subbuf-size={HOT_SUBBUF}",
          f"--num-subbuf={HOT_NUM_SUBBUF}", f"--tracefile-size={HOT_TRACEFILE_SIZE}",
          f"--tracefile-count={HOT_TRACEFILE_COUNT}"])
    _run(["lttng", "enable-event", "-u", "-s", h, "-c", "hot", "ros2:rmw_publish"])
    # drop empty takes (taken=0) inside the traced process
    _run(["lttng", "enable-event", "-u", "-s", h, "-c", "hot", "ros2:rmw_take",
          "--filter", "taken == 1"])
    _run(["lttng", "add-context", "-u", "-s", h, "-c", "hot", "-t", "vpid"])
    _run(["lttng", "untrack", "-u", "-s", h, "--vpid", "--all"])


def start_sessions():
    for s in (META_SESSION, HOT_SESSION):
        _run(["lttng", "start", s])


def destroy_sessions():
    """Destroy both sessions and delete their relayd output. Returns names destroyed."""
    done = []
    for s in (META_SESSION, HOT_SESSION):
        if session_exists(s):
            _run(["lttng", "destroy", s], check=False)
            done.append(s)
        base = os.path.expanduser(os.path.join("~/lttng-traces", socket.gethostname()))
        for d in glob.glob(os.path.join(base, f"{s}-[0-9]*-[0-9]*")):
            shutil.rmtree(d, ignore_errors=True)
    return done


def launch_daemon():
    """Start trace_daemon detached; wait until its socket answers."""
    exe = daemon_path()
    if not os.access(exe, os.X_OK):
        raise DaemonError(f"{exe} not found; build it: cmake -S collector -B collector/build "
                          f"&& make -C collector/build")
    cmd = [exe, "live", "--meta", live_url(META_SESSION), "--hot", live_url(HOT_SESSION),
           "--hot-session", HOT_SESSION, "--socket", socket_path(),
           "--lag-ms", str(LAG_MS), "--bucket-ms", str(BUCKET_MS)]
    with open(log_path(), "a") as log:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise DaemonError(f"trace_daemon exited ({proc.returncode}), see {log_path()}")
        try:
            Client(timeout=1).close()
            return proc.pid
        except DaemonError:
            time.sleep(0.05)
    proc.terminate()
    raise DaemonError(f"trace_daemon did not come up, see {log_path()}")
