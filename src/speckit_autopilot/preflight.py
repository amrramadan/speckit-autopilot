"""Preflight (docs/DESIGN.md §4): seconds, before any real spend. Used by --dry-run and at run start."""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import gates as G
from .config import Config, resolve_linear
from .linear import read_linear
from .policy import PolicyContext, PolicyGate
from .repo import SETUID_SHIMS, Git, RepoProfile, detect_profile, gh, run_shell, sandbox_shim_dir
from .sessions import SANDBOX_EXEMPT_PHASES, SessionFactory, SessionSpec, sandbox_available, sandbox_settings, tooling_sandbox_available, tooling_sandbox_profile
from .state import RunLock, RunPaths

REQUIRED_LINEAR_TOOLS = ("get_issue", "list_issue_statuses", "save_issue", "save_comment", "list_comments", "get_project", "save_project", "list_issues")
OPTIONAL_LINEAR_TOOLS = ("save_status_update", "get_workspace")


@dataclass
class Item:
    name: str
    status: str  # PASS | FAIL | WARN | INFO | SKIP
    detail: str = ""


@dataclass
class PreflightReport:
    items: list[Item] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.items.append(Item(name, status, detail))

    @property
    def ok(self) -> bool:
        return not any(i.status == "FAIL" for i in self.items)

    def render(self) -> str:
        w = max((len(i.name) for i in self.items), default=10)
        lines = ["Spec Kit Autopilot — preflight", ""]
        for i in self.items:
            first, *rest = (i.detail or "").splitlines() or [""]
            lines.append(f"  {i.status:<4}  {i.name:<{w}}  {first}")
            for r in rest:
                lines.append(f"  {'':<4}  {'':<{w}}  {r}")
        n = {s: sum(1 for i in self.items if i.status == s) for s in ("PASS", "WARN", "FAIL")}
        lines += ["", f"Result: {'READY' if self.ok else 'NOT READY'} — {n['PASS']} pass, {n['WARN']} warn, {n['FAIL']} fail"]
        return "\n".join(lines)


_PKG_SCRIPT = re.compile(r"^(?:pnpm|npm|yarn|bun)(?:\s+run)?\s+(?P<name>[\w:-]+)\s*$")


def baseline_command_notes(cmd: str | None, repo: Path) -> list[str]:
    """Why test_baseline = "base" could never excuse a failure for this test command (empty: it can). Looks one level
    into package.json for "pnpm test"-style commands; a bail hidden deeper in a script is not visible."""
    if not cmd:
        return ["test_baseline = base but no test command is configured: nothing is measured"]
    seen = cmd
    m = _PKG_SCRIPT.match(cmd.strip())
    if m:
        try:
            scripts = json.loads((repo / "package.json").read_text()).get("scripts", {})
        except (OSError, ValueError):
            scripts = {}
        if isinstance(scripts.get(m.group("name")), str):
            seen = f"{cmd} → {scripts[m.group('name')]}"
    notes = []
    if G.CHAINED_COMMAND.search(seen.split("→")[-1]):
        notes.append(f"`{seen}` chains commands (&&, ||, ;): an early pre-existing failure hides later suites, so the baseline never applies")
    if G.BAIL_FLAG.search(seen):
        notes.append(f"`{seen}` stops at the first failure (bail flag), so the baseline never applies")
    if re.search(r"\bpnpm\b.*\s(?:-r|--recursive)\b", seen) and "--no-bail" not in seen:
        notes.append(f"`{seen}`: pnpm -r stops at the first failing package; use `pnpm -r --no-bail test` as test_cmd for the baseline to apply")
    if re.search(r"\bturbo\b", seen) and "--continue" not in seen:
        notes.append(f"`{seen}`: turbo stops at the first failing package unless --continue is passed; the baseline applies only when nothing was skipped")
    return notes


def sandbox_item(cfg: Config) -> tuple[str, str, str]:
    """The "sandbox" preflight line: the configured mode and whether worker Bash commands will really be sandboxed."""
    ok, why = sandbox_available()
    exempt = ", ".join(sorted(SANDBOX_EXEMPT_PHASES - {"halt_notice"}))
    extra = f" · extra hosts {cfg.sandbox_network_domains}" if cfg.sandbox_network_domains else ""
    if cfg.sandbox == "off":
        return ("sandbox", "INFO", "off: worker Bash commands run without the OS sandbox (only the policy checks them)")
    if not ok:
        if cfg.sandbox == "on":
            return ("sandbox", "FAIL", f"sandbox = on but the OS sandbox is not available here ({why}); install it or set sandbox = \"auto\"/\"off\"")
        return ("sandbox", "WARN", f"auto: not available here ({why}), so worker Bash commands run without the OS sandbox")
    return ("sandbox", "PASS", f"{cfg.sandbox}: active ({why}) for analyze/implement/review (clarify/plan/tasks only in a session of their own); not for {exempt}{extra}")


def tooling_sandbox_item(cfg: Config) -> tuple[str, str, str]:
    """The "tooling sandbox" preflight line: whether the gates' test/lint/typecheck/build run under the OS sandbox."""
    if cfg.tooling_sandbox == "off" or (cfg.tooling_sandbox == "auto" and cfg.sandbox == "off"):
        return ("tooling sandbox", "INFO", "off: the gates run test/lint/typecheck/build (worker-written code) with your rights, minus credential-like variables")
    ok, why = tooling_sandbox_available()
    if not ok:
        if cfg.tooling_sandbox == "on":
            return ("tooling sandbox", "FAIL", f"tooling_sandbox = on but {why}; set tooling_sandbox = \"auto\"/\"off\"")
        return ("tooling sandbox", "WARN", f"auto: {why}, so the gates run worker-written code unsandboxed (minus credential-like variables)")
    detail = f"{cfg.tooling_sandbox}: {why}; writes only in the repo/temp/package caches, no credential files, localhost-only network (build keeps the network)"
    shim = sandbox_shim_dir()
    if not shim:
        return ("tooling sandbox", "WARN", detail + f"\nno non-setuid copy of {', '.join(SETUID_SHIMS)} could be built, so tests that run `ps` fail inside the sandbox (setuid programs cannot run there)")
    return ("tooling sandbox", "PASS", detail + f" · `{'`, `'.join(SETUID_SHIMS)}` from non-setuid copies in {shim}")


async def run_preflight(
    cfg: Config,
    make_session: SessionFactory,
    *,
    judge_ping: bool = True,
    linear_check: bool = True,
    run_tests: bool = False,
    profile: RepoProfile | None = None,
    check_preconditions: bool = True,
) -> PreflightReport:
    rep = PreflightReport()
    repo = cfg.repo
    # ---------------------------------------------------------------- environment
    try:
        import claude_agent_sdk
        from claude_agent_sdk._cli_version import __cli_version__

        rep.add("python / sdk", "PASS", f"Python {sys.version.split()[0]} · claude-agent-sdk {claude_agent_sdk.__version__} (bundled CLI {__cli_version__})")
    except Exception as exc:
        rep.add("python / sdk", "FAIL", f"claude-agent-sdk not importable: {exc!r} — see README.md#install")
        return rep
    if not (repo / ".git").exists():
        rep.add("repo", "FAIL", f"{repo} is not a git repository")
        return rep
    prof = profile or detect_profile(repo, cfg.commands, {"test": cfg.test_cmd, "lint": cfg.lint_cmd, "typecheck": cfg.typecheck_cmd, "build": cfg.build_cmd}, cfg.base_branch)
    rep.facts["profile"] = prof
    rep.add("repo", "PASS", f"{repo} · base {prof.base_branch} · origin {prof.remote_slug or '?'} · Spec Kit {prof.speckit_version or '?'}")
    lock = RunLock(RunPaths(repo).lock)
    info = lock.read()
    rep.add("run lock", "FAIL" if lock.holder_alive() else "PASS", f"held by pid {info.get('pid')} ({info.get('run_id')})" if lock.holder_alive() and info else "free")

    # ---------------------------------------------------------------- commands on disk
    needed = ["feature_start", "specify", "clarify", "plan", "tasks", "analyze", "implement", "review", "ship"]
    missing = [k for k in needed if k not in prof.commands]
    rep.add(
        "commands (files)",
        "FAIL" if missing else "PASS",
        ("missing: " + ", ".join(missing) + " — set [commands] in .autopilot.toml\n" if missing else "")
        + " ".join(f"{k}={v}" for k, v in prof.commands.items() if k in needed),
    )

    # ---------------------------------------------------------------- feature_start preconditions (+ fetch)
    if check_preconditions:
        pre = G.preconditions_feature_start(repo, prof.base_branch, fetch=True)
        for c in pre.checks:
            rep.add(f"precondition: {c.name}", "PASS" if c.passed else "FAIL", c.detail)
    else:
        rep.add("preconditions", "SKIP", "resuming an existing run: feature_start preconditions are re-checked by the runner where they apply")

    # ---------------------------------------------------------------- tooling
    for kind, cmd in prof.tooling.items():
        if not cmd:
            rep.add(f"tooling: {kind}", "WARN", f"no {kind} command detected; set {kind}_cmd in .autopilot.toml if the repo has one")
            continue
        exe = cmd.split()[0]
        found = shutil.which(exe) is not None
        rep.add(f"tooling: {kind}", "PASS" if found else "FAIL", f"`{cmd}` ({exe} {'found' if found else 'NOT on PATH'})")
    if cfg.test_baseline == "base":
        notes = baseline_command_notes(prof.tooling.get("test"), repo)
        rep.add("test baseline", "WARN" if notes else "PASS", "\n".join(notes) if notes else f"`{prof.tooling.get('test')}`: pre-existing failures on {prof.base_branch} are measured once in feature_start and tolerated test by test")
    if run_tests and prof.tooling.get("test"):
        t0 = time.monotonic()
        # the same way a run's gates do: under the tooling sandbox when it is on, without credential-like variables
        tsb = cfg.tooling_sandbox != "off" and not (cfg.tooling_sandbox == "auto" and cfg.sandbox == "off") and tooling_sandbox_available()[0]
        r = run_shell(prof.tooling["test"], repo, cfg.tooling_timeout_min * 60, env=cfg.tooling_env or None, sandbox_profile=tooling_sandbox_profile(repo, "test") if tsb else None, scrub_secrets=True)
        extra = ""
        if not r.ok:
            fails, why = G.explain_test_failures((r.stdout or "") + "\n" + (r.stderr or ""))
            extra = f"\nbaseline view: {why}" + (f": {sorted(fails)[:5]}" if fails else "")
        rep.add("tooling: test run", "PASS" if r.ok else "FAIL", f"exit {r.returncode} in {time.monotonic() - t0:.0f}s" + ("" if r.ok else extra + "\n" + r.tail(8)))

    # ---------------------------------------------------------------- gh / CI
    ga = gh(repo, "auth", "status")
    rep.add("gh auth", "PASS" if ga.ok else "FAIL", (ga.stdout + ga.stderr).strip().splitlines()[1].strip() if ga.ok and len((ga.stdout + ga.stderr).strip().splitlines()) > 1 else (ga.stderr.strip()[:200] or "gh auth status failed"))
    ci_required = cfg.ci_required if cfg.ci_required is not None else prof.has_ci
    healthy, detail = G.ci_health(repo, prof.base_branch)
    if ci_required and healthy is False:
        rep.add("ci", "FAIL", f"CI is required (ci_required={'auto' if cfg.ci_required is None else cfg.ci_required}) but looks non-functional: {detail}.\nThe ship gate would halt after merging. Set ci_required=false for this repo if merging on local evidence is acceptable.")
    else:
        rep.add("ci", "PASS" if healthy is not False else "WARN", f"required={ci_required} · {detail} · workflows {prof.ci_workflows}")

    # ---------------------------------------------------------------- spec identity preview
    rep.add("spec id style", "INFO", f"{cfg.spec_id_style} · branch '{cfg.branch_template}' · dir '{cfg.feature_dir_template}' · cc-feature-start args '{cfg.feature_start_args}'")
    rep.add(
        "human-owned work",
        "INFO",
        f"open human-only tasks before ship: {'pause' if cfg.human_tasks_block_ship else 'ship anyway (listed in the report + Linear)'} · "
        f"unchecked checklists before implement: {'pause' if cfg.checklists_gate == 'halt' else 'the judge answers speckit-implement'} · test_baseline={cfg.test_baseline}",
    )
    if cfg.spec_id_style == "key" and not cfg.spec and not cfg.spec_title_regex:
        rep.add("spec id", "FAIL", "this repo uses spec keys: pass --spec <KEY> (or set spec_title_regex to derive it from the Linear title)")
    if cfg.shared_issues:
        mine = cfg.issue.upper() in {k.strip().upper() for k in cfg.shared_issues} if cfg.issue else False
        rep.add("shared issues", "INFO", f"shared_issues {cfg.shared_issues}" + (f": {cfg.issue} is one, so the close-out comments and updates the status block but never changes its status" if mine else ""))
    rep.add(*sandbox_item(cfg))
    rep.add(*tooling_sandbox_item(cfg))
    linear_on, linear_why = resolve_linear(cfg)
    rep.add("linear", "INFO", ("on" if linear_on else "off") + f" ({linear_why})" + ("" if linear_on else " · no Linear reads or writes; the close-out phase is skipped"))
    linear_check = linear_check and linear_on

    # ---------------------------------------------------------------- SDK session: commands + MCP (no model turn)
    gate = PolicyGate(PolicyContext(phase="preflight", repo=repo, linear_tool_prefix=cfg.linear_tool_prefix, linear_enabled=linear_on))
    plugin_dirs = tuple(cfg.resolved_plugin_dirs())
    if cfg.plugin_dirs and not plugin_dirs:
        rep.add("plugin dirs", "FAIL", f"plugin_dirs {cfg.plugin_dirs} matched no directory")
    elif plugin_dirs:
        rep.add("plugin dirs", "PASS", ", ".join(plugin_dirs))
    # with the implement-tier sandbox settings the run will use, so a CLI that rejects them fails here, not mid-run
    spec = SessionSpec(label="preflight", phase="preflight", cwd=repo, gate=gate, system_append="preflight", budget_usd=None, model=cfg.worker_model, plugin_dirs=plugin_dirs, env=cfg.session_env(), wait_mcp=(cfg.linear_server,) if linear_on else (), sandbox=sandbox_settings("implement", cfg.sandbox, cfg.sandbox_network_domains, repo=repo))
    sess = make_session(spec)
    server_info = None
    mcp = None
    try:
        await sess.start()
        server_info = await sess.server_info()
        mcp = await sess.mcp_status()
    except Exception as exc:
        rep.add("sdk session", "FAIL", f"could not start a worker session: {exc!r}")
    finally:
        try:
            await sess.close()
        except Exception:
            pass
    if server_info is not None:
        names = set()
        for c in server_info.get("commands", []) or []:
            names.add(c.get("name") if isinstance(c, dict) else str(c).lstrip("/"))
        unresolved = []
        for k in needed:
            want = prof.commands.get(k, "").lstrip("/")
            alts = {want, want.replace("-", ".", 1), want.replace(".", "-", 1)}
            hit = next((n for n in names if n in alts or n.split(":")[-1] in alts), None)
            if not hit:
                unresolved.append(f"{k}={want}")
        rep.add("commands (SDK)", "FAIL" if unresolved else "PASS", ("not visible to the SDK session: " + ", ".join(unresolved)) if unresolved else f"all {len(needed)} commands visible to an SDK session ({len(names)} commands/skills loaded)")
        rep.facts["sdk_commands"] = sorted(n for n in names if n)
    if mcp is not None:
        servers = {s.get("name"): s for s in mcp.get("mcpServers", [])}
        srv = servers.get(cfg.linear_server)
        if not linear_on:
            pass
        elif srv is None:
            linearish = [f"{n} ({s.get('status')})" for n, s in servers.items() if "linear" in (n or "").lower()]
            rep.add("linear mcp", "FAIL", f"server {cfg.linear_server!r} not loaded in SDK sessions. Linear-like servers seen: {linearish or 'none'}. Set linear_server in .autopilot.toml.")
        else:
            tools = {t.get("name") for t in (srv.get("tools") or [])}
            short = {t.rsplit("__", 1)[-1] for t in tools if t}
            miss = [t for t in REQUIRED_LINEAR_TOOLS if t not in short]
            st = srv.get("status")
            rep.add("linear mcp", "PASS" if st == "connected" and not miss else "FAIL", f"{cfg.linear_server}: {st} · {len(short)} tools" + (f" · missing {miss}" if miss else "") + (f" · error {srv.get('error')}" if srv.get("error") else ""))
            rep.facts["linear_tools"] = sorted(short)
        others = [f"{n}:{s.get('status')}" for n, s in servers.items() if n != cfg.linear_server or not linear_on]
        rep.add("other mcp servers", "INFO", ", ".join(others)[:400] or "none")

    # ---------------------------------------------------------------- judge ping (tiny paid call)
    if judge_ping:
        rep_item = await _judge_ping(cfg)
        rep.add(*rep_item)

    # ---------------------------------------------------------------- Linear workspace + issue (cheap reader call)
    if linear_check and cfg.issue:
        snap = await read_linear(make_session, repo=repo, issue=cfg.issue, project=None, tool_prefix=cfg.linear_tool_prefix, model=cfg.reader_model, plugin_dirs=plugin_dirs, server=cfg.linear_server, env=cfg.session_env())
        if cfg.linear_workspace_url:
            ok = bool(snap.workspace_url) and snap.workspace_url.rstrip("/") == cfg.linear_workspace_url.rstrip("/")
            rep.add("linear workspace", "PASS" if ok else "FAIL", f"connected to {snap.workspace_url or '?'} (expected {cfg.linear_workspace_url})")
        else:
            rep.add("linear workspace", "FAIL", f"connected to {snap.workspace_url or '?'}; set linear_workspace_url for this repo (required: the run refuses to touch Linear without it)")
        if snap.issue:
            iss = snap.issue
            rep.add("linear issue", "PASS", f"{cfg.issue}: {iss.get('title')!r} · status {iss.get('status')} ({iss.get('statusType')}) · project {_nm(iss.get('project'))}")
            rep.facts["issue"] = iss
        else:
            rep.add("linear issue", "FAIL", f"could not read {cfg.issue}: {'; '.join(snap.errors)[:300]}")
    return rep


async def _judge_ping(cfg: Config) -> tuple[str, str, str]:
    try:
        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, SystemMessage, query

        opts = ClaudeAgentOptions(
            cwd=str(cfg.repo),
            model=cfg.judge_model,
            system_prompt="Reply with JSON only.",
            setting_sources=["project"],
            tools=[],
            permission_mode="dontAsk",
            max_turns=3,
            output_format={"type": "json_schema", "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}},
            strict_mcp_config=True,
            mcp_servers={},
            env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
        )
        src = None
        cost = 0.0
        out = None
        t0 = time.monotonic()
        async for m in query(prompt='Return {"ok": true}.', options=opts):
            if isinstance(m, SystemMessage) and m.subtype == "init":
                src = m.data.get("apiKeySource")
            elif isinstance(m, ResultMessage):
                cost = float(m.total_cost_usd or 0)
                out = m.structured_output
        ok = isinstance(out, dict) and out.get("ok") is True
        auth = {"none": "Claude Code login (OAuth / subscription)", None: "unknown"}.get(src, src)
        return ("sdk auth + judge", "PASS" if ok else "FAIL", f"{cfg.judge_model} answered {'with valid structured output' if ok else repr(out)} in {time.monotonic() - t0:.1f}s · auth: {auth}")
    except Exception as exc:
        return ("sdk auth + judge", "FAIL", f"{type(exc).__name__}: {exc}")


def _nm(v: Any) -> str:
    if isinstance(v, dict):
        return str(v.get("name") or v.get("id"))
    return str(v)
