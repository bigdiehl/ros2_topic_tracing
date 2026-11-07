# Benchmarks

Results: 2026-11-06, Ryzen 7 3700X (8C/16T), Ubuntu 24.04, Jazzy, rmw_fastrtps_cpp, LTTng 2.13.11, babeltrace 1.5.11. Background ROS processes were running (about 0.1 core), so expect noise of about ±10%.

| File | Purpose |
|---|---|
| `pubsub_bench.cpp` | `load`: N nodes x T topics x S subs at R Hz. `tight`: publish loop, ns/publish |
| `run_overhead.py` | Runs `load` under each tracing mode (off, disk, names, sel1, all), measures per-process CPU |
| `parse_bench.cpp` | C++ equivalent of the Python hot-path parser (sized the C++ consumer) |

## Run

```bash
source /opt/ros/jazzy/setup.bash
cmake -S bench -B bench/build && make -C bench/build -j
bench/run_overhead.py --rate 10 --rate 100              # modes: off,disk,disk-all,names,sel1,all
bench/build/pubsub_bench tight 1000000 64               # run with/without an lttng session
babeltrace --clock-seconds TRACE > t.txt && bench/build/parse_bench < t.txt
```

## 0. trace_daemon (current design)

Same load as section 1. Tracer = lttng daemons + trace_daemon, % of one core.

| Rate | Mode | ROS | Tracer | Delivered |
|---|---|---|---|---|
| 100 Hz | off | 395-412 | 0 | 99.8% |
| | disk (no consumer) | 408 | 0.5 | 99.8% |
| | names (`trace_ctl.py start`, no rules) | 398 | 2.2 | 99.8% |
| | sel1 (1 of 10 processes, `-n '/bench_[0-9]'`) | 394 | 4.8 | 99.8% |
| | all (`--all`) | 403 | **17.2** | 99.8% |
| 10 Hz | off | 61 | 0.1 | 100% |
| | names | 59 | 2.2 | 99.9% |
| | sel1 | 61 | 2.9 | 100% |
| | all | 61 | 5.3 | 100% |

Breakdown, all at 100 Hz: trace_daemon 13%, relayd 2%, consumerd 1%. Idle: trace_daemon 1.2%, relayd 0.5%.

Before the idle backoff (DESIGN.md D21), all at 100 Hz was 38%: relayd 15%, daemon 24%. Polling the idle meta graph on every pass while the hot graph was busy sent relayd a request per stream each time. Hot channel config (default vs 256K x 4 subbufs, with or without the 1 MiB x 4 tracefile cap) made no difference.

## 1. System load: 100 nodes / 10 processes / 300 topics / 2 subs per topic, 64 B

CPU in % of one core. ROS = the 10 load processes. Tracer = lttng daemons + babeltrace + topic_stats.

| Rate | Events/s | Mode | ROS | Tracer | Delivered |
|---|---|---|---|---|---|
| 10 Hz | ~9.6k | off | 59-64 | 0 | 99.9% |
| | | disk, monitor events | 59 | 0.1 | 99.9% |
| | | disk, all `ros2:*` | 60 | 0.2 | 99.9% |
| | | live + topic_stats | 59 | 14-16 | 99.9% |
| 100 Hz | ~96k | off | 375-405 | 0 | 99.8% |
| | | disk, monitor events | 415 | 0.4 | 99.9% |
| | | disk, all `ros2:*` | 441 | 2.1 | 99.8% |
| | | live + topic_stats (generic parser) | 405 | 104 | 99.8% |
| | | live + topic_stats (hot-path parser) | 354 | 90 | 99.8% |

Previous design (C++ collector + Python stats, text fallback), separate run on the same machine:

| Rate | Mode | ROS | Tracer | Delivered |
|---|---|---|---|---|
| 10 Hz | off | 80 | 0 | 100% |
| | live-text | 65 | 18.4 | 99.8% |
| | live-cpp | 64 | 10.3 | 99.7% |
| 100 Hz | off | 337 | 0 | 99.8% |
| | live-text | 341 | 91.1 | 99.7% |
| | **live-cpp** | 340 | **21.6** | 99.6% |

Live-cpp breakdown (% of one core):

| Rate, bucket | collector (C++) | Python | relayd | consumerd |
|---|---|---|---|---|
| 100 Hz, 100 ms | 14.5 | 6.4 | 1.9 | 0.8 |
| 100 Hz, 500 ms | 12.9 | 3.3 | 2.0 | 0.8 |
| 10 Hz, 500 ms | 2.9 | 2.8 | 1.8 | 0.7 |
| idle (no load) | 0.7 (50 ms poll), 1.2 (10 ms poll) | | | |

Live tracer breakdown at 100 Hz: babeltrace 39%, Python 49%, relayd 1.8%, consumerd 0.9%, sessiond 0.2%. Trace size about 5 MB/s.

## 2. Microbenchmark: ns per publish (1 publisher, no subscribers, best of 3-5)

| Session config | ns | Delta |
|---|---|---|
| no session | 688-699 | |
| `rmw_publish`, no context | 842 | +145 |
| + vpid | 870 | +172 |
| + vpid + procname | 881 | +183 |
| monitor event set | 894 | +195 |
| all `ros2:*` (4 events per publish) | 1131 | +432 |
| filter `handle == X`, no match | 758 | +70 |
| filter of 20 OR'd handles, no match | 854 | +165 |

No discarded events at about 1M events/s with the default channel.

## 3. Consumer: per-event cost by stage (offline, 956k events, 227 MB text)

| Stage | Time | µs/event | Events/s |
|---|---|---|---|
| babeltrace CTF decode (`-o dummy`) | 0.50 s | 0.53 | 1.9M |
| babeltrace decode + text | 1.94 s | 2.0 | 490k |
| Python generic parser | 6.2 s | 6.5 | 154k |
| Python hot-path parser (`HOT_RE`) | 3.3 s | 3.4 | 290-308k |
| C++ hot-path parser (`parse_bench`) | 0.40 s | 0.42 | 2.4M |
| babeltrace2 C API decode + muxer, no handler | 0.97 s | 1.0 | 990k |
| collector `fs` mode (decode + aggregate) | 1.05 s | 1.1 | 910k |
| `topic_stats --trace`, cpp vs text backend | 1.15 s vs 3.78 s | | |
| babeltrace `--no-delta` | -6% | | |

## 4. Consumer cost at 96k events/s: projected vs measured

Offline µs/event x 96k. Live mode measured about 1.7x the offline figure (relayd protocol, polling, pipe).

| Pipeline | µs/event | Offline-equivalent | Live estimate |
|---|---|---|---|
| babeltrace text -> Python (current) | 5.4 | 52% | 90% (measured) |
| babeltrace text -> C++ parser -> Python (aggregates only) | 2.4 | 23% | ~40% |
| C++ consumer on the babeltrace2 C API (no text) | ~1.1 | ~11% | **21.6% measured** (collector 14.5%) |

The projection assumed babeltrace1's decode speed (0.5 µs). babeltrace2's decode is about 1 µs/event, which is now the floor.

## Conclusions

- In-process overhead is about 180 ns per event and can't be told apart from noise at 30k msg/s. The tooling language doesn't affect it.
- The consumer is the cost. Text formatting (1.5 µs) and Python parsing (3.4 µs) dominate; decoding is only 0.5 µs.
- C++ helps only in the consumer hot path: text backend 91% -> C++ collector + Python 21.6% -> trace_daemon 17.2% of a core at 96k events/s.
- Remaining floor: babeltrace2 decoding (~1 µs/event). Process selection (section 0) is the real lever: 1 of 10 processes traced costs 4.8%, names only 2.2%.
