# How ROS 2 Tracing Works (and how this project uses it)

## 1. The stack

```
 ROS process                                   lttng daemons (per user)           consumer
+---------------------------------------+     +--------------------------+     +----------------+
| rclcpp / rclpy                        |     | lttng-sessiond           |     | trace_daemon   |
|   TRACETOOLS_TRACEPOINT(rclcpp_...)   |     |   session config, events |     |  (babeltrace2  |
| rcl                                   |     | lttng-consumerd          |---->|   C API)       |
|   TRACETOOLS_TRACEPOINT(rcl_...)      |     |   drains ring buffers    |     |   counts       |
| rmw_fastrtps / rmw_cyclonedds         |     | lttng-relayd (live only) |     +----------------+
|   TRACETOOLS_TRACEPOINT(rmw_...)      |     |   streams to live readers|
|        |                              |     +--------------------------+
|        v                              |                 ^
| libtracetools.so: ros_trace_<event>() |                 |
|        |                              |  shared-memory ring buffers (per CPU)
| liblttng-ust.so ----------------------+-----------------+
+---------------------------------------+
```

| Layer | What it does |
|---|---|
| `TRACETOOLS_TRACEPOINT(name, ...)` | Macro in rcl/rclcpp/rmw source. Calls `ros_trace_<name>()` in `libtracetools.so`. Compiled to a no-op if tracetools was built without LTTng. |
| `libtracetools.so` | Defines the `ros2` LTTng-UST tracepoint provider, i.e. every `ros2:*` event and its fields. Linked by `librcl.so`, so every ROS process loads `liblttng-ust`. |
| `liblttng-ust` | In-process tracer. Each tracepoint checks an "enabled" flag. If the flag is set, it serializes the fields into a lock-free per-CPU shared-memory ring buffer. Also starts 2 listener threads per process that register with `lttng-sessiond`. |
| `lttng-sessiond` | Control plane. Holds sessions, decides which events are enabled, and pushes that to registered processes. |
| `lttng-consumerd` | Data plane. Drains ring buffers into CTF trace files (or forwards them to relayd). |
| `lttng-relayd` | Needed only for live mode. Lets readers consume a session while it records. Also writes the trace to `~/lttng-traces`. |
| `libbabeltrace2` | Reads CTF (from files or `net://` live). `trace_daemon` uses its C API. |

Verify the build: `ros2 run tracetools status` prints `Tracing enabled`.

## 2. Cost model

| State | Cost per tracepoint hit |
|---|---|
| No session, or event not enabled | Function call into `libtracetools` plus one branch on a flag. Effectively free. |
| Event enabled | Read the clock, reserve space in the ring buffer, copy the fields, commit. No syscalls, no locks. |
| Buffer full | The event is **dropped** (default discard mode). The application is never blocked. |

The consumer side (consumerd, relayd, trace_daemon) runs in separate processes. It costs CPU on the machine, but never latency inside the traced application.

Measured numbers: see [Overhead](#6-overhead).

## 3. Message path and tracepoints

Publish (one `publish()` call):

```
rclcpp_publish(publisher_handle=NULL, msg)   rclcpp only
  rcl_publish(publisher_handle, msg)         rclcpp + rclpy
    rmw_publish(rmw_publisher_handle, msg, ts)
```

Receive (one message):

```
rmw_take(rmw_subscription_handle, msg, src_ts, taken)   taken=0 when nothing was available
  rcl_take(msg)
    rclcpp_take(msg)
      callback_start(callback, is_intra)
      callback_end(callback)
```

Setup events fire once, when the entity is created, and carry the mapping from handles to names:

| Event | Key fields |
|---|---|
| `rcl_node_init` | `node_handle`, `node_name`, `namespace` |
| `rcl_publisher_init` | `publisher_handle`, `node_handle`, `rmw_publisher_handle`, `topic_name`, `queue_depth` |
| `rcl_subscription_init` | `subscription_handle`, `node_handle`, `rmw_subscription_handle`, `topic_name` |
| `rmw_publisher_init` / `rmw_subscription_init` | `rmw_*_handle`, `gid` (DDS GUID) |
| `rclcpp_subscription_init`, `rclcpp_subscription_callback_added` | links the rcl subscription to the rclcpp object and its callback |

The full list is in `/opt/ros/jazzy/include/tracetools/tracetools/tracetools.h`, or run `lttng list -u` while a node is running.

## 4. How trace_daemon builds its table

```
rcl_node_init          (pid, node_handle)             -> "/ns/node"
rcl_publisher_init     (pid, rmw_publisher_handle)    -> Endpoint(node, topic, pub)
rcl_subscription_init  (pid, rmw_subscription_handle) -> Endpoint(node, topic, sub)
rmw_*_init only        (pid, rmw_handle)              -> internal (discovery), hidden
rmw_publish            (pid, rmw_publisher_handle)    -> count, timestamp
rmw_take [taken==1]    (pid, rmw_subscription_handle) -> count, timestamp
```

- Handles are raw pointers, unique only inside one process, hence the `vpid` key.
- Init events come from session `ros2mon_meta` (every process). Message events come from `ros2mon_hot`, whose vpid tracker contains only the selected processes.
- Selection: a rule (node/topic/proc globs, dir) matches named endpoints. Their PIDs go into the tracker via `lttng track -u -s ros2mon_hot --vpid PID`. Untracked processes never get the events enabled.
- Rates: 100 ms buckets by event timestamp, evaluated at `wall - 0.7 s` (live lag), over `[max(now - window, since), now)`.
- Details: [collector/README.md](../collector/README.md). Rationale: [DESIGN.md](DESIGN.md).

## 5. Session lifetime and start order

| Situation | Result |
|---|---|
| Node starts after `trace_ctl.py start` | Named; counted whenever its process is selected |
| Node started before | No names (init events were never recorded). In `all` mode counted per pid ("unnamed") |
| Live viewer attaches to an already running session | Starts at the live head and misses init events |
| Process selected late (diagnostics node started later, `-s` sample) | Counted from the moment of selection (`since`) |

Consequence: `trace_ctl.py start` creates both sessions and the daemon before the stack. Everything else connects to the daemon at any time.

## 6. Limitations

| Limitation | Why | Workaround |
|---|---|---|
| Start order | See section 5 | `trace_ctl.py start` first |
| Process granularity | The tracker selects PIDs | Split components that need separate selection |
| Intra-process only | Messages bypass rmw | Also count `rclcpp_intra_publish` |
| Loaned / serialized messages | Different rmw take paths | None |
| No endpoint fini event | Jazzy has no `*_fini` tracepoints | Prune exited PIDs via `/proc` |
| Drops at high rate | Ring buffer overrun | Larger `--subbuf-size` / `--num-subbuf` |

## 7. Overhead

About 180 ns per enabled event in traced processes, 0 in untraced ones. Tracer CPU at 96k events/s: 17% of a core with everything traced, 2.2% with names only. Full numbers: [OVERHEAD.md](OVERHEAD.md).
