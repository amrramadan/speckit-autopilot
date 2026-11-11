"""Linear read-back through MCP.

Python cannot call the Linear MCP server directly (it is an OAuth HTTP server owned by
the Claude Code CLI), so a tiny read-only session is asked to call specific tools and the
supervisor parses the RAW tool results from the message stream — never the model's
paraphrase. Writes happen only in the close-out session (phase ``linear``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import gates as G
from .policy import PolicyContext, PolicyGate
from .prompts import linear_reader_prompt
from .sessions import SessionFactory, SessionSpec, TurnResult


@dataclass
class LinearSnapshot:
    workspace_url: str | None = None
    issue: dict[str, Any] | None = None
    comments: list[dict[str, Any]] | None = None
    project: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    tools_called: list[str] = field(default_factory=list)


def parse_snapshot(turn: TurnResult, tool_prefix: str | None = None) -> LinearSnapshot:
    snap = LinearSnapshot(cost_usd=turn.cost_delta_usd)
    for call in turn.tool_calls:
        snap.tools_called.append(call.name)
        if tool_prefix and call.name.startswith("mcp__") and not call.name.startswith(tool_prefix):
            snap.errors.append(f"ignored {call.name}: not the configured Linear server ({tool_prefix}*)")
            continue
        res = turn.tool_results.get(call.id)
        if res is None:
            continue
        short = call.name.rsplit("__", 1)[-1]
        if res.is_error:
            snap.errors.append(f"{short}: {res.text()[:300]}")
            continue
        data = G.parse_mcp_json(res.content)
        if short == "get_workspace" and isinstance(data, dict):
            snap.workspace_url = data.get("url")
        elif short == "get_issue" and isinstance(data, dict):
            snap.issue = data
        elif short == "list_comments":
            if isinstance(data, dict):
                snap.comments = list(data.get("comments") or data.get("nodes") or [])
            elif isinstance(data, list):
                snap.comments = data
        elif short == "get_project" and isinstance(data, dict):
            snap.project = data
    if snap.issue is None and not any(e.startswith("get_issue") for e in snap.errors):
        snap.errors.append("get_issue returned nothing parseable" if any(c.endswith("get_issue") for c in snap.tools_called) else "get_issue was not called (is the Linear MCP server connected?)")
    return snap


async def read_linear(
    make_session: SessionFactory,
    *,
    repo: Path,
    issue: str,
    project: str | None,
    tool_prefix: str,
    model: str | None,
    base_ctx: PolicyContext | None = None,
    log_event: Callable[..., Any] = lambda *a, **k: None,
    budget_usd: float | None = None,
    timeout_s: float = 300,
    plugin_dirs: tuple[str, ...] = (),
    server: str | None = None,
    env: dict[str, str] | None = None,
) -> LinearSnapshot:
    ctx = base_ctx or PolicyContext(phase="linear_read", repo=repo, linear_tool_prefix=tool_prefix)
    from dataclasses import replace

    gate = PolicyGate(replace(ctx, phase="linear_read"), log_event=log_event)
    spec = SessionSpec(
        label=f"linear-read-{time.strftime('%H%M%S')}",
        phase="linear_read",
        cwd=repo,
        gate=gate,
        system_append="You are a read-only Linear reader. Call exactly the tools you are asked to call, nothing else.",
        budget_usd=budget_usd,
        model=model,
        allowed_tools=("Read",),
        read_only=True,
        plugin_dirs=tuple(plugin_dirs),
        wait_mcp=(server,) if server else (),
        env=dict(env or {}),
    )
    sess = make_session(spec)
    try:
        await sess.start()
        turn = await sess.send(linear_reader_prompt(tool_prefix, issue, project), timeout_s=timeout_s)
    except Exception as exc:
        return LinearSnapshot(errors=[f"reader session failed: {exc!r}"])
    finally:
        try:
            await sess.close()
        except Exception:
            pass
    return parse_snapshot(turn, tool_prefix)
