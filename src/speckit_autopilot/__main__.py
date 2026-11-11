"""CLI: speckit-autopilot --repo PATH --issue KEY "description" [flags]   (or: python -m speckit_autopilot ...)

    speckit-autopilot --repo ~/code/my-app --issue ENG-142 "Add product filters" --until ship
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import PHASES, ConfigError, load_config
from .report import status_summary
from .state import RunLock, RunPaths, StateStore, pid_alive


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="speckit-autopilot", description="Spec Kit Autopilot: one Linear issue from start to merged + closed, unattended.",
        epilog="settings: speckit-autopilot {init,config,model,effort,linear,context-reset,sandbox,statusline,version} --help")
    p.add_argument("description", nargs="?", default=None, help="feature description (required for a new run)")
    p.add_argument("--repo", default=None, help="target repository path. Default: the current directory if it is a git repo")
    p.add_argument("--issue", default=None, help="issue key, e.g. ENG-142 (required with Linear on; optional with --no-linear)")
    lin = p.add_mutually_exclusive_group()
    lin.add_argument("--linear", dest="linear", action="store_const", const="on", default=None, help="use Linear for this run (read the issue, close it out after merge)")
    lin.add_argument("--no-linear", dest="linear", action="store_const", const="off", help="no Linear at all for this run (--issue becomes optional)")
    p.add_argument("--spec", default=None, help="spec id for cc-feature-start (number like 116, or key like spec-ABC / RATINGS-01)")
    p.add_argument("--resume", action="store_true", help="continue from the last checkpoint (gates re-checked idempotently)")
    p.add_argument("--until", choices=(*PHASES, "none"), default=None, help="stop BEFORE this phase (e.g. --until ship stops before shipping); --until none clears a stop a run was started with (with --resume: run through ship and the close-out)")
    p.add_argument("--from", dest="from_phase", choices=PHASES, default=None, help="restart at this phase (its preconditions are re-checked)")
    p.add_argument("--allow-escalations", action="store_true", help="let the ship gate open despite judge escalations")
    # no USD budget and no wall-clock limit: these old flags still parse, so old command lines keep working, but they
    # are hidden and ignored
    p.add_argument("--budget", type=float, default=None, help=argparse.SUPPRESS)
    p.add_argument("--wallclock-h", type=float, default=None, help=argparse.SUPPRESS)
    p.add_argument("--skip-preflight", action="store_true", help="start a real run without the preflight checks (not recommended)")
    p.add_argument("--judge-model", default=None)
    p.add_argument("--worker-model", default=None)
    p.add_argument("--dry-run", action="store_true", help="run preflight only")
    p.add_argument("--preflight-run-tests", action="store_true", help="with --dry-run: also run the test command once")
    p.add_argument("--no-judge-ping", action="store_true", help="with --dry-run: skip the paid judge ping")
    p.add_argument("--detach", action="store_true", help="start the run in the background (survives the terminal / Claude Code session) and print the PID")
    p.add_argument("--status", action="store_true", help="summarise the current/last run and exit")
    p.add_argument("--watch", action="store_true", help="live, refreshing progress view (this checkout's run inside a repo, every active run elsewhere or with --all); Ctrl-C to quit")
    p.add_argument("--all", action="store_true", help="with --watch: every active or recent run in the workspace")
    p.add_argument("--interval", type=float, default=5.0, help="with --watch: seconds between refreshes (default 5)")
    p.add_argument("--once", action="store_true", help="with --watch: print one frame and exit")
    p.add_argument("--stop", action="store_true", help="send SIGTERM to the running supervisor (it kills the running tooling command, cancels an in-flight judge call and stops within seconds) and wait for the checkpoint")
    p.add_argument("--report", action="store_true", help="print REPORT.md and decisions.md and exit")
    p.add_argument("--abandon", action="store_true", help="archive a halted/stopped/paused run so a new one can start in this repo (never touches git or Linear)")
    p.add_argument("--cli-path", default=None, help="use this Claude Code CLI instead of the SDK's bundled one")
    return p


def resolve_repo(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    cwd = Path.cwd()
    if (cwd / ".git").exists():
        return cwd
    raise SystemExit("--repo is required (the current directory is not a git repository)")


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    from .admin import ADMIN_COMMANDS

    if raw and raw[0] in ADMIN_COMMANDS:  # settings sub-commands (config, model, effort, statusline, ...)
        from . import admin

        return admin.main(raw)
    args = build_parser().parse_args(argv)
    if args.watch:  # works outside a repo too (the workspace root): then it shows every active run
        from . import live

        here = None if args.all else (resolve_repo(args.repo) if args.repo or (Path.cwd() / ".git").exists() else None)
        return live.watch(here, args.interval, args.once)
    repo = resolve_repo(args.repo)
    paths = RunPaths(repo)

    if args.status:
        print(status_summary(paths))
        return 0
    if args.report:
        for p in (paths.report,):
            print(p.read_text() if p.exists() else f"(no {p})")
        if paths.state.exists():
            st = StateStore(paths.state).load()
            if st.feature_dir and (Path(st.feature_dir) / "decisions.md").exists():
                print("\n" + "=" * 80 + f"\n{Path(st.feature_dir) / 'decisions.md'}\n" + "=" * 80)
                print((Path(st.feature_dir) / "decisions.md").read_text())
        return 0
    if args.stop:
        return stop_run(paths)
    if args.abandon:
        return abandon_run(paths)

    issue = args.issue
    description = args.description
    if (args.resume or args.from_phase) and paths.state.exists():
        st = StateStore(paths.state).load()
        issue = issue or st.issue
        description = description or st.description
    if not description and not args.dry_run:
        raise SystemExit("a feature description is required for a new run")

    overrides = {
        "linear": args.linear,
        "issue": issue,
        "description": description or "",
        "spec": args.spec,
        "resume": args.resume,
        "until": args.until,
        "from_phase": args.from_phase,
        "allow_escalations": args.allow_escalations or None,
        "judge_model": args.judge_model,
        "worker_model": args.worker_model,
        "dry_run": args.dry_run,
        "python": sys.executable,
    }
    try:
        cfg = load_config(repo, overrides)
    except ConfigError as exc:
        raise SystemExit(f"config error: {exc}")
    from .config import looks_like_issue_key, resolve_linear
    from .runner import slugify

    linear_on, linear_why = resolve_linear(cfg)
    if linear_on and not looks_like_issue_key(cfg.issue):
        raise SystemExit(f"Linear is on ({linear_why}): pass --issue <KEY> (e.g. ENG-142), or --no-linear to run without Linear")
    if not cfg.issue:
        # no tracker: the run is labelled by its description (state, report, lock), e.g. "product-filters"
        if not cfg.description:
            raise SystemExit("--issue or a feature description is required")
        cfg.issue = slugify(cfg.description, cfg.slug_words)

    if args.detach:
        return detach(argv if argv is not None else sys.argv[1:], paths)

    from .sessions import scrub_parent_session_env, sdk_session_factory
    from .state import EventLog

    removed = scrub_parent_session_env()
    from .state import ensure_git_exclude

    ensure_git_exclude(repo)  # before anything is written under .autopilot/ (keeps the user's tree clean)
    paths.ensure()

    if args.dry_run:
        from .preflight import run_preflight

        log = EventLog(paths.root / "preflight.jsonl", "preflight")
        factory = sdk_session_factory(log, paths.transcripts / "preflight", cli_path=args.cli_path)
        rep = asyncio.run(run_preflight(cfg, factory, judge_ping=not args.no_judge_ping, run_tests=args.preflight_run_tests))
        if removed:
            rep.add("env scrub", "INFO", "removed parent-session variables: " + ", ".join(removed))
        print(rep.render())
        return 0 if rep.ok else 1

    from .judge import SDKJudge
    from .prompts import JUDGE_SYSTEM_PROMPT
    from .runner import Runner

    if not args.skip_preflight:
        # preflight runs in seconds, before any real spend; a resume skips feature_start's preconditions
        from .preflight import run_preflight

        plog = EventLog(paths.root / "preflight.jsonl", "preflight")
        pfactory = sdk_session_factory(plog, paths.transcripts / "preflight", cli_path=args.cli_path)
        resuming = bool(args.resume or args.from_phase)
        rep = asyncio.run(run_preflight(cfg, pfactory, judge_ping=True, check_preconditions=not resuming))
        (paths.root / "PREFLIGHT.md").write_text("```\n" + rep.render() + "\n```\n")
        print(rep.render(), flush=True)
        if not rep.ok:
            print(f"\nAutopilot did not start: preflight failed (details: {paths.root / 'PREFLIGHT.md'}).", flush=True)
            return 1

    log = EventLog(paths.events, "", echo=lambda m: print(m, flush=True))
    factory = sdk_session_factory(log, paths.transcripts, cli_path=args.cli_path)
    judge = SDKJudge(repo, cfg.judge_model, cfg.judge_call_budget_usd, log, paths.transcripts, JUDGE_SYSTEM_PROMPT, effort=cfg.judge_effort, env=cfg.session_env())
    runner = Runner(cfg, session_factory=factory, judge=judge, echo=lambda m: print(m, flush=True))

    outcome = asyncio.run(runner.run())
    print(f"\nAutopilot finished: {outcome.status} — {outcome.reason}\nReport: {outcome.report}")
    return {"completed": 0, "paused": 0, "halted": 2, "stopped": 3}.get(outcome.status, 1)


def detach(argv: list[str], paths: RunPaths) -> int:
    """Re-exec this CLI in a new session (setsid equivalent; macOS has no setsid binary)."""
    from .state import ensure_git_exclude

    ensure_git_exclude(paths.repo)
    paths.ensure()
    child_args = [a for a in argv if a != "--detach"]
    if "--repo" not in child_args and not any(a.startswith("--repo=") for a in child_args):
        child_args = ["--repo", str(paths.repo), *child_args]
    log = open(paths.console, "ab", buffering=0)
    log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} detached launch: {' '.join(child_args)}\n".encode())
    kwargs: dict = dict(cwd=os.getcwd(), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    if os.name == "nt":  # pragma: no cover
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([sys.executable, "-m", "speckit_autopilot", *child_args], **kwargs)
    print(json.dumps({"pid": proc.pid, "console": str(paths.console), "events": str(paths.events), "status": f"{sys.executable} -m speckit_autopilot --repo {shlex.quote(str(paths.repo))} --status"}))
    return 0


def abandon_run(paths: RunPaths) -> int:
    if RunLock(paths.lock).holder_alive():
        print("A live autopilot run holds the lock; stop it first (--stop).")
        return 1
    if not paths.state.exists():
        print("No run to abandon.")
        return 0
    st = StateStore(paths.state).load()
    dest = paths.root / "archive" / f"{st.run_id}-abandoned"
    dest.mkdir(parents=True, exist_ok=True)
    for p in (paths.state, paths.events, paths.decisions_jsonl, paths.report, paths.post_ship_decisions, paths.lock):
        if p.exists():
            p.rename(dest / p.name)
    print(f"Archived run {st.run_id} ({st.issue}, {st.status}) to {dest}. Git branches, PRs and Linear were not touched.")
    return 0


def stop_run(paths: RunPaths) -> int:
    info = RunLock(paths.lock).read()
    if not info or not pid_alive(int(info.get("pid", -1))):
        print("No live autopilot run holds the lock.")
        return 1
    pid = int(info["pid"])
    os.kill(pid, signal.SIGTERM)
    print(f"Sent SIGTERM to pid {pid}; waiting for the checkpoint …")
    for _ in range(180):
        time.sleep(1)
        if not pid_alive(pid):
            break
    st = StateStore(paths.state).load() if paths.state.exists() else None
    print(f"Process {'exited' if not pid_alive(pid) else 'still running'}; state: {st.status if st else '?'} (phase {st.current_phase if st else '?'})")
    print(f"Halt report: {paths.report}")
    return 0 if not pid_alive(pid) else 1


def entry() -> None:  # console-script entry point
    sys.exit(main())


if __name__ == "__main__":
    sys.exit(main())
