#!/usr/bin/env bash
# Claude Code status line with live speckit-autopilot progress.
# Installed by:  speckit-autopilot statusline install [--scope local|project|user]
#   row 1:  the status line you had before (a checkout's .claude/statusline-command.sh, or the command saved by
#           `statusline install` in ~/.config/speckit-autopilot/statusline-previous.json)
#   row 2+: autopilot progress for this checkout's run, and a compact row for the other live runs
# Always exits 0: a failing status line would blank the bar.
input=$(cat)
PY="${SPECKIT_AUTOPILOT_PYTHON:-python3}"
here="${CLAUDE_PROJECT_DIR:-}"
if [ -z "$here" ]; then
    here=$(printf '%s' "$input" | "$PY" -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: d={}
w=d.get("workspace") or {}
print(w.get("project_dir") or w.get("current_dir") or d.get("cwd") or "")' 2>/dev/null)
fi

base=""
if [ -n "$here" ] && [ -f "$here/.claude/statusline-command.sh" ]; then
    base=$(printf '%s' "$input" | bash "$here/.claude/statusline-command.sh" 2>/dev/null)
else
    cmd=$(HERE="$here" "$PY" -c 'import json,os
cfg=os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
here=os.environ.get("HERE","")
cand=[os.path.join(here,".claude","settings.local.json"), os.path.join(here,".claude","settings.json"),
      os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"),"settings.json")]
try: saved=json.load(open(os.path.join(cfg,"speckit-autopilot","statusline-previous.json")))
except Exception: saved={}
for c in cand:
    if c in saved: print(saved[c].get("command","")); break
' 2>/dev/null)
    case "$cmd" in
        ""|*"speckit-autopilot statusline"*) ;;  # nothing saved, or this script itself
        *) base=$(printf '%s' "$input" | bash -c "$cmd" 2>/dev/null) ;;
    esac
fi

auto=$(printf '%s' "$input" | "$PY" -m speckit_autopilot.live --statusline 2>/dev/null)

printf '%s' "$base"
if [ -n "$auto" ]; then
    [ -n "$base" ] && printf '\n'
    printf '%s' "$auto"
fi
exit 0
