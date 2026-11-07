# Findings

Experiments on 2026-11-06, LTTng 2.13.11, Jazzy, `demo_nodes_cpp` talker + listener.

## Which tracepoints are on

Only the 7 monitor events (`trace_stats.EVENTS`), plus `vpid`/`procname` context. About 30 other `ros2:*` tracepoints stay off. Cost: +195 ns per publish, vs +432 ns with all `ros2:*` ([bench/README.md](../bench/README.md) section 2). Per-message events: only `rmw_publish` and `rmw_take`.

## Default scope: every process

A session's event rules apply to every process of the user that loads `liblttng-ust`, which means every ROS process.

## Restricting to selected processes

| Mechanism | Command | Excluded-process cost | Result |
|---|---|---|---|
| vpid tracker | `lttng untrack -u --vpid --all`, then `lttng track -u --vpid PID` | **zero**, events never enabled there | Works. Untracked processes produce no events at all, init events included. |
| Late tracking | `lttng track -u --vpid PID` on an already running, started session | zero | **Works**: events appear right away in the running process. |
| Context filter | `enable-event ... --filter '$ctx.procname == "talker*"'` | ~70 ns per event (filter runs in-process) | Works. Init events left unfiltered, so names are still known. |
| vuid/vgid tracker | `lttng track -u --vuid USER` | zero | Not tested. Needs nodes run as a separate user/group. |

Notes:
- `procname` is the 15-char thread name. A Python node started as `python3 x.py` is `python3`. Started via a shebang (`ros2 run`) it gets the script name.
- Tracker granularity is the process. A component container gets tracked whole.
- A tracker change applies to the session, not to an event. Init events and message events that need different scopes need **separate sessions**.

## Snapshot mode

```bash
lttng create mon --snapshot
lttng enable-channel -u -s mon meta --subbuf-size=1M --num-subbuf=4   # init events, must never wrap
lttng enable-channel -u -s mon hot  --subbuf-size=1M --num-subbuf=4   # rmw_publish/take, ring buffer
lttng snapshot record -s mon        # dump buffers -> topic_stats.py --trace <dir>
```

| Result | |
|---|---|
| Works | Snapshots at 8 s and 12 s both named talker/listener, correct counts, about 300-400 KB each |
| Consumer CPU between snapshots | ~0, nothing reads the buffers |
| Counts | Only what's in the hot ring buffer (last N seconds), not since start |
| Bug to fix | Per-CPU buffers wrap at different times. The rate window must start at the latest per-CPU first event. The offline path uses the global first event, so rates come out low. |
| Sizing | About 6 MB of hot buffer per second at 96k events/s. Meta: about 4 MB covers thousands of endpoints. |

## Live consumer cost (for comparison)

| Load | Live, C++ collector |
|---|---|
| idle | 0.7% core |
| 10 Hz, 300 topics, ~9.6k events/s | 10% core |
| 100 Hz, ~96k events/s | 21.6% core |

Consumer cost scales with traced events/s. Tracing only selected processes cuts it roughly in proportion.
