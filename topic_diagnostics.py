#!/usr/bin/env python3
"""ROS 2 node: publish /diagnostics for topic activity measured with LTTng tracing.

Needs the trace daemon (trace_ctl start, before the stack). Can start at any time:
it asks the daemon to trace the processes its checks match, for as long as it runs.

  python3 topic_diagnostics.py --config demo/diagnostics.yaml

One DiagnosticStatus per check, published every `period` seconds:
  pub check  rate summed over all matching publishers
             ERROR: no publisher found | no messages in window | rate out of range
  sub check  each matching subscription evaluated on its own (worst one wins)
             ERROR: no subscription found | nothing received | rate out of range
  STALE      during the first `window` seconds after start, or while tracing starts
Plus one "tracer" status: ERROR if the daemon is unreachable, WARN on discarded events.

Config (YAML):
  period: 1.0            # publish period [s]
  window: 5.0            # rate window [s]
  checks:
    - name: chatter      # optional, defaults to "<dir> <topic>"
      topic: /chatter    # glob
      dir: pub           # pub | sub
      node: /talker      # optional node glob
      min_rate: 0.5      # optional [Hz]; default: any activity (> 0)
      max_rate: 20       # optional [Hz]
"""

import argparse
import signal
import sys
import time

import rclpy
import yaml
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node

import trace_stats as ts

OK, WARN, ERROR, STALE = (DiagnosticStatus.OK, DiagnosticStatus.WARN,
                          DiagnosticStatus.ERROR, DiagnosticStatus.STALE)


def kv(k, v):
    return KeyValue(key=str(k), value=str(v))


class Check:
    def __init__(self, cfg):
        self.topic = cfg["topic"]
        self.dir = cfg.get("dir", "pub")
        if self.dir not in ("pub", "sub"):
            raise ValueError(f"check {self.topic}: dir must be pub or sub")
        self.node = cfg.get("node")
        self.min_rate = cfg.get("min_rate")
        self.max_rate = cfg.get("max_rate")
        self.name = cfg.get("name", f"{self.dir} {self.topic}")

    def rule(self):
        return dict(node=self.node, topic=self.topic, dir=self.dir)

    def _range_error(self, rate):
        if self.min_rate is not None and rate < self.min_rate:
            return f"rate {rate:.2f} Hz < min {self.min_rate}"
        if self.max_rate is not None and rate > self.max_rate:
            return f"rate {rate:.2f} Hz > max {self.max_rate}"
        if self.min_rate is None and rate <= 0:
            return "no messages"
        return None

    def status(self, level, message, values=()):
        st = DiagnosticStatus(name=self.name, hardware_id=self.topic, level=level, message=message)
        st.values = [kv("topic", self.topic), kv("dir", self.dir),
                     kv("node_filter", self.node or "*"), kv("min_rate", self.min_rate),
                     kv("max_rate", self.max_rate)] + list(values)
        return st

    def evaluate(self, rows):
        eps = ts.filter_rows(rows, nodes=[self.node] if self.node else None, topics=[self.topic],
                             direction=self.dir, internal=True)
        n = kv("endpoints", len(eps))
        if not eps:
            return self.status(ERROR, f"no {'publisher' if self.dir == 'pub' else 'subscription'} found", [n])
        pending = [e for e in eps if not e["tracked"]]
        if pending:
            return self.status(STALE, f"tracing starting for {len(pending)} endpoint(s)", [n])

        if self.dir == "pub":
            rate = sum(e["rate"] for e in eps)
            ages = [e["last_age"] for e in eps if e["last_age"] is not None]
            vals = [n, kv("rate", f"{rate:.3f}"),
                    kv("publishers", ", ".join(sorted({e["node"] for e in eps}))),
                    kv("last_age", f"{min(ages):.2f}" if ages else "never")]
            err = self._range_error(rate)
            return self.status(ERROR, err, vals) if err else self.status(OK, f"{rate:.2f} Hz", vals)

        vals, bad = [n], []
        for e in sorted(eps, key=lambda e: e["node"]):
            vals.append(kv(f"rate[{e['node']}]", f"{e['rate']:.3f}"))
            err = self._range_error(e["rate"])
            if err:
                bad.append(f"{e['node']}: {err}")
        if bad:
            return self.status(ERROR, "; ".join(bad), vals)
        return self.status(OK, f"{len(eps)} subscription(s) receiving", vals)


class TopicDiagnostics(Node):
    def __init__(self, cfg):
        super().__init__("topic_diagnostics")
        self.period = float(cfg.get("period", 1.0))
        self.window = float(cfg.get("window", 5.0))
        self.checks = [Check(c) for c in cfg.get("checks", [])]
        self.topics = sorted({c.topic for c in self.checks})
        self.client = None
        self.since = None       # monotonic time rules were registered
        self.discarded = None   # last seen counters, for per-period deltas
        self.pub = self.create_publisher(DiagnosticArray, "/diagnostics", 10)
        self.create_timer(self.period, self.tick)
        self.get_logger().info(f"{len(self.checks)} checks, trace daemon {ts.socket_path()}")

    def connect(self):
        """(Re)connect and register one rule per check; rules die with the connection."""
        if self.client:
            return self.client
        c = ts.Client(timeout=2)
        try:
            for chk in self.checks:
                c.add_rule(**chk.rule())
        except Exception:
            c.close()
            raise
        self.client, self.since, self.discarded = c, time.monotonic(), None
        self.get_logger().info(f"connected to trace daemon, {len(self.checks)} rule(s) registered")
        return c

    def tick(self):
        msg = DiagnosticArray()
        msg.header.stamp = self.get_clock().now().to_msg()
        tr = DiagnosticStatus(name="topic_diagnostics: tracer", hardware_id="lttng")
        try:
            snap = self.connect().stats(window=self.window, topics=self.topics)
        except (ts.DaemonError, OSError, ValueError) as e:
            if self.client:
                self.client.close()
            self.client = None
            msg.status = [c.status(ERROR, "trace daemon unreachable") for c in self.checks]
            tr.level, tr.message = ERROR, str(e)
            msg.status.append(tr)
            self.pub.publish(msg)
            return

        warming = time.monotonic() - self.since < self.window
        for c in self.checks:
            st = c.evaluate(snap["endpoints"])
            if warming and st.level != STALE:
                st.level, st.message = STALE, f"warming up ({st.message})"
            msg.status.append(st)

        disc = (snap["discarded"], snap["discarded_meta"])
        prev = self.discarded or disc
        new = tuple(a - b for a, b in zip(disc, prev, strict=True))
        self.discarded = disc
        unnamed = sorted({f"{u['proc']}({u['pid']})" for u in snap["untracked"]})
        tr.values = [kv("mode", snap["mode"]), kv("traced_procs", snap["traced_procs"]),
                     kv("events", snap["events"]), kv("discarded", disc[0]),
                     kv("discarded_meta", disc[1])]
        if new[1]:
            tr.level, tr.message = WARN, f"{new[1]} name events discarded (endpoints may be unnamed)"
        elif new[0]:
            tr.level, tr.message = WARN, f"{new[0]} trace events discarded (rates under-reported)"
        elif unnamed:
            tr.level, tr.message = WARN, "unnamed processes (started before tracing): " + ", ".join(unnamed)
        else:
            tr.level, tr.message = OK, "ok"
        msg.status.append(tr)
        self.pub.publish(msg)


def main():
    argv = rclpy.utilities.remove_ros_args(sys.argv)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="YAML config with checks")
    args = ap.parse_args(argv[1:])
    with open(args.config) as f:
        cfg = yaml.safe_load(f) or {}

    rclpy.init()
    node = TopicDiagnostics(cfg)
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)   # launch may send a second SIGINT
        if node.client:
            node.client.close()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
