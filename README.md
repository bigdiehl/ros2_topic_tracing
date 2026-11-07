# ROS 2 Topic Monitoring via ros2_tracing

Per-node, per-topic pub/sub counts and rates from the `ros2:*` LTTng tracepoints already compiled into Jazzy's rcl/rclcpp/rmw. Monitored nodes need no code changes or rebuilds.

| File | Purpose |
|---|---|
| `trace_ctl.py` | Start/stop tracing, choose which processes are traced |
| `topic_stats.py` | CLI: stats for all or selected nodes/topics (table/json/csv, live or offline) |
| `topic_diagnostics.py` | ROS node: publishes `/diagnostics`, ERROR when topics are missing, silent, or off-rate |
| `trace_stats.py` | Shared Python: LTTng session setup, daemon client |
| `collector/` | `trace_daemon` (C++, babeltrace2 C API): consumes the trace, counts, answers queries |
| `demo/` | Dummy multi-robot system with injected faults |
| `bench/` | Overhead benchmark |
| `test/`, `.github/` | pytest suite, CI |
| `docs/`, `working/` | Design record; ideas and proposals |

## Install

```bash
sudo apt install lttng-tools libbabeltrace2-dev cmake g++ pkg-config python3-yaml
cmake -S collector -B collector/build && make -C collector/build
```

ROS 2 Jazzy from apt (its rcl/rmw are built with tracepoints). The user must be able to run `lttng-sessiond` (on its own, or in the `tracing` group).

## Quick start

```bash
source /opt/ros/jazzy/setup.bash
./trace_ctl.py start                         # BEFORE the stack: names are recorded at node creation
ros2 launch demo/demo.launch.py              # 2 robots + talker/listener + topic_diagnostics
./topic_stats.py -s 5                        # any time: sample everything for 5 s
ros2 topic echo /diagnostics
./trace_ctl.py stop
```

## How tracing is scoped

Names (node/topic/process) are always recorded for every process; that's a few events per endpoint, once. Message events are recorded **only for processes selected by rules**. Untraced processes pay nothing.

| Who | Rules | Lifetime |
|---|---|---|
| `topic_diagnostics.py` | One per check (node/topic/dir) | While the node runs |
| `topic_stats.py -s N` | From `-n/-t/--dir`, or everything | For the N-second sample |
| `trace_ctl.py start --all/-n/-t/-p`, `track` | Persistent | Until `untrack` / `stop` |

```bash
./trace_ctl.py start --all          # record everything since system start
./trace_ctl.py mode all|selected    # switch at any time
./trace_ctl.py track -n '/robot1/*' # persistent rule; untrack ID | --all
./trace_ctl.py status               # rules, traced processes, counters
```

Granularity is the process: a container hosting many nodes is traced whole.

## topic_stats.py

```bash
./topic_stats.py                          # traced endpoints, counts since tracing began
./topic_stats.py -s 5 -n '/robot1/*'      # trace matching processes for 5 s
./topic_stats.py -t '*scan*' --known      # include endpoints that exist but are not traced
./topic_stats.py -w -g node --dir sub     # refresh; subscriptions grouped by node
./topic_stats.py --trace ~/.ros/tracing/foo -f json   # offline
```

Columns: `count` (since `since`), `rate` (window), `avg_rate` (first to last message), `last_age`, `since` (`start` = counted since the endpoint was created).

## topic_diagnostics.py

```bash
python3 topic_diagnostics.py --config demo/diagnostics.yaml    # can start at any time
```

Configured by a YAML file (`--config`); ROS args are ignored. Example: [demo/diagnostics.yaml](demo/diagnostics.yaml).

```yaml
period: 1.0                 # publish period [s]
window: 5.0                 # rate window [s]
checks:
  - {topic: /scan, dir: pub, min_rate: 8, max_rate: 12}    # summed over publishers
  - {topic: /cmd_vel, dir: sub, node: /controller}         # each subscriber must receive
```

| Check key | Default | Meaning |
|---|---|---|
| `topic` | required | Topic name or glob |
| `dir` | `pub` | `pub`: rate summed over publishers. `sub`: every subscription checked |
| `node` | any | Node name or glob |
| `min_rate`, `max_rate` | none | Hz. Neither set: any message is OK |
| `name` | `<dir> <topic>` | DiagnosticStatus name |

Each check is also a tracing rule: the processes it matches are traced while the node runs.

| Level | When |
|---|---|
| OK | rate within `[min_rate, max_rate]` (or > 0 if unset) |
| ERROR | no matching endpoint, no messages in window, rate out of range, daemon unreachable |
| STALE | first `window` seconds, or while tracing of a new process starts |
| WARN (tracer status) | discarded events, or unnamed processes (started before `trace_ctl.py start`) |

## Caveats

| Issue | Detail |
|---|---|
| Start order | Processes started before `trace_ctl.py start` have no names (counted per pid in `--all` mode only) |
| Intra-process | Messages that never leave the process aren't counted |
| Loaned / serialized | Not seen by `rmw_take` |
| Disk | relayd also writes live traces to `~/lttng-traces`. The hot channel is capped (1 MiB x 4 files per stream); `stop` deletes it |

## Tests

```bash
python3 -m pytest -q test                                  # offline + client; diagnostics if ROS sourced
ROS2MON_LIVE_TEST=1 python3 -m pytest -q test              # + end to end (uses the ros2mon_* sessions)
uvx ruff check .
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)): lint; build + offline tests without ROS; Jazzy + LTTng end-to-end.

## Overhead

About 180 ns per traced message per endpoint inside traced processes; 0 in untraced ones. Tracer CPU at 100 nodes / 300 topics / 100 Hz: 2.2% names only, 4.8% with 1 of 10 processes traced, 17% everything. See [docs/OVERHEAD.md](docs/OVERHEAD.md).
