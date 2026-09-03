"""Normalize Kimi Code ``wire.jsonl`` traces to Harbor's ATIF format.

The native wire file remains the authoritative trace.  This module emits the
loss-minimized, viewer-facing ``agent/trajectory.json`` without importing or
patching Harbor internals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from report_pipeline.atomic import write_json


CONVERTER_VERSION = "kimi-wire-to-atif-v1"


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _message(value: Any) -> str | list[dict[str, Any]]:
    """Convert Kimi content parts while retaining unsupported parts as text."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return _json_text(value)
    converted: list[dict[str, Any]] = []
    for part in value:
        if not isinstance(part, dict):
            converted.append({"type": "text", "text": _json_text(part)})
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            converted.append({"type": "text", "text": part["text"]})
            continue
        # Kimi protocol versions may use image, image_url, or a source object.
        source = part.get("source")
        path = None
        media_type = None
        if isinstance(source, dict):
            path = source.get("path") or source.get("url")
            media_type = source.get("media_type") or source.get("mediaType")
        image_url = part.get("image_url")
        if isinstance(image_url, str):
            path = image_url
        elif isinstance(image_url, dict):
            path = image_url.get("url")
        if isinstance(path, str) and not path.startswith("data:"):
            if media_type not in {
                "image/jpeg", "image/png", "image/gif", "image/webp"
            }:
                suffix = Path(path.split("?", 1)[0]).suffix.lower()
                media_type = {
                    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".png": "image/png", ".gif": "image/gif",
                    ".webp": "image/webp",
                }.get(suffix, "image/png")
            converted.append({
                "type": "image",
                "source": {"media_type": media_type, "path": path},
            })
        else:
            converted.append({
                "type": "text",
                "text": "[unsupported Kimi content part] " + _json_text(part),
            })
    return converted or ""


def _origin_source(origin: Any) -> str:
    if isinstance(origin, dict) and origin.get("kind") == "user":
        return "user"
    return "system"


def _usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    other = int(value.get("inputOther") or 0)
    cache_read = int(value.get("inputCacheRead") or 0)
    cache_create = int(value.get("inputCacheCreation") or 0)
    output = int(value.get("output") or 0)
    metrics = {
        "prompt_tokens": other + cache_read + cache_create,
        "completion_tokens": output,
    }
    if cache_read:
        metrics["cached_tokens"] = cache_read
    return metrics


@dataclass
class _AgentStep:
    uuid: str
    timestamp: str | None
    model_name: str | None = None
    text: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, int] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def atif(self, step_id: int) -> dict[str, Any]:
        message = "\n".join(self.text).strip()
        if not message and self.calls:
            names = ", ".join(call["function_name"] for call in self.calls)
            message = f"[tool call: {names}]"
        if not message:
            message = "[empty response]"
        value: dict[str, Any] = {
            "step_id": step_id,
            "source": "agent",
            "message": message,
            "llm_call_count": 1,
        }
        if self.timestamp:
            value["timestamp"] = self.timestamp
        if self.model_name:
            value["model_name"] = self.model_name
        if self.reasoning:
            value["reasoning_content"] = "\n".join(self.reasoning)
        if self.calls:
            value["tool_calls"] = self.calls
        if self.results:
            value["observation"] = {"results": self.results}
        if self.metrics:
            value["metrics"] = self.metrics
        if self.extra:
            value["extra"] = self.extra
        return value


def read_wire(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                hint = ""
                if "[REDACTED]" in line:
                    hint = "; trace appears corrupted by broad Harbor redaction"
                raise ValueError(
                    f"invalid Kimi wire JSON at {path}:{line_number}: {exc.msg}{hint}"
                ) from exc
            if not isinstance(value, dict) or not isinstance(value.get("type"), str):
                raise ValueError(f"invalid Kimi wire record at {path}:{line_number}")
            records.append(value)
    if not records:
        raise ValueError(f"empty Kimi wire trace: {path}")
    return records


def convert_wire(path: Path) -> dict[str, Any]:
    path = path.resolve(strict=True)
    raw = path.read_bytes()
    records = read_wire(path)
    metadata: dict[str, Any] = {}
    profile = "kimi-code"
    model_name: str | None = None
    thinking_effort: str | None = None
    permission_mode: str | None = None
    tool_definitions: list[dict[str, Any]] | None = None
    steps: list[dict[str, Any]] = []
    active: dict[str, _AgentStep] = {}
    call_parent: dict[str, _AgentStep] = {}
    current: _AgentStep | None = None
    dropped_types: dict[str, int] = {}

    def append_user(record: dict[str, Any]) -> None:
        message = record.get("message")
        if not isinstance(message, dict):
            return
        step: dict[str, Any] = {
            "step_id": len(steps) + 1,
            "source": _origin_source(message.get("origin")),
            "message": _message(message.get("content")),
            "extra": {"kimi_origin": message.get("origin")},
        }
        timestamp = _timestamp(record.get("time"))
        if timestamp:
            step["timestamp"] = timestamp
        steps.append(step)

    for record in records:
        record_type = record["type"]
        if record_type == "metadata":
            metadata = {
                "protocol_version": record.get("protocol_version"),
                "created_at": record.get("created_at"),
            }
        elif record_type == "config.update":
            profile = record.get("profileName") or profile
            model_name = record.get("modelAlias") or model_name
            thinking_effort = record.get("thinkingEffort") or thinking_effort
        elif record_type == "permission.set_mode":
            permission_mode = record.get("mode")
        elif record_type == "llm.tools_snapshot":
            tools = record.get("tools")
            if isinstance(tools, list) and all(isinstance(item, dict) for item in tools):
                tool_definitions = tools
        elif record_type == "llm.request":
            model_name = record.get("model") or model_name
            if current is not None:
                current.model_name = record.get("model") or current.model_name
        elif record_type == "context.append_message":
            append_user(record)
        elif record_type == "context.append_loop_event":
            event = record.get("event")
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type == "step.begin":
                uuid = str(event.get("uuid") or f"step-{len(active) + 1}")
                current = _AgentStep(uuid=uuid, timestamp=_timestamp(record.get("time")),
                                     model_name=model_name)
                active[uuid] = current
            elif event_type == "content.part":
                target = active.get(str(event.get("stepUuid"))) or current
                part = event.get("part")
                if target is not None and isinstance(part, dict):
                    if part.get("type") == "think" and isinstance(part.get("think"), str):
                        target.reasoning.append(part["think"])
                    elif part.get("type") == "text" and isinstance(part.get("text"), str):
                        target.text.append(part["text"])
                    else:
                        target.text.append(_json_text(part))
            elif event_type == "tool.call":
                target = active.get(str(event.get("stepUuid"))) or current
                if target is not None:
                    call_id = str(event.get("toolCallId") or event.get("uuid") or "")
                    arguments = event.get("args")
                    if not isinstance(arguments, dict):
                        arguments = {"input": arguments}
                    target.calls.append({
                        "tool_call_id": call_id,
                        "function_name": str(event.get("name") or "unknown"),
                        "arguments": arguments,
                    })
                    call_parent[str(event.get("uuid"))] = target
            elif event_type == "tool.result":
                target = call_parent.get(str(event.get("parentUuid"))) or current
                if target is not None:
                    result = event.get("result")
                    extra = None
                    if isinstance(result, dict):
                        content = result.get("output")
                        extra_fields = {k: v for k, v in result.items() if k != "output"}
                        extra = extra_fields or None
                    else:
                        content = result
                    observation: dict[str, Any] = {
                        "source_call_id": str(event.get("toolCallId") or ""),
                        "content": _json_text(content),
                    }
                    if extra:
                        observation["extra"] = extra
                    target.results.append(observation)
            elif event_type == "step.end":
                target = active.pop(str(event.get("uuid")), None) or current
                if target is not None:
                    target.metrics = _usage(event.get("usage"))
                    target.extra = {
                        key: event[key]
                        for key in ("finishReason", "llmClientConsumeMs",
                                    "llmFirstTokenLatencyMs", "llmStreamDurationMs")
                        if key in event
                    }
                    steps.append(target.atif(len(steps) + 1))
                    if current is target:
                        current = None
            else:
                dropped_types[str(event_type)] = dropped_types.get(str(event_type), 0) + 1
        elif record_type not in {
            "turn.prompt", "turn.steer", "usage.record", "tools.set_active_tools",
            "tools.update_store",
        }:
            dropped_types[record_type] = dropped_types.get(record_type, 0) + 1

    if active:
        raise ValueError(f"unfinished Kimi steps in {path}: {sorted(active)}")
    if not steps:
        raise ValueError(f"Kimi wire trace contains no convertible steps: {path}")

    prompt = sum(step.get("metrics", {}).get("prompt_tokens", 0) for step in steps)
    completion = sum(step.get("metrics", {}).get("completion_tokens", 0) for step in steps)
    cached = sum(step.get("metrics", {}).get("cached_tokens", 0) for step in steps)
    protocol = str(metadata.get("protocol_version") or "unknown")
    session_id = next((parent.name.removeprefix("session_") for parent in path.parents
                       if parent.name.startswith("session_")), None)
    digest = hashlib.sha256(raw).hexdigest()
    agent: dict[str, Any] = {
        "name": "kimi-code",
        "version": f"wire-protocol-{protocol}",
        "model_name": model_name,
        "extra": {
            "profile": profile,
            "thinking_effort": thinking_effort,
            "permission_mode": permission_mode,
        },
    }
    if tool_definitions:
        agent["tool_definitions"] = tool_definitions
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": session_id,
        "trajectory_id": f"kimi-{digest[:24]}",
        "agent": agent,
        "steps": steps,
        "notes": "Normalized viewer representation; native Kimi wire.jsonl is authoritative.",
        "final_metrics": {
            "total_prompt_tokens": prompt,
            "total_completion_tokens": completion,
            "total_cached_tokens": cached,
            "total_steps": len(steps),
        },
        "extra": {
            "converter": CONVERTER_VERSION,
            "source_wire_sha256": digest,
            "source_record_count": len(records),
            "unmapped_event_counts": dropped_types,
        },
    }


def discover_wires(source: Path) -> list[Path]:
    source = source.resolve(strict=True)
    if source.is_file():
        if source.name != "wire.jsonl":
            raise ValueError(f"expected wire.jsonl, got: {source}")
        return [source]
    wires = sorted(source.glob("**/agents/main/wire.jsonl"))
    if not wires:
        raise ValueError(f"no Kimi agents/main/wire.jsonl below: {source}")
    return wires


def default_output(wire: Path) -> Path:
    for parent in wire.parents:
        if parent.name == "agent" and (parent / ".kimi-code").exists():
            return parent / "trajectory.json"
    raise ValueError(f"cannot locate Harbor agent directory above: {wire}")


def convert(source: Path, output: Path | None = None, *, force: bool = False) -> list[dict[str, Any]]:
    wires = discover_wires(source)
    if output is not None and len(wires) != 1:
        raise ValueError("--output is only valid when exactly one wire trace is selected")
    results = []
    for wire in wires:
        destination = output.resolve() if output else default_output(wire)
        if destination.exists() and not force:
            raise ValueError(f"refusing to overwrite existing ATIF trajectory: {destination}")
        trajectory = convert_wire(wire)
        write_json(destination, trajectory)
        results.append({
            "wire": str(wire),
            "output": str(destination),
            "wire_sha256": trajectory["extra"]["source_wire_sha256"],
            "step_count": len(trajectory["steps"]),
        })
    return results
