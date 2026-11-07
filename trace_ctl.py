#!/usr/bin/env python3
"""Start/stop/configure the ROS 2 pub/sub trace daemon.

Run `start` BEFORE launching the stack: node/topic names are only recorded when a
node is created. Message events are traced only for selected processes (rules).

  trace_ctl.py start                     names only; diagnostics/CLI select processes later
  trace_ctl.py start --all               record everything from the start
  trace_ctl.py start -n '/robot1/*'      always trace processes hosting matching nodes
  trace_ctl.py status
  trace_ctl.py mode all | selected       switch between everything and rule-selected
  trace_ctl.py track -t '/scan*'         add a persistent rule
  trace_ctl.py untrack 3 | --all         remove persistent rule(s)
  trace_ctl.py stop
"""

import argparse
import json
import sys
import time

import trace_stats as ts


def add_rule_args(p):
    p.add_argument("-n", "--node", action="append", help="node name glob (repeatable)")
    p.add_argument("-t", "--topic", action="append", help="topic name glob (repeatable)")
    p.add_argument("-p", "--proc", action="append", help="process name glob (repeatable)")
    p.add_argument("--dir", choices=["pub", "sub"], help="only endpoints of this direction")
    p.add_argument("--all", action="store_true", help="trace every process")


def persistent_rules(c, args):
    if args.all:
        return [c.add_rule(persist=True)]
    if not (args.node or args.topic or args.proc):
        return []
    ids = []
    for r in ts.rules_for(args.node, args.topic, args.dir):
        for p in args.proc or [None]:
            ids.append(c.add_rule(proc=p, persist=True, **r))
    return ids


def running():
    try:
        ts.Client(timeout=1).close()
        return True
    except ts.DaemonError:
        return False


def cmd_start(args):
    if running():
        sys.exit("trace daemon already running (trace_ctl.py status)")
    ts.ensure_lttng_daemons()
    stale = ts.destroy_sessions()
    if stale:
        print(f"removed stale sessions: {', '.join(stale)}", file=sys.stderr)
    ts.create_sessions()
    try:
        pid = ts.launch_daemon()
        ts.start_sessions()
        time.sleep(args.settle)  # let the live viewer attach before nodes start
        with ts.Client() as c:
            ids = persistent_rules(c, args)
    except Exception:
        ts.destroy_sessions()
        raise
    print(f"trace daemon pid {pid}, socket {ts.socket_path()}, log {ts.log_path()}")
    print(f"sessions {ts.META_SESSION} (names, all processes), {ts.HOT_SESSION} "
          f"(messages, {'all processes' if args.all else f'{len(ids)} rule(s)'})")


def cmd_stop(args):
    try:
        with ts.Client(timeout=2) as c:
            c.request("shutdown")
        stopped = True
    except ts.DaemonError:
        stopped = False
    time.sleep(0.2)
    gone = ts.destroy_sessions()
    print(f"daemon {'stopped' if stopped else 'not running'}; "
          f"sessions destroyed: {', '.join(gone) or 'none'}")


def cmd_status(args):
    try:
        with ts.Client() as c:
            st = c.status()
    except ts.DaemonError as e:
        sessions = [s for s in (ts.META_SESSION, ts.HOT_SESSION) if ts.session_exists(s)]
        sys.exit(f"{e}" + (f"\nstale sessions: {', '.join(sessions)} (trace_ctl.py stop)"
                           if sessions else ""))
    if args.format == "json":
        print(json.dumps(st, indent=2))
        return
    print(f"daemon pid {st['pid']}, up {st['uptime']:.0f} s, mode {st['mode']}"
          f"{'' if st['applied'] else ' (tracker update pending)'}")
    print(f"processes {st['procs']} known, {len(st['traced'])} traced; endpoints {st['endpoints']}; "
          f"events {st['events']} (dropped {st['dropped']}, discarded {st['discarded']}, "
          f"meta discarded {st['discarded_meta']}); clients {st['clients']}")
    print("\nRULES  id  owner       node / topic / proc / dir")
    for r in st["rules"] or []:
        owner = "persistent" if r["persist"] else f"client {r['owner']}"
        print(f"     {r['id']:>4}  {owner:<10}  {r['node']} / {r['topic']} / {r['proc']} / {r['dir']}")
    if not st["rules"]:
        print("     (none: names only)")
    print("\nTRACED  pid       proc              since")
    for p in sorted(st["traced"], key=lambda p: p["pid"]):
        print(f"        {p['pid']:<9} {p['proc']:<17} {p['since']:.0f} s")


def cmd_track(args):
    with ts.Client() as c:
        ids = persistent_rules(c, args)
    if not ids:
        sys.exit("give -n/-t/-p or --all")
    print(f"added persistent rule(s): {', '.join(map(str, ids))}")


def cmd_untrack(args):
    with ts.Client() as c:
        if args.all:
            c.request("rule", "clear")
            print("removed all persistent rules")
        for i in args.ids:
            c.request("rule", "del", id=i)
            print(f"removed rule {i}")


def cmd_mode(args):
    with ts.Client() as c:
        rules = c.status()["rules"]
        is_all = [r for r in rules if r["persist"] and
                  (r["node"], r["topic"], r["proc"], r["dir"]) == ("*", "*", "*", "*")]
        if args.mode == "all" and not is_all:
            c.add_rule(persist=True)
        elif args.mode == "selected":
            for r in is_all:
                c.request("rule", "del", id=r["id"])
        print(f"mode {c.status()['mode']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start", help="create sessions and start the daemon")
    add_rule_args(p)
    p.add_argument("--settle", type=float, default=1.0, help="wait for the viewer to attach [s]")
    p.set_defaults(fn=cmd_start)
    sub.add_parser("stop", help="stop the daemon, destroy sessions").set_defaults(fn=cmd_stop)
    p = sub.add_parser("status", help="rules, traced processes, counters")
    p.add_argument("-f", "--format", choices=["text", "json"], default="text")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("track", help="add a persistent rule")
    add_rule_args(p)
    p.set_defaults(fn=cmd_track)
    p = sub.add_parser("untrack", help="remove persistent rules")
    p.add_argument("ids", nargs="*", type=int, help="rule ids (see status)")
    p.add_argument("--all", action="store_true", help="all persistent rules")
    p.set_defaults(fn=cmd_untrack)
    p = sub.add_parser("mode", help="all = trace everything; selected = rules only")
    p.add_argument("mode", choices=["all", "selected"])
    p.set_defaults(fn=cmd_mode)
    args = ap.parse_args()
    try:
        args.fn(args)
    except ts.DaemonError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
