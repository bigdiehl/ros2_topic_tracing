#!/usr/bin/env python3
"""Dump ROS 2 pub/sub statistics for all or selected nodes/topics (needs trace_ctl.py start).

  topic_stats.py                          traced endpoints, counts since tracing began (instant)
  topic_stats.py -s 5                     trace every process for 5 s, then print
  topic_stats.py -s 5 -n '/robot1/*'      trace only matching processes for 5 s
  topic_stats.py -t '*scan*' --known      also list matching endpoints that are not traced
  topic_stats.py -w [-s 3]                refresh until Ctrl-C (with -s: keep tracing the selection)
  topic_stats.py --trace ~/.ros/tracing/foo -f json     offline, a recorded trace

SINCE: how long the endpoint has been counted; "start" = since it was created.
"""

import argparse
import csv
import json
import sys
import time

import trace_stats as ts

COLUMNS = [("topic", 32), ("dir", 4), ("node", 28), ("pid", 8), ("count", 9),
           ("rate", 8), ("avg_rate", 8), ("last_age", 8), ("since", 7)]


def cell(r, name, w):
    v = r.get(name)
    if name == "since" and r.get("from_start"):
        v = "start"
    elif v is None:
        v = "-"
    elif name == "since":
        v = f"{v:.0f}s"
    elif isinstance(v, float):
        v = f"{v:.1f}"
    return f"{v:<{w}}" if name in ("topic", "dir", "node") else f"{v:>{w}}"


def fmt_table(snap, rows, args, hidden):
    hdr = " ".join(f"{n.upper():<{w}}" if n in ("topic", "dir", "node") else f"{n.upper():>{w}}"
                   for n, w in COLUMNS)
    out = [hdr, "-" * len(hdr)] + [" ".join(cell(r, n, w) for n, w in COLUMNS) for r in rows]
    if not rows:
        out.append("(no traced endpoints match; -s 5 samples every process for 5 s)"
                   if hidden else "(no matching endpoints)")
    unt = snap["untracked"]
    if unt and not args.no_untracked:
        out.append("\nUnnamed endpoints (process started before trace_ctl.py start):")
        out += [f"  {u['proc']:<20} pid={u['pid']:<8} {u['dir']}  count={u['count']}" for u in unt]
    out.append(f"\nmode {snap['mode']}  traced procs {snap['traced_procs']}/{snap['procs']}  "
               f"window {snap['window']:.1f} s  events {snap['events']}  "
               f"rmw-internal msgs {snap['internal_msgs']}  "
               f"unnamed msgs {sum(u['count'] for u in unt)}  discarded {snap['discarded']}")
    if hidden:
        out.append(f"{hidden} matching endpoint(s) not traced (--known lists them)")
    return "\n".join(out)


def emit(snap, args, clear=False):
    sel = dict(nodes=args.node, topics=args.topic, direction=args.dir, internal=args.all)
    rows = ts.filter_rows(snap["endpoints"], **sel)
    hidden = 0
    if not args.known:
        hidden = sum(1 for r in rows if not r["tracked"])
        rows = [r for r in rows if r["tracked"]]
    order = ("node", "topic", "dir") if args.group_by == "node" else ("topic", "dir", "node")
    rows.sort(key=lambda r: tuple(r[k] for k in order))
    if args.format == "json":
        print(json.dumps(dict(snap, endpoints=rows), indent=2), flush=True)
    elif args.format == "csv":
        w = csv.DictWriter(sys.stdout, fieldnames=[c for c, _ in COLUMNS] +
                           ["from_start", "tracked", "proc", "depth"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        sys.stdout.flush()
    else:
        if clear and sys.stdout.isatty():
            sys.stdout.write("\033[H\033[J")
        print(fmt_table(snap, rows, args, hidden), flush=True)


def live(args):
    with ts.Client() as c:
        window = args.window or args.sample or 5.0
        if args.sample:
            for r in ts.rules_for(args.node, args.topic, args.dir):
                c.add_rule(**r)  # removed by the daemon when this connection closes
            print(f"sampling {args.sample:g} s ...", file=sys.stderr)
            time.sleep(args.sample + ts.LAG_MS / 1000 + 0.3)
        while True:
            emit(c.stats(window=window, nodes=args.node, topics=args.topic), args,
                 clear=args.watch)
            if not args.watch:
                return
            time.sleep(args.interval)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_argument_group("source")
    src.add_argument("-s", "--sample", type=float, metavar="SEC",
                     help="trace matching processes (default: all) for SEC seconds, then print")
    src.add_argument("--trace", metavar="DIR", help="analyze a recorded trace instead")
    flt = ap.add_argument_group("filters (globs, repeatable, OR-ed; also select what -s traces)")
    flt.add_argument("-n", "--node", action="append", help="node name glob, e.g. '/robot1/*'")
    flt.add_argument("-t", "--topic", action="append", help="topic name glob, e.g. '/scan*'")
    flt.add_argument("--dir", choices=["pub", "sub"], help="only publishers or subscribers")
    flt.add_argument("-a", "--all", action="store_true", help="include /rosout, /parameter_events, ...")
    flt.add_argument("--known", action="store_true", help="also list endpoints that are not traced")
    out = ap.add_argument_group("output")
    out.add_argument("-w", "--watch", action="store_true", help="refresh until Ctrl-C")
    out.add_argument("--interval", type=float, default=1.0, help="watch period [s]")
    out.add_argument("--window", type=float, help="rate window [s] (default: -s value or 5)")
    out.add_argument("-f", "--format", choices=["table", "json", "csv"], default="table")
    out.add_argument("-g", "--group-by", choices=["topic", "node"], default="topic")
    out.add_argument("--no-untracked", action="store_true", help="hide unnamed endpoint list")
    args = ap.parse_args()

    try:
        if args.trace:
            args.known = True
            return emit(ts.load_trace(args.trace, args.window), args)
        live(args)
    except ts.DaemonError as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
