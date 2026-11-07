# Goals

1. **Query** pub/sub counts for any or all nodes/topics, at any time.
2. **Diagnostics node**: monitors configured nodes/topics, publishes `/diagnostics` at about 1 Hz.

## Workflow

```
start tracing  ->  launch stack  ->  (any time) diagnostics node and/or CLI
```

Tracing start must come first: init events (node/topic names) fire once ([docs/DESIGN.md](../docs/DESIGN.md) D6). Everything after it can start in any order.

## Priorities

| Priority | Meaning |
|---|---|
| Low CPU | Nominal launch runs only the diagnostics node. Only its configured processes should be traced. |
| Ad hoc queries | The CLI may ask about things the diagnostics node doesn't watch. Processes must be addable/removable at runtime. |
| Mode switch | Switch between "record everything" and "record selected" at any time. |

## CLI questions to answer

| # | Question | Needs |
|---|---|---|
| Q1 | Sample 5 s, stats for everything | Temporarily trace all processes for 5 s |
| Q2 | Stats for the diagnostics topics, since the diagnostics node started | Its processes counted while it runs (W3) |
| Q3 | Stats for everything, since system start | System started in "record everything" mode |
