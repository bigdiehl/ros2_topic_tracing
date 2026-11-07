#!/usr/bin/env python3
"""Measure tracing overhead on a synthetic ROS 2 system.

Launches PROCS processes hosting NODES nodes in total (TOPICS topics per node, SUBS
subscribers per topic, RATE Hz) under each tracing mode, and reports CPU used by
the ROS processes and by the tracing pipeline (lttng daemons, trace_daemon).

  ./run_overhead.py --rate 10 --rate 100

Modes: off | disk (monitor events to disk, no consumer) | disk-all (all ros2:*) |
       names (trace_ctl start, no rules) | sel1 (1 of PROCS processes selected) |
       all (trace_ctl start --all)
"""

import argparse
import os
import shutil
import subprocess
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.join(HERE, "build", "pubsub_bench")
TRACE_CTL = os.path.join(HERE, "..", "trace_ctl.py")
TICK = os.sysconf("SC_CLK_TCK")
EVENTS = ("ros2:rcl_node_init,ros2:rcl_publisher_init,ros2:rcl_subscription_init,"
          "ros2:rmw_publisher_init,ros2:rmw_subscription_init,ros2:rmw_publish,ros2:rmw_take")


def cpu_seconds(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().rsplit(")", 1)[1].split()
        return (int(parts[11]) + int(parts[12])) / TICK  # utime + stime
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return 0.0


def pids_named(*names):
    out = subprocess.run(["pgrep", "-u", str(os.getuid()), "-x", "|".join(names)],
                         capture_output=True, text=True).stdout
    return [int(p) for p in out.split()]


def lttng(*args):
    return subprocess.run(["lttng", *args], capture_output=True, text=True)


def run_mode(mode, a, rate):
    session, tmp, daemon = "ovh", None, False
    if mode in ("disk", "disk-all"):
        tmp = tempfile.mkdtemp(dir=a.scratch)
        lttng("create", session, f"--output={tmp}")
        lttng("enable-event", "-u", EVENTS if mode == "disk" else "ros2:*")
        lttng("add-context", "-u", "-t", "vpid", "-t", "procname")
        lttng("start", session)
    elif mode in ("names", "sel1", "all"):
        extra = {"names": [], "sel1": ["-n", "/bench_[0-9]"], "all": ["--all"]}[mode]
        subprocess.run(["python3", TRACE_CTL, "start", *extra], check=True,
                       stdout=subprocess.DEVNULL)
        daemon = True

    per = a.nodes // a.procs
    procs = [subprocess.Popen(
        [BENCH, "load", str(i * per), str(per), str(a.nodes), str(a.topics), str(a.subs),
         str(rate), str(a.duration), str(a.payload)],
        stdout=subprocess.PIPE, text=True) for i in range(a.procs)]

    time.sleep(a.warmup)
    tracer = pids_named("lttng-sessiond", "lttng-consumerd", "lttng-relayd")
    if daemon:
        tracer += pids_named("trace_daemon")
    t0 = time.monotonic()
    ros0 = sum(cpu_seconds(p.pid) for p in procs)
    tr0 = {p: cpu_seconds(p) for p in tracer}
    time.sleep(a.window)
    dt = time.monotonic() - t0
    ros1 = sum(cpu_seconds(p.pid) for p in procs)
    tr1 = {p: cpu_seconds(p) for p in tracer}

    sent = recv = 0
    for p in procs:
        out = p.communicate()[0].split()
        sent += int(out[1])
        recv += int(out[3])

    if mode in ("disk", "disk-all"):
        lttng("stop", session)
        lttng("destroy", session)
        shutil.rmtree(tmp, ignore_errors=True)
    if daemon:
        subprocess.run(["python3", TRACE_CTL, "stop"], stdout=subprocess.DEVNULL)
    return {
        "ros_cpu": 100 * (ros1 - ros0) / dt,
        "tracer_cpu": 100 * sum(tr1[p] - tr0[p] for p in tracer) / dt,
        "sent": sent, "recv": recv,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nodes", type=int, default=100)
    ap.add_argument("--procs", type=int, default=10)
    ap.add_argument("--topics", type=int, default=3, help="topics per node")
    ap.add_argument("--subs", type=int, default=2, help="subscribers per topic")
    ap.add_argument("--rate", type=float, action="append", help="Hz, repeatable")
    ap.add_argument("--payload", type=int, default=64)
    ap.add_argument("--duration", type=float, default=25)
    ap.add_argument("--warmup", type=float, default=8)
    ap.add_argument("--window", type=float, default=12)
    ap.add_argument("--modes", default="off,names,sel1,all")
    ap.add_argument("--scratch", default=tempfile.gettempdir())
    a = ap.parse_args()
    rates = a.rate or [10.0]

    print(f"{a.nodes} nodes / {a.procs} procs, {a.nodes * a.topics} topics, "
          f"{a.subs} subs/topic, payload {a.payload} B, {os.cpu_count()} CPUs. CPU in % of one core.")
    print(f"{'rate':>6} {'mode':<9} {'msg/s pub':>10} {'ROS cpu%':>9} {'tracer cpu%':>12} {'delivered':>10}")
    for rate in rates:
        for mode in a.modes.split(","):
            r = run_mode(mode, a, rate)
            exp = r["sent"] * a.subs
            print(f"{rate:>6g} {mode:<9} {a.nodes * a.topics * rate:>10g} {r['ros_cpu']:>9.1f} "
                  f"{r['tracer_cpu']:>12.1f} {100 * r['recv'] / max(exp, 1):>9.1f}%", flush=True)


if __name__ == "__main__":
    main()
