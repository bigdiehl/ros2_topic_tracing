# Design Record

Decisions, the reasons for them, and open items. Newest first within each section. Proposals not yet adopted: [working/](../working/README.md).

## Architecture

```
trace_ctl.py start  (before the stack)
  ros2mon_meta  live session, ALL processes:  rcl_node_init, rcl/rmw_*_init  (+vpid, procname)
  ros2mon_hot   live session, vpid tracker:   rmw_publish, rmw_take[taken==1] (+vpid)
        |  relayd
        v
  trace_daemon (C++) -- names from meta, counts from hot, `lttng track/untrack` from rules
        ^  Unix socket ($XDG_RUNTIME_DIR/ros2mon.sock), line request / JSON reply
        |
  topic_diagnostics.py  (rules per check, polls stats at 1 Hz, publishes /diagnostics)
  topic_stats.py        (queries; -s N adds temporary rules)
  trace_ctl.py          (persistent rules, mode all|selected, status, stop)
```

| Component | Role |
|---|---|
| `collector/trace_daemon` | Two bt2 live graphs, endpoint table, bucketed rates, rules -> vpid tracker, socket server. Offline `fs` mode. |
| `trace_stats.py` | Session setup, daemon launch, `Client`, `filter_rows` |
| `trace_ctl.py` | start / stop / status / track / untrack / mode |
| `topic_stats.py` | CLI. Instant query, `-s N` sample, `--trace` offline |
| `topic_diagnostics.py` | Checks from a YAML file. A daemon client; can start at any time |
| `test/`, `.github/workflows/ci.yml` | pytest suite (D22); CI: lint, build + offline, Jazzy end-to-end |

## Decisions

| # | Decision | Why |
|---|---|---|
| D22 | Tests: offline on a committed 160 KB trace (`test/data`), client against a fake socket, live end-to-end opt-in (`ROS2MON_LIVE_TEST=1`). CI runs all three, the last on Jazzy + LTTng | Most logic is testable without ROS; the live test uses the fixed session names so it must not run by surprise on a dev machine |
| D21 | Idle live graph not re-polled for 100 ms (hot) / 200 ms (meta) | Each poll is one relayd request per stream. Polling idle meta while hot was busy: relayd 15% -> 2%, daemon 24% -> 13% at 100 Hz. Idle 2.1% -> 1.2%. |
| D20 | Hot channel trace files capped (1 MiB x 4 per stream); `stop` deletes session output | relayd writes every live session to `~/lttng-traces` too (972 MB accumulated during development). Cap measured to cost nothing. |
| D19 | **C++ required**. Text backend and Python stats removed | One code path. The Python share was 30% of consumer CPU (6.4 of 21.6%). Supersedes D3, D8, D11, D13. |
| D18 | Rules owned by their socket connection; persistent rules only via `trace_ctl.py`. Counts reset when tracking (re)starts; each endpoint reports `since` / `from_start` | The diagnostics node only needs counts since it started; a crashed client can't leave tracing on. "Since system start" is valid only for `from_start` endpoints. |
| D17 | Unix socket, text request / JSON reply. Daemon has no ROS dependency | Daemon starts before any ROS setup; doesn't trace itself; simple to test |
| D16 | **Two sessions**: meta (all processes) + hot (vpid tracker). Tracker driven by `lttng track/untrack` CLI | Tracker is per session, and names must come from every process. Tested: untracked processes emit nothing (zero in-app cost) and PIDs can be added to a running session. CLI avoids a `liblttng-ctl-dev` dependency; changes are rare. Hot events carry only `vpid` (procname from meta). |
| D15 | **Pure C++ daemon**, Python argparse front ends | Low CPU priority; everything user-facing stays Python. Supersedes D5 (CLI reads `/topic_stats`) and D6 (diagnostics node owns the session). |
| D12 | Rates from 100 ms buckets (`TRACE_STATS_BUCKET_MS`) | Query is O(buckets), not O(messages) |
| D1 | Count with `rmw_publish` / `rmw_take`, keyed `(vpid, rmw_handle)` | `rcl_publish`'s handle for `/rosout` differs from its init handle (rcl copies the struct). rmw events cover rclcpp and rclpy. |
| D2 | Map names with `rcl_*_init`. An rmw handle with only `rmw_*_init` is internal | Separates `ros_discovery_info` traffic from user topics |
| D4 | Rates from **event timestamps**, evaluated at `wall - live_lag` (0.7 s) | Independent of pipe/consumer lag |
| D7 | `rmw_take` filtered `taken == 1` inside the tracer | Drops empty takes before they reach the consumer |
| D9 | Prune exited PIDs via `/proc` every 5 s | Jazzy has no fini tracepoint. Assumes one PID namespace. |
| D10 | Diagnostics: pub check sums over publishers; sub check per subscriber, worst wins; STALE during first window | "Is the topic alive" vs. "is every consumer receiving" |

Superseded: D3 (parse babeltrace text), D5 (no attach, CLI reads `/topic_stats`), D6 (diagnostics node owns the session), D8 (Python hot-path regex), D11 (C++ collector + Python stats), D13 (collector self-check with text fallback).

## Performance options

| # | Option | Status |
|---|---|---|
| P1 | Selective tracing by process | **DONE (D16)**: vpid tracker, zero cost in untraced processes |
| P2b | C++ consumer on the babeltrace2 C API | **DONE (D11, then D15)** |
| P3 | Per-handle filters | Not pursued: filter cost grows with handle count; P1 covers it |
| P4 | Pub-only mode (skip `rmw_take`) | Open. Events / (1 + subs). Loses receive checks. |
| P5 | Larger live timer | Open. Fewer wakeups, more latency |
| P7 | Own CTF / lttng-live reader | Not worth it: everything mode is 17% at 96k events/s, selected modes are near idle |
| P8 | Snapshot-mode session for ad hoc queries | Open, see [working/FINDINGS.md](../working/FINDINGS.md) |

## Open items

- Intra-process comms: add `rclcpp_intra_publish` + `callback_start(is_intra=1)` if used.
- Containers / PID namespaces: vpid keys, `/proc` pruning and the vpid tracker assume one namespace.
- Daemon restart loses names (init events are gone). Restart = restart the stack.
- Process granularity: a component container is traced whole.
- Packaging / distribution: not decided (ament package + deb via bloom, or a plain deb).
- Diagnostics config is a YAML file, not ROS parameters.
