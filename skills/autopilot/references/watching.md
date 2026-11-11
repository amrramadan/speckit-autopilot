# Watching a detached run

## Right after `--detach`

`--detach` prints one JSON line:
`{"pid": N, "console": "<repo>/.autopilot/console.log", "events": "<repo>/.autopilot/run.jsonl", "status": "…"}`.
Show the user the pid and the paths. If the command printed an error instead (`config error: …`,
`--issue is required`, `A previous run for … is halted. Use --resume`), relay it verbatim and stop.

Within ~10 s, read the last lines of `console.log`. If the process already exited (lock held by another run,
preflight failure, traceback), report that instead of starting a watch.

## Live updates

While the user is present, use the Monitor tool (timeout 1800000 ms; re-arm on expiry while the process is alive):

```bash
EV="<events path>"; PID=<pid>; n=$(wc -l < "$EV" 2>/dev/null || echo 0)
while true; do
  m=$(wc -l < "$EV" 2>/dev/null || echo 0)
  if [ "$m" -gt "$n" ]; then
    sed -n "$((n+1)),${m}p" "$EV" | jq -r 'select(.event|test("^(phase_start|phase_end|route|reset|escalation|ship_gate|decisions_frozen|run_end|halt_notice_posted|human_tasks_pause|checklists_pause|test_baseline_unparseable|shared_issue|signal)$")) | "\(.ts) \(.event) \(.phase // "") \(.status // "") \(if has("open") then "open=\(.open)" else "" end) \(.summary // .reason // "")"'
    n=$m
  fi
  kill -0 "$PID" 2>/dev/null || { echo "autopilot pid $PID exited"; exit 0; }
  sleep 60
done
```

(Without `jq`, use `SA --repo REPO --watch --once` on each tick instead.)

For each batch, tell the user in one or two lines what changed: phase started / passed / failed, routed back, context
reset, escalation, ship gate open/closed, run finished. Skip `turn` / `judge` / `gate` noise unless asked.

The user can also watch without Claude: `speckit-autopilot --watch` in a terminal, or the status line
(`/autopilot statusline install`).

## When the process exits

Run `SA --repo REPO --status` and summarise it, then by outcome:

- **completed** — give the PR URL and merge commit from `.autopilot/REPORT.md`. For a shared issue (REPORT.md says
  "shared issue … status not changed") say the issue status was deliberately left alone.
- **paused** — say why: `--until` (which phase it stopped before), open human-only tasks before ship, or open
  checklist items before implement. For the last two, show "What a person must do before resuming" from REPORT.md.
  The user does that work; never tick the boxes yourself. Continue with `/autopilot resume` once they say it is done.
- **halted / stopped** — show "Why it stopped", the failed checks and the **Resume** command from REPORT.md. A
  `stopped` run failed nothing; `--resume` continues it. Never resume on your own; ask the user.

The detached supervisor keeps running without this session. The user can come back any time with
`/autopilot status`, `/autopilot report`, `/autopilot watch` or `/autopilot stop`.
