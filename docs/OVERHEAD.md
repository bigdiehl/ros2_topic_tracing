# Tracing Overhead

Raw results, method and reproduction: [bench/README.md](../bench/README.md).

## Where the cost is

| Where | Cost | Scales with |
|---|---|---|
| Traced ROS processes | ~180 ns per message event (~145 ns record + ~40 ns vpid/procname context) | msgs x (1 + subscribers) |
| Untraced ROS processes (not in the vpid tracker) | 0: events never enabled | |
| Name events (meta session, all processes) | a few events per endpoint, once | endpoint creation |
| Disabled tracepoints | function call + branch, not measurable | |
| lttng daemons + trace_daemon, idle | ~2.2% of one core (1.2% daemon, 0.5% relayd) | fixed |
| lttng daemons + trace_daemon, traced traffic | ~15% of one core per 100k events/s | traced events/s |

**The consumer is the cost, not the traced application. Select fewer processes to pay less.**

## Cost model

```
in-app CPU  ~= traced msg/s x (1 + subs_per_topic) x 0.18 us   (spread over traced processes)
tracer CPU  ~= 2.2% core + ~15% core per 100k traced events/s
```

Measured (100 nodes / 10 processes / 300 topics / 2 subs per topic):

| Scenario | Traced events/s | Tracer CPU |
|---|---|---|
| Names only (`trace_ctl.py start`), any rate | 0 | 2.2% |
| 10 Hz, 1 of 10 processes | ~1k | 2.9% |
| 10 Hz, everything | ~9.6k | 5.3% |
| 100 Hz, 1 of 10 processes | ~10k | 4.8% |
| 100 Hz, everything | ~96k | 17.2% |
| 100 Hz, everything, previous design (C++ collector + Python stats) | ~96k | 21.6% |

## Consumer per-event cost

| Stage | µs/event |
|---|---|
| babeltrace2 C API decode (floor) | ~1.0 offline, ~1.4 live |
| trace_daemon count + bucket | ~0.1 |
| (removed) babeltrace text formatting | 1.5 |
| (removed) Python parse + stats | 3.4 |

What's left is babeltrace2 decoding of traced events. Going lower means tracing fewer events: fewer processes, or pub-only (DESIGN.md P4).
