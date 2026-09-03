"""Validate and render repository-test-context packets without a model call."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path

from report_pipeline.atomic import write_json


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def render(packet_paths: list[Path], output: Path) -> dict:
    output = output.resolve()
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    output.mkdir(parents=True)
    rows = []
    for path in sorted(item.resolve(strict=True) for item in packet_paths):
        packet = json.loads(path.read_text())
        context = packet.get("repository_test_context", {})
        files = context.get("context_files", [])
        integrity = all(item.get("sha256") == _sha_text(item.get("content", ""))
                        for item in files)
        roles = {}
        for item in files:
            roles[item.get("role", "unknown")] = roles.get(item.get("role", "unknown"), 0) + 1
        completeness = context.get("completeness", {})
        rows.append({
            "task_id": packet.get("task_id"), "packet": str(path),
            "packet_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "status": completeness.get("status"), "integrity": integrity,
            "files": len(files), "bytes": context.get("limits", {}).get("actual_bytes", 0),
            "roles": roles, "commands": context.get("command_evidence", []),
            "blockers": completeness.get("blockers", []),
            "warnings": completeness.get("warnings", []),
            "file_inventory": [{key: item.get(key) for key in
                                ("path", "role", "sha256", "size_bytes", "source",
                                 "base_blob_matches", "requested_by", "dependency_depth")}
                               for item in files],
        })
    audit = {"schema_version": "test-context-audit-v1",
             "created_at": datetime.now(timezone.utc).isoformat(),
             "all_complete": all(row["status"] == "complete" and row["integrity"] for row in rows),
             "rows": rows}
    json_path = output / "20_16_01_test_context_audit.json"
    write_json(json_path, audit)
    cards = []
    for row in rows:
        badge = "ok" if row["status"] == "complete" and row["integrity"] else "bad"
        commands = "".join(
            f"<li><code>{html.escape(str(item.get('command')))}</code> · "
            f"{html.escape(str(item.get('provenance_kind')))}</li>" for item in row["commands"])
        diagnostics = html.escape(json.dumps({"blockers": row["blockers"],
                                               "warnings": row["warnings"]},
                                              ensure_ascii=False, indent=2))
        files = "".join(
            f"<tr><td>{html.escape(str(item['path']))}</td><td>{html.escape(str(item['role']))}</td>"
            f"<td>{item['size_bytes']}</td><td>{html.escape(str(item['source']))}</td>"
            f"<td><code>{str(item['sha256'])[:12]}</code></td></tr>"
            for item in row["file_inventory"])
        cards.append(
            f"<section><h2>{html.escape(str(row['task_id']))} <span class={badge}>{row['status']}</span></h2>"
            f"<p>{row['files']} files · {row['bytes']:,} bytes · hashes {'valid' if row['integrity'] else 'invalid'} · "
            f"roles {html.escape(json.dumps(row['roles'], ensure_ascii=False))}</p><ul>{commands}</ul>"
            f"<details><summary>blockers / warnings</summary><pre>{diagnostics}</pre></details>"
            f"<details><summary>全部输入文件</summary><table><tr><th>path</th><th>role</th><th>bytes</th>"
            f"<th>source</th><th>sha256</th></tr>{files}</table></details></section>")
    page = """<!doctype html><meta charset=utf-8><title>Test context audit</title>
<style>body{font:13px/1.45 system-ui;margin:16px;background:#f5f6f8;color:#202124}main{max-width:1380px;margin:auto}section{background:white;border:1px solid #ddd;border-radius:9px;padding:10px 14px;margin:9px 0}h1,h2{margin:4px 0 8px}.ok,.bad{font-size:12px;padding:2px 7px;border-radius:10px}.ok{color:#116329;background:#dff5e5}.bad{color:#9c1c1c;background:#fde3e3}table{border-collapse:collapse;width:100%;margin-top:8px}th,td{border-bottom:1px solid #eee;padding:5px;text-align:left;vertical-align:top}pre{white-space:pre-wrap;max-height:240px;overflow:auto}code{font-size:12px}</style>
<main><h1>20.16 · Verifier 信息输入完整性</h1><p>模型调用前审计：Base blob、SUT、直接依赖、测试模板、配置、冻结命令及哈希。</p>""" + "".join(cards) + "</main>"
    html_path = output / "20_16_02_test_context_audit.html"
    html_path.write_text(page)
    return {"json": str(json_path), "html": str(html_path),
            "all_complete": audit["all_complete"], "count": len(rows)}

