"""The judge: a fresh, read-only decision session for every call (docs/DESIGN.md §2.5).

Modes: ``respond`` (reply to the worker), ``answer`` (AskUserQuestion), ``verify``
(Definition of Done) and ``route`` (forward / back / halt). Output is forced through
a JSON schema and validated in code; invalid output is retried once, then the
caller treats it as a failed phase.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .state import EventLog, TranscriptWriter

# --------------------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------------------

DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "item": {"type": "string", "description": "The question or finding being decided"},
        "recommended": {"type": ["string", "null"], "description": "The option the worker recommended/suggested, if any"},
        "chosen": {"type": "string"},
        "followed_recommendation": {"type": ["boolean", "null"]},
        "rationale": {"type": "string"},
        "evidence": {"type": "string", "description": "file + section citations"},
        "impact": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["item", "chosen", "followed_recommendation", "rationale", "evidence", "impact"],
    "additionalProperties": False,
}

RESPOND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["needs_reply", "done", "failed"]},
        "reply": {"type": "string", "description": "Exact text to send to the worker when status=needs_reply, else empty"},
        "decisions": {"type": "array", "items": DECISION_SCHEMA},
        "blocking_issues_remaining": {"type": "integer", "minimum": 0},
        "escalate": {"type": "boolean"},
        "escalate_reason": {"type": "string"},
    },
    "required": ["status", "reply", "decisions", "blocking_issues_remaining", "escalate", "escalate_reason"],
    "additionalProperties": False,
}

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "the exact question text"},
                    "labels": {"type": "array", "items": {"type": "string"}, "description": "chosen option label(s), verbatim"},
                    "free_text": {"type": "string", "description": "only when no option fits"},
                },
                "required": ["question", "labels"],
                "additionalProperties": False,
            },
        },
        "decisions": {"type": "array", "items": DECISION_SCHEMA},
        "escalate": {"type": "boolean"},
        "escalate_reason": {"type": "string"},
    },
    "required": ["answers", "decisions", "escalate", "escalate_reason"],
    "additionalProperties": False,
}

VERIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pass": {"type": "boolean"},
        "failed_criteria": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
        "escalate": {"type": "boolean"},
        "escalate_reason": {"type": "string"},
        # model_mode = "auto" only: the worker model / effort for the next attempt at this phase (null = keep the ladder)
        "next_worker": {
            "type": ["object", "null"],
            "properties": {
                "model": {"type": ["string", "null"]},
                "effort": {"type": ["string", "null"]},
                "reason": {"type": "string"},
            },
            "required": ["reason"],
            "additionalProperties": False,
        },
    },
    "required": ["pass", "failed_criteria", "notes"],
    "additionalProperties": False,
}

# "fix_here" answers the early route check (analyze / review round 1, before any fix): the root cause is in this phase,
# so the normal repair loop runs. After repairs are exhausted only forward / back / halt are meaningful.
ROUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["forward", "back", "halt", "fix_here"]},
        "target_phase": {"type": ["string", "null"]},
        "reason": {"type": "string"},
    },
    "required": ["action", "reason"],
    "additionalProperties": False,
}

SCHEMAS = {"respond": RESPOND_SCHEMA, "answer": ANSWER_SCHEMA, "verify": VERIFY_SCHEMA, "route": ROUTE_SCHEMA}


class JudgeOutputError(ValueError):
    pass


def validate(instance: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Minimal JSON-schema validator for the subset used above (no external deps)."""
    t = schema.get("type")
    types = t if isinstance(t, list) else [t] if t else []
    if types:
        ok = False
        for ty in types:
            if ty == "object" and isinstance(instance, dict):
                ok = True
            elif ty == "array" and isinstance(instance, list):
                ok = True
            elif ty == "string" and isinstance(instance, str):
                ok = True
            elif ty == "boolean" and isinstance(instance, bool):
                ok = True
            elif ty == "integer" and isinstance(instance, int) and not isinstance(instance, bool):
                ok = True
            elif ty == "number" and isinstance(instance, (int, float)) and not isinstance(instance, bool):
                ok = True
            elif ty == "null" and instance is None:
                ok = True
        if not ok:
            raise JudgeOutputError(f"{path}: expected {types}, got {type(instance).__name__}")
    if "enum" in schema and instance not in schema["enum"]:
        raise JudgeOutputError(f"{path}: {instance!r} not in {schema['enum']}")
    if isinstance(instance, int) and not isinstance(instance, bool) and "minimum" in schema and instance < schema["minimum"]:
        raise JudgeOutputError(f"{path}: {instance} < minimum {schema['minimum']}")
    if isinstance(instance, dict):
        for req in schema.get("required", []):
            if req not in instance:
                raise JudgeOutputError(f"{path}: missing required field {req!r}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = [k for k in instance if k not in props]
            if extra:
                raise JudgeOutputError(f"{path}: unexpected field(s) {extra}")
        for k, v in instance.items():
            if k in props:
                validate(v, props[k], f"{path}.{k}")
    if isinstance(instance, list) and "items" in schema:
        for i, v in enumerate(instance):
            validate(v, schema["items"], f"{path}[{i}]")


# --------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------


@dataclass
class JudgeResult:
    mode: str
    data: dict[str, Any]
    cost_usd: float = 0.0
    duration_s: float = 0.0
    ok: bool = True
    error: str = ""

    # convenience accessors ---------------------------------------------------------
    @property
    def status(self) -> str:
        return self.data.get("status", "failed")

    @property
    def reply(self) -> str:
        return self.data.get("reply", "")

    @property
    def decisions(self) -> list[dict[str, Any]]:
        return list(self.data.get("decisions", []) or [])

    @property
    def escalate(self) -> bool:
        return bool(self.data.get("escalate"))

    @property
    def escalate_reason(self) -> str:
        return self.data.get("escalate_reason", "")


@dataclass
class JudgeRequest:
    mode: str  # respond | answer | verify | route (payload["early"]: the round-1 root-cause check)
    phase: str
    context: dict[str, Any]  # issue, description, feature_dir, artifacts, ...
    payload: dict[str, Any]  # mode specific
    recent_decisions: list[dict[str, Any]] = field(default_factory=list)


class Judge(abc.ABC):
    @abc.abstractmethod
    async def call(self, req: JudgeRequest) -> JudgeResult: ...

    async def respond(self, req: JudgeRequest) -> JudgeResult:
        req.mode = "respond"
        return await self.call(req)

    async def answer(self, req: JudgeRequest) -> JudgeResult:
        req.mode = "answer"
        return await self.call(req)

    async def verify(self, req: JudgeRequest) -> JudgeResult:
        req.mode = "verify"
        return await self.call(req)

    async def route(self, req: JudgeRequest) -> JudgeResult:
        req.mode = "route"
        return await self.call(req)


# --------------------------------------------------------------------------------------
# SDK judge
# --------------------------------------------------------------------------------------


class SDKJudge(Judge):
    """One fresh ``query()`` per decision, read-only tools, JSON-schema output."""

    def __init__(
        self,
        repo: Path,
        model: str,
        budget_usd: float | None,
        log: EventLog,
        transcripts_dir: Path,
        system_prompt: str,
        effort: str | None = None,
        max_turns: int = 40,
        env: dict[str, str] | None = None,
        timeout_s: float = 600.0,
    ):
        self.repo = repo
        self.model = model
        self.budget_usd = budget_usd
        self.log = log
        self.transcripts_dir = transcripts_dir
        self.system_prompt = system_prompt
        self.effort = effort
        self.max_turns = max_turns
        self.env = dict(env or {})
        self.timeout_s = timeout_s
        self.calls = 0

    def _options(self, schema: dict[str, Any]) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        kwargs: dict[str, Any] = dict(
            cwd=str(self.repo),
            model=self.model,
            system_prompt=self.system_prompt,
            setting_sources=["project"],
            tools=["Read", "Grep", "Glob"],
            allowed_tools=["Read", "Grep", "Glob"],
            permission_mode="dontAsk",
            max_turns=self.max_turns,
            max_budget_usd=self.budget_usd,
            output_format={"type": "json_schema", "schema": schema},
            # The judge needs no MCP: without this every call carries ~100K tokens of MCP tool definitions
            # (measured: $0.70 vs $0.10 per cold call).
            strict_mcp_config=True,
            mcp_servers={},
            env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false", **self.env},
        )
        if self.effort:
            kwargs["effort"] = self.effort
        # same credential guard as workers: the judge's reads never touch .env*, ~/.ssh, ~/.aws, token stores
        from claude_agent_sdk import HookMatcher

        from .policy import PolicyContext, PolicyGate

        guard = PolicyGate(PolicyContext(phase="judge", repo=self.repo), log_event=lambda ev, **kw: self.log.emit(f"judge_{ev}", **kw))
        kwargs["hooks"] = {"PreToolUse": [HookMatcher(matcher=None, hooks=[guard.pre_tool_use_hook], timeout=30, on_failure="block")]}
        return ClaudeAgentOptions(**kwargs)

    @staticmethod
    def _on_msg(msg: Any, tw: TranscriptWriter) -> None:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock):
                    tw.write("assistant_text", b.text)
                elif isinstance(b, ToolUseBlock):
                    tw.write("tool_use", {"name": b.name, "input": b.input})
        elif isinstance(msg, ResultMessage):
            tw.write("result", {"subtype": msg.subtype, "is_error": msg.is_error, "cost": msg.total_cost_usd, "structured_output": msg.structured_output, "result": msg.result or ""})

    async def call(self, req: JudgeRequest) -> JudgeResult:
        from claude_agent_sdk import ResultMessage, query

        from .prompts import judge_prompt

        schema = SCHEMAS[req.mode]
        prompt = judge_prompt(req)
        self.calls += 1
        tw = TranscriptWriter(self.transcripts_dir, f"{time.strftime('%Y%m%d-%H%M%S')}-judge-{self.calls:03d}-{req.phase}-{req.mode}")
        tw.write("prompt", prompt)
        total_cost = 0.0
        last_err = ""
        start = time.monotonic()
        for attempt in (1, 2):
            data: Any = None
            result_text = ""
            try:
                async with asyncio.timeout(self.timeout_s):
                    async with contextlib.aclosing(query(prompt=prompt if attempt == 1 else prompt + _retry_note(last_err), options=self._options(schema))) as stream:
                        async for msg in stream:
                            self._on_msg(msg, tw)
                            if isinstance(msg, ResultMessage):
                                total_cost += float(msg.total_cost_usd or 0.0)
                                data = msg.structured_output
                                result_text = msg.result or ""
                                if msg.is_error and data is None:
                                    last_err = f"judge session error: {msg.subtype} {getattr(msg, 'errors', None) or ''}"
            except TimeoutError:
                last_err = f"judge call timed out after {self.timeout_s:.0f}s"
                tw.write("error", last_err)
                continue
            except Exception as exc:  # SDK/process errors (ResultError is raised AFTER an error ResultMessage)
                if data is None:
                    last_err = f"judge call raised {type(exc).__name__}: {exc}"
                    tw.write("error", last_err)
                    continue
            if data is None and result_text:
                data = _extract_json(result_text)
            try:
                if data is None:
                    raise JudgeOutputError(last_err or "no structured output")
                validate(data, schema)
                out = JudgeResult(req.mode, data, cost_usd=total_cost, duration_s=time.monotonic() - start)
                self.log.emit("judge", phase=req.phase, mode=req.mode, attempt=attempt, cost_usd=round(total_cost, 4), output=data, summary=_judge_summary(req.mode, data))
                return out
            except JudgeOutputError as exc:
                last_err = str(exc)
                self.log.emit("judge_invalid", phase=req.phase, mode=req.mode, attempt=attempt, error=last_err)
        return JudgeResult(req.mode, {}, cost_usd=total_cost, duration_s=time.monotonic() - start, ok=False, error=last_err)


def _retry_note(err: str) -> str:
    return f"\n\nYOUR PREVIOUS OUTPUT WAS INVALID ({err}). Return ONLY output that matches the JSON schema exactly."


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _judge_summary(mode: str, data: dict[str, Any]) -> str:
    if mode == "respond":
        return f"{data.get('status')}: {str(data.get('reply', ''))[:120]}"
    if mode == "verify":
        return f"pass={data.get('pass')} {'; '.join(data.get('failed_criteria', []))[:160]}"
    if mode == "route":
        return f"{data.get('action')} {data.get('target_phase') or ''}: {str(data.get('reason', ''))[:120]}"
    if mode == "answer":
        return "; ".join(f"{a.get('question', '')[:40]}={a.get('labels')}" for a in data.get("answers", []))[:200]
    return ""


# --------------------------------------------------------------------------------------
# Fake judge (tests)
# --------------------------------------------------------------------------------------

JudgePolicy = Callable[[JudgeRequest], "dict[str, Any] | None"]


class FakeJudge(Judge):
    """Scripted judge. ``policy(req)`` returns the raw output dict (validated like the real one).

    Returning ``None`` (or an invalid dict) simulates invalid judge output.
    """

    def __init__(self, policy: JudgePolicy, cost_usd: float = 0.005):
        self.policy = policy
        self.cost_usd = cost_usd
        self.requests: list[JudgeRequest] = []

    async def call(self, req: JudgeRequest) -> JudgeResult:
        self.requests.append(req)
        last = ""
        for _ in (1, 2):
            data = self.policy(req)
            try:
                if data is None:
                    raise JudgeOutputError("no output")
                validate(data, SCHEMAS[req.mode])
                return JudgeResult(req.mode, data, cost_usd=self.cost_usd)
            except JudgeOutputError as exc:
                last = str(exc)
        return JudgeResult(req.mode, {}, cost_usd=self.cost_usd, ok=False, error=last)


def respond(status: str = "done", reply: str = "", decisions: list[dict[str, Any]] | None = None, blocking: int = 0, escalate: bool = False, reason: str = "") -> dict[str, Any]:
    """Helper to build a respond-mode output (used by FakeJudge policies and tests)."""
    return {
        "status": status,
        "reply": reply,
        "decisions": decisions or [],
        "blocking_issues_remaining": blocking,
        "escalate": escalate,
        "escalate_reason": reason,
    }


def decision(item: str, chosen: str, recommended: str | None = None, followed: bool | None = None, impact: str = "low", rationale: str = "r", evidence: str = "e") -> dict[str, Any]:
    if followed is None:
        followed = (recommended == chosen) if recommended else None
    return {
        "item": item,
        "recommended": recommended,
        "chosen": chosen,
        "followed_recommendation": followed,
        "rationale": rationale,
        "evidence": evidence,
        "impact": impact,
    }
