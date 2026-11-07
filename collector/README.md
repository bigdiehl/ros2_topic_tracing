# trace_daemon (C++)

Long-running consumer of the two LTTng live sessions created by `trace_ctl.py start`. Names every endpoint, counts messages for selected processes, keeps the hot session's vpid tracker in sync with the rules, and answers queries on a Unix socket. Users never call it directly; `trace_ctl.py`, `topic_stats.py` and `topic_diagnostics.py` do.

## Build

```bash
sudo apt install --no-install-recommends libbabeltrace2-dev   # build; runtime needs only libbabeltrace2-0
cmake -S collector -B collector/build && make -C collector/build
collector/build/trace_daemon check                             # prints "ok" if plugins load
```

## Modes

```bash
trace_daemon live --meta URL --hot URL --hot-session NAME --socket PATH \
  [--lag-ms 700] [--bucket-ms 100] [--prune-s 5] [--history-s 60] [--idle-ms 100]
trace_daemon fs DIR [--window S]      # offline: one stats JSON on stdout (topic_stats.py --trace)
trace_daemon check
```

| Env var (read by the Python side) | Effect |
|---|---|
| `ROS2MON_SOCKET` | Socket path (default `$XDG_RUNTIME_DIR/ros2mon.sock`); log next to it (`.log`) |
| `TRACE_DAEMON` | Use a different binary |
| `TRACE_STATS_BUCKET_MS=100` | Rate bucket width |

## Socket protocol

One request line, one JSON reply line. Values contain no spaces.

| Request | Reply |
|---|---|
| `ping` / `status` / `shutdown` | ok / rules, traced processes, counters / ok, then exit |
| `stats [window=S] [node=G]... [topic=G]... [tracked=1]` | `endpoints` (named), `untracked` (unnamed, per pid), counters |
| `rule add [node=G] [topic=G] [proc=G] [dir=pub\|sub] [persist=1]` | `{"id": N}` |
| `rule del id=N` / `rule clear` | Remove one rule / all persistent rules |

- A rule selects every process with a named endpoint matching all of its globs. `node=*` `topic=*` `proc=*` and no `dir` means everything (`lttng track --vpid --all`).
- A non-persistent rule belongs to its connection and is removed when that connection closes.

Endpoint fields: `topic dir node pid proc depth tracked count rate avg_rate last_age since from_start`. Untraced endpoints have null counts. `since` = seconds counted; `from_start` = counted since the endpoint was created.

## Internals

- **Graphs:** two babeltrace2 graphs (`source.ctf.lttng-live` -> `filter.utils.muxer` -> sink), one per session, run in one thread with `poll()` on the socket.
- **Idle backoff:** a graph that returned AGAIN isn't polled again for `--idle-ms` (hot) or 2x that (meta). Each poll costs one relayd request per stream. Without the backoff relayd used 15% of a core at 100 Hz; with it, 2%.
- **Tracker:** changes run `lttng track/untrack -u -s <hot> --vpid ...`, one at a time, diffing desired vs applied. Counting starts or resets when the command completes.
- **Keys:** endpoints are keyed `(vpid, rmw_handle)`. A message seen before its init event creates a placeholder that the init event fills in later. Exited PIDs are pruned via `/proc` every `--prune-s`.
- **Rates:** 100 ms buckets, evaluated at `wall - lag`, over `[max(now - window, since), now)`.
