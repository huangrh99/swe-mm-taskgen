"""Fail-closed security audit for model-agent trial traces.

The public interface intentionally has one operation, :func:`audit_trial_trace`.
Both the online Pass@5 runner and the offline evidence auditor use it so a
trial cannot be accepted under a weaker, drifted trace policy.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable
from urllib.parse import urlsplit


_SOURCE_HOST = re.compile(
    r"(?i)(?:^|\.)(?:github\.com|githubusercontent\.com|githubassets\.com|"
    r"github\.io|gitlab\.com|bitbucket\.org)$"
)
_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_REFERENCE_ARTIFACT = re.compile(
    r"(?i)(?:^|[/\\._-])(?:gold|reference|solution|answer|oracle)"
    r"(?:[-_.][\w-]+)*\.(?:patch|diff)(?:[?#\s\"'}]|$)"
)
_UPSTREAM_ARTIFACT = re.compile(
    r"(?i)(?:/pull/\d+|/merge_requests?/\d+|/compare/|/commits?/|"
    r"/releases?(?:/|$)|/tags?(?:/|$)|\.(?:diff|patch)(?:[?#\s]|$)|"
    r"reference[-_/ ]?solution|upstream)"
)
_GIT_HISTORY = re.compile(
    r"(?is)\bgit(?:\s+-C\s+\S+)?\s+"
    r"(?:log|show|rev-list|reflog|blame|cat-file|name-rev|for-each-ref|"
    r"verify-commit|verify-tag|remote\s+(?:show|get-url)|branch\s+-[ar]|tag(?:\s|$))\b"
)
_GIT_REV_DIFF = re.compile(
    r"(?is)\bgit(?:\s+-C\s+\S+)?\s+diff\b[^\n;&|]{0,200}"
    r"(?:HEAD[~^]|refs/|remotes/|origin/|[0-9a-f]{7,40}(?:\.\.|\.\.\.)?)"
)
_NETWORK_COMMAND = re.compile(
    r"(?i)\b(?:curl|wget|aria2c|httpie|lynx|links|nc|ncat|netcat|socat|ssh|scp|sftp|rsync|"
    r"gh\s+(?:api|pr|issue|repo|release)|hub\b|"
    r"git(?:\s+-C\s+\S+)?\s+(?:clone|fetch|pull|ls-remote)|"
    r"python\S*\s+-c\s+[^\n]*(?:requests\.|urlopen\(|httpx\.)|"
    r"node\s+-e\s+[^\n]*(?:fetch\(|https?\.(?:get|request)))\b"
)

_LOCAL_TOOLS = {
    "applypatch", "bash", "edit", "execcommand", "glob", "grep", "listfiles",
    "read", "readmediafile", "search", "shell", "terminal", "todolist",
    "updateplan", "viewimage", "write", "writestdin",
}
_REMOTE_TOOL_RULES = (
    ("remote_web_search_call", re.compile(r"(?i)(?:^|[_.:])web_?search(?:$|[_.:])|websearch")),
    ("remote_url_fetch_call", re.compile(r"(?i)(?:^|[_.:])fetch_?url(?:$|[_.:])|fetchurl")),
    ("remote_browser_call", re.compile(r"(?i)(?:^|[_.:])(?:browser(?:_use)?|computer)(?:$|[_.:])")),
    ("remote_file_search_call", re.compile(r"(?i)(?:^|[_.:])file_?search(?:$|[_.:])")),
    ("remote_mcp_call", re.compile(r"(?i)(?:^|[_.:])mcp(?:$|[_.:])")),
    ("remote_connector_call", re.compile(r"(?i)(?:^|[_.:])connectors?(?:$|[_.:])")),
    ("remote_code_interpreter_call", re.compile(r"(?i)(?:^|[_.:])code_?interpreter(?:$|[_.:])")),
    ("remote_image_generation_call", re.compile(r"(?i)(?:^|[_.:])image_?generation(?:$|[_.:])")),
)


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("trace_json_not_object")
    return value


def _normal_tool(name: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _canonical_arguments(value: Any) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = value
    else:
        parsed = value
    try:
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(parsed)


def _call_parts(call: dict[str, Any]) -> tuple[str, Any]:
    function = call.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "unknown"), function.get("arguments")
    return str(call.get("function_name") or call.get("name") or "unknown"), (
        call.get("arguments") if "arguments" in call else call.get("args")
    )


def _atif_calls(trajectory: Path) -> list[tuple[str, Any]]:
    value = _load_object(trajectory)
    steps = value.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("empty_codex_trajectory")
    calls: list[tuple[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("invalid_codex_trajectory_step")
        raw_calls = step.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise ValueError("invalid_codex_atif_tool_calls")
        for call in raw_calls:
            if not isinstance(call, dict):
                raise ValueError("invalid_codex_atif_tool_call")
            calls.append(_call_parts(call))
    return calls


def _codex_raw_calls(rollout: Path) -> list[tuple[str, Any]]:
    calls: list[tuple[str, Any]] = []
    saw_session = saw_turn = False
    for line in rollout.read_text().splitlines():
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("codex_rollout_record_not_object")
        saw_session = saw_session or value.get("type") == "session_meta"
        saw_turn = saw_turn or value.get("type") == "turn_context"
        if value.get("type") != "response_item":
            continue
        payload = value.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("codex_response_item_payload_invalid")
        item_type = str(payload.get("type") or "")
        call_like = item_type.endswith("_call") or item_type in {
            "function_call", "custom_tool_call", "mcp_list_tools",
        }
        if not call_like:
            continue
        if item_type in {"function_call", "custom_tool_call"}:
            name = str(payload.get("name") or "unknown")
        elif item_type in {
            "local_shell_call", "shell_call", "apply_patch_call",
            "web_search_call", "file_search_call", "mcp_call", "mcp_list_tools",
            "computer_call", "image_generation_call", "code_interpreter_call",
        }:
            name = item_type
        else:
            name = f"unapproved:{item_type or 'unknown_call'}"
        arguments = payload.get("arguments")
        if arguments is None:
            arguments = {key: payload[key] for key in (
                "action", "query", "server_label", "connector_id", "command", "cmd",
            ) if key in payload}
        calls.append((name, arguments))
    if not saw_session or not saw_turn:
        raise ValueError("codex_rollout_structure_invalid")
    return calls


def _call_category(name: str) -> str:
    normalized = _normal_tool(name.removeprefix("unapproved:"))
    aliases = {
        "localshellcall": "shell", "shellcall": "shell", "bash": "shell",
        "execcommand": "shell", "shell": "shell", "terminal": "shell",
        "applypatchcall": "edit", "applypatch": "edit", "edit": "edit",
        "readmediafile": "viewimage", "viewimage": "viewimage",
        "todolist": "plan", "updateplan": "plan",
        "websearchcall": "websearch", "websearch": "websearch",
        "filesearchcall": "filesearch", "filesearch": "filesearch",
        "mcpcall": "mcp", "mcp": "mcp", "mcplisttools": "mcp",
        "computercall": "computer", "computer": "computer",
        "imagegenerationcall": "imagegeneration",
        "codeinterpretercall": "codeinterpreter",
    }
    return aliases.get(normalized, normalized)


def _call_signature(call: tuple[str, Any]) -> tuple[str, str]:
    name, arguments = call
    return _call_category(name), hashlib.sha256(_canonical_arguments(arguments).encode()).hexdigest()


def _host(url: str) -> str | None:
    try:
        return (urlsplit(url).hostname or "").rstrip(".").lower() or None
    except ValueError:
        return None


def _rule_hits(trace: Path, index: int, name: str, arguments: Any,
               allowed_network_hosts: frozenset[str]) -> list[dict[str, Any]]:
    text = _canonical_arguments(arguments)
    lower_name = name.lower()
    rules: list[tuple[str, list[str]]] = []
    for rule, pattern in _REMOTE_TOOL_RULES:
        if pattern.search(lower_name):
            rules.append((rule, [lower_name]))
    if name.startswith("unapproved:"):
        rules.append(("unapproved_call_event", [lower_name]))
    if not any(pattern.search(lower_name) for _rule, pattern in _REMOTE_TOOL_RULES):
        if _normal_tool(name) not in _LOCAL_TOOLS:
            rules.append(("unapproved_tool_call", [_normal_tool(name) or "unknown"]))

    urls = _URL.findall(text)
    hosts = sorted({host for host in map(_host, urls) if host})
    source_hosts = sorted(host for host in hosts if _SOURCE_HOST.search(host))
    if source_hosts:
        rules.append(("source_host_runtime_access", source_hosts))
    disallowed = sorted(host for host in hosts if host not in allowed_network_hosts)
    if disallowed:
        rules.append(("unapproved_network_host_access", disallowed))
    if _NETWORK_COMMAND.search(text) and not hosts:
        rules.append(("network_command_without_auditable_host", ["network_command"]))
    if (_UPSTREAM_ARTIFACT.search(text)
            and (_NETWORK_COMMAND.search(text) or any(
                rule in {"remote_url_fetch_call", "remote_browser_call"}
                for rule, _indicators in rules))):
        rules.append(("upstream_artifact_access", ["upstream_artifact"]))
    if _GIT_HISTORY.search(text) or _GIT_REV_DIFF.search(text):
        rules.append(("git_history_access", ["git_history"]))
    if _REFERENCE_ARTIFACT.search(text):
        rules.append(("reference_patch_access", ["reference_patch"]))

    fingerprint = hashlib.sha256(text.encode()).hexdigest()
    return [{
        "rule": rule,
        "trace_file": str(trace.resolve()),
        "record_index": index,
        "tool_name": name,
        "indicators": indicators,
        "arguments_sha256": fingerprint,
    } for rule, indicators in rules]


def _kimi_calls(trial: Path) -> tuple[list[tuple[Path, int, str, Any]], list[Path]]:
    wires = sorted((trial / "agent").rglob("wire.jsonl"))
    if not wires:
        raise ValueError("missing_kimi_wire_trace")
    calls: list[tuple[Path, int, str, Any]] = []
    for wire in wires:
        for index, line in enumerate(wire.read_text().splitlines(), 1):
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("invalid_kimi_wire_trace")
            event = value.get("event")
            if value.get("type") != "context.append_loop_event" or not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "")
            if "call" in event_type.lower() and event_type != "tool.call":
                calls.append((wire, index, f"unapproved:{event_type}", event))
            elif event_type == "tool.call":
                name, arguments = _call_parts(event)
                calls.append((wire, index, name, arguments))
    return calls, wires


def _codex_calls(trial: Path) -> tuple[
        list[tuple[Path, int, str, Any]], list[Path], str | None]:
    trajectories = sorted((trial / "agent").rglob("trajectory.json"))
    rollouts = sorted((trial / "agent").rglob("rollout-*.jsonl"))
    if not trajectories:
        raise ValueError("missing_codex_trajectory")
    if not rollouts:
        raise ValueError("missing_codex_raw_rollout")
    if len(trajectories) != 1 or len(rollouts) != 1:
        raise ValueError("ambiguous_codex_trace_set")
    try:
        atif = _atif_calls(trajectories[0])
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(str(exc) if isinstance(exc, ValueError)
                         else "invalid_codex_trajectory") from exc
    try:
        raw = _codex_raw_calls(rollouts[0])
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(str(exc) if isinstance(exc, ValueError)
                         else "invalid_codex_raw_rollout") from exc
    mismatch = None
    if [_call_signature(item) for item in atif] != [_call_signature(item) for item in raw]:
        mismatch = "codex_atif_raw_call_mismatch"
    calls = [(rollouts[0], index, name, arguments)
             for index, (name, arguments) in enumerate(raw, 1)]
    return calls, [trajectories[0], rollouts[0]], mismatch


def trace_files(trial: Path) -> list[Path]:
    names = {"trajectory.json", "wire.jsonl", "kimi-code.txt", "codex.txt"}
    return [path for path in sorted((trial / "agent").rglob("*"))
            if path.is_file() and (path.name in names
                                   or (path.name.startswith("rollout-")
                                       and path.suffix == ".jsonl"))]


def audit_trial_trace(trial: Path, *, agent: str,
                      allowed_network_hosts: Iterable[str] = ()) -> dict[str, Any]:
    """Return one fail-closed verdict for the authoritative agent trace."""
    trial = trial.resolve()
    allowed = frozenset(str(host).rstrip(".").lower() for host in allowed_network_hosts)
    files = trace_files(trial)
    if not files:
        return {"classification": "infrastructure_invalid", "valid": False,
                "reason": "missing_trace", "trace_files": []}
    if (trial / "agent").is_symlink() or any(path.is_symlink() for path in files):
        return {"classification": "infrastructure_invalid", "valid": False,
                "reason": "trace_symlink_detected",
                "trace_files": [str(path.resolve()) for path in files]}
    try:
        consistency_reason = None
        if agent == "kimi-code":
            calls, authoritative = _kimi_calls(trial)
        elif agent == "codex":
            calls, authoritative, consistency_reason = _codex_calls(trial)
        else:
            raise ValueError("unsupported_agent_trace_policy")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        reason = str(exc) or "invalid_trace"
        if agent == "kimi-code" and reason.startswith("Expecting"):
            reason = "invalid_kimi_wire_trace"
        elif agent == "codex" and reason.startswith("Expecting"):
            reason = "invalid_codex_raw_rollout"
        return {"classification": "infrastructure_invalid", "valid": False,
                "reason": reason,
                "trace_files": [str(path.resolve()) for path in files]}
    hits: list[dict[str, Any]] = []
    for trace, index, name, arguments in calls:
        hits.extend(_rule_hits(trace, index, name, arguments, allowed))
    if hits:
        return {"classification": "invalid_answer_leakage", "valid": False,
                "reason": "runtime_answer_source_or_unapproved_tool_access",
                "trace_files": [str(path.resolve()) for path in authoritative],
                "answer_leakage_hits": hits}
    if consistency_reason is not None:
        return {"classification": "infrastructure_invalid", "valid": False,
                "reason": consistency_reason,
                "trace_files": [str(path.resolve()) for path in authoritative]}
    return {"classification": "trace_policy_passed", "valid": True,
            "reason": None,
            "trace_files": [str(path.resolve()) for path in authoritative]}
