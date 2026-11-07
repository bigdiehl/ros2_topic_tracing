# Proposal: trace daemon with selectable process scope

Status: **implemented 2026-11-06**. Decisions moved to [docs/DESIGN.md](../docs/DESIGN.md) D15-D21. Usage: [README.md](../README.md).

## How the goals map

| Goal ([GOALS.md](GOALS.md)) | Implementation |
|---|---|
| Start tracing, then launch, then tools in any order | `trace_ctl.py start`: meta + hot sessions + `trace_daemon` |
| Low CPU, diagnostics traces only its processes | Diagnostics registers one rule per check; only those PIDs are in the hot tracker |
| Add/remove processes at runtime | Rules -> `lttng track/untrack --vpid`, applied in ~20 ms |
| Switch everything/selected at any time | `trace_ctl.py mode all\|selected` |
| Q1: 5 s sample, everything | `topic_stats.py -s 5` (temporary rule, removed on exit) |
| Q2: diagnostics topics since the diagnostics node started | `topic_stats.py -t ...` (instant; those processes are already traced) |
| Q3: everything since system start | `trace_ctl.py start --all`; rows show `since = start` |

## Decisions taken in this round

| # | Decision |
|---|---|
| W1 | Unix socket, no ROS in the daemon (D17) |
| W2 | Pure C++ daemon; Python argparse scripts start/stop/configure it (D15) |
| W3 | Rules owned by their connection; persistent rules only from `trace_ctl.py` (D18) |

## Verified during implementation

- A viewer attached right after `lttng start` sees processes started after `trace_ctl.py start` returns. Tested with talker started immediately after; it was named.
- Late `lttng track --vpid PID` on a running, viewed session works.
- `track --vpid --all` covers processes started later.
- Two live graphs in one thread work, but each needs an idle backoff (D21).

## Not done / open

| Item | Notes |
|---|---|
| Daemon restart | Names are lost. Option: keep meta also in a snapshot-mode session as a backup |
| Snapshot-mode Q1 | ~0 consumer CPU between queries; see [FINDINGS.md](FINDINGS.md). Not needed now that "names only" is 2.2% |
| Start race | `trace_ctl.py start` waits 1 s (`--settle`) for the viewer to attach. No positive "attached" signal yet |
| Containers | vpid tracker and keys assume one PID namespace |
