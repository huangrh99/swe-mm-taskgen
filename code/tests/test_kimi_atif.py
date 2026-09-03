from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from report_pipeline.kimi_atif import convert, convert_wire


def record(value: dict) -> str:
    return json.dumps(value) + "\n"


class KimiAtifTests(unittest.TestCase):
    def fixture(self, root: Path) -> Path:
        wire = root / "trial" / "agent" / ".kimi-code" / "sessions" / "session_abc" / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True)
        wire.write_text("".join([
            record({"type": "metadata", "protocol_version": "1.4", "created_at": 1000}),
            record({"type": "config.update", "modelAlias": "kimi-k3", "thinkingEffort": "max", "time": 1000}),
            record({"type": "context.append_message", "time": 1001, "message": {"role": "user", "content": [{"type": "text", "text": "fix it"}], "origin": {"kind": "user"}}}),
            record({"type": "context.append_loop_event", "time": 1002, "event": {"type": "step.begin", "uuid": "s1", "step": 1}}),
            record({"type": "llm.request", "model": "kimi-k3", "time": 1003}),
            record({"type": "context.append_loop_event", "time": 1004, "event": {"type": "content.part", "stepUuid": "s1", "part": {"type": "think", "think": "inspect"}}}),
            record({"type": "context.append_loop_event", "time": 1005, "event": {"type": "tool.call", "uuid": "c-event", "stepUuid": "s1", "toolCallId": "c1", "name": "Bash", "args": {"command": "pwd"}}}),
            record({"type": "context.append_loop_event", "time": 1006, "event": {"type": "tool.result", "parentUuid": "c-event", "toolCallId": "c1", "result": {"output": "/app"}}}),
            record({"type": "context.append_loop_event", "time": 1007, "event": {"type": "content.part", "stepUuid": "s1", "part": {"type": "text", "text": "done"}}}),
            record({"type": "context.append_loop_event", "time": 1008, "event": {"type": "step.end", "uuid": "s1", "finishReason": "stop", "usage": {"inputOther": 10, "inputCacheRead": 20, "inputCacheCreation": 2, "output": 3}}}),
        ]))
        return wire

    def test_converts_to_atif_shape(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            wire = self.fixture(Path(value))
            result = convert_wire(wire)
            self.assertEqual(result["schema_version"], "ATIF-v1.7")
            self.assertEqual(result["session_id"], "abc")
            self.assertEqual([s["source"] for s in result["steps"]], ["user", "agent"])
            agent = result["steps"][1]
            self.assertEqual(agent["tool_calls"][0]["tool_call_id"], "c1")
            self.assertEqual(agent["observation"]["results"][0]["source_call_id"], "c1")
            self.assertEqual(agent["metrics"]["prompt_tokens"], 32)
            self.assertEqual(result["final_metrics"]["total_steps"], 2)

    def test_default_output_and_no_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            self.fixture(root)
            written = convert(root / "trial")
            output = Path(written[0]["output"])
            self.assertEqual(output, (root / "trial" / "agent" / "trajectory.json").resolve())
            self.assertTrue(output.exists())
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                convert(root / "trial")

    def test_reports_corrupt_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            wire = Path(value) / "wire.jsonl"
            wire.write_text('{"type":"metadata","created_at":1.[REDACTED]}\n')
            with self.assertRaisesRegex(ValueError, "broad Harbor redaction"):
                convert_wire(wire)


if __name__ == "__main__":
    unittest.main()
