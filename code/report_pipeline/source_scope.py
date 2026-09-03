"""Prepare and run a bounded parent-Issue source-scope verifier."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import urllib.error
import urllib.request


PROMPT = Path(__file__).resolve().parents[1] / "analysis/prompts/01_01_source_scope_verifier.system.md"
SCHEMA = Path(__file__).resolve().parents[1] / "analysis/prompts/01_02_source_scope_verifier.schema.json"
PARENT_PATTERNS = (
    re.compile(r"(?i)\bparent\s+issue\s+(?:https://github\.com/([\w.-]+/[\w.-]+)/issues/)?#?(\d+)\b"),
    re.compile(r"(?i)\bsee\s+(?:https://github\.com/([\w.-]+/[\w.-]+)/issues/)?#?(\d+)\s+for\s+acceptance\s+criteria\b"),
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def discover(packet: dict) -> list[dict]:
    """Return explicit one-hop ancestors; never inspect their descendants."""
    found: dict[tuple[str, int], dict] = {}
    default_repo = packet["repository"].lower()
    for source in packet["problem_sources"]:
        if source.get("kind") != "issue" or source.get("field") != "body":
            continue
        for pattern in PARENT_PATTERNS:
            for match in pattern.finditer(source.get("text") or ""):
                repo = (match.group(1) or default_repo).lower()
                number = int(match.group(2))
                key = (repo, number)
                item = found.setdefault(key, {
                    "repository": repo,
                    "issue_number": number,
                    "relation": "acceptance_parent_reference",
                    "ancestor_depth": 1,
                    "expand_descendants": False,
                    "source_ids": [],
                    "matched_text": [],
                })
                if source["source_id"] not in item["source_ids"]:
                    item["source_ids"].append(source["source_id"])
                quote = match.group(0)
                if quote not in item["matched_text"]:
                    item["matched_text"].append(quote)
    return sorted(found.values(), key=lambda item: (item["repository"], item["issue_number"]))


def fetch_issue(repository: str, number: int, token: str | None = None) -> dict:
    endpoint = f"https://api.github.com/repos/{repository}/issues/{number}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "visual-benchmark-source-scope/1",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(endpoint, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = json.loads(response.read(16 * 1024 * 1024))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        raise ValueError(f"parent Issue fetch failed: {type(exc).__name__}") from None
    if data.get("number") != number or "pull_request" in data:
        raise ValueError("parent source is not the requested Issue")
    return {
        "schema_version": "source-scope-issue-snapshot-v1",
        "repository": repository,
        "issue_number": number,
        "url": data.get("html_url"),
        "title": data.get("title") or "",
        "body": data.get("body") or "",
        "state": data.get("state"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "descendants_fetched": False,
        "sub_issues_fetched": False,
    }


def build_packet(dossier_path: Path, test_context_path: Path | None,
                 snapshots: list[dict]) -> dict:
    dossier = json.loads(dossier_path.read_text())
    source_packet_path = Path(dossier["source_bindings"]["packet_path"])
    if _sha(source_packet_path) != dossier["source_bindings"]["packet_sha256"]:
        raise ValueError("direct source packet binding changed")
    source_packet = json.loads(source_packet_path.read_text())
    candidates = discover(source_packet)
    expected = [(item["repository"], item["issue_number"]) for item in candidates]
    observed = [(item.get("repository"), item.get("issue_number")) for item in snapshots]
    if not expected or observed != expected:
        raise ValueError("ancestor snapshots do not match explicit one-hop references")
    tests = None
    if test_context_path:
        tests = json.loads(test_context_path.read_text())
        if tests.get("candidate_id") != dossier["candidate_id"]:
            raise ValueError("test context candidate differs")
    direct_sources = [
        {key: item.get(key) for key in ("source_id", "kind", "field", "relation", "url", "text")}
        for item in source_packet["problem_sources"]
    ]
    return {
        "schema_version": "source-scope-verifier-packet-v1",
        "candidate_id": dossier["candidate_id"],
        "repository": dossier["repository"],
        "selected_pr": {
            "number": dossier["pr_number"], "title": dossier["title"],
            "url": dossier["url"], "changed_files": dossier["changed_files"],
        },
        "direct_issue_sources": direct_sources,
        "ancestor_discovery": candidates,
        "ancestor_issues": snapshots,
        "existing_test_context": tests,
        "traversal_policy": {
            "direction": "ancestors_only", "maximum_depth": 1,
            "expand_descendants": False, "expand_siblings": False,
        },
        "bindings": {
            "dossier_sha256": _sha(dossier_path),
            "direct_source_packet_sha256": _sha(source_packet_path),
            "test_context_sha256": _sha(test_context_path) if test_context_path else None,
        },
    }


def validate(annotation: dict, packet: dict) -> None:
    import jsonschema
    schema = json.loads(SCHEMA.read_text())
    jsonschema.Draft202012Validator(schema).validate(annotation)
    if annotation["candidate_id"] != packet["candidate_id"] or annotation["expand_descendants"]:
        raise ValueError("source-scope output identity or traversal changed")
    expected = [(item["repository"], item["issue_number"]) for item in packet["ancestor_issues"]]
    observed = [(item["repository"], item["issue_number"]) for item in annotation["ancestor_issues"]]
    if observed != expected or not all(item["descendants_excluded"] for item in annotation["ancestor_issues"]):
        raise ValueError("source-scope output changed ancestor inventory")
    for issue in annotation["ancestor_issues"]:
        for requirement in issue["requirements"]:
            if requirement["decision"] == "include_agent_prompt" and not requirement["requires_test_update"] \
                    and requirement["currently_tested"] != "yes":
                raise ValueError("included parent requirement lacks a test binding")


def run(dossier_path: Path, output: Path, test_context_path: Path | None = None,
        evaluator=None, token: str | None = None, timeout: int = 480) -> dict:
    dossier_path, output = dossier_path.resolve(), output.resolve()
    if output.exists():
        raise ValueError("source-scope output directory already exists")
    output.mkdir(parents=True)
    dossier = json.loads(dossier_path.read_text())
    direct_packet = json.loads(Path(dossier["source_bindings"]["packet_path"]).read_text())
    candidates = discover(direct_packet)
    if not candidates:
        raise ValueError("no explicit parent or acceptance Issue reference found")
    snapshots = []
    for index, candidate in enumerate(candidates, 1):
        snapshot = fetch_issue(candidate["repository"], candidate["issue_number"], token)
        snapshots.append(snapshot)
        _write(output / f"18_45_02_parent_issue_{index:04d}_{candidate['issue_number']}.json", snapshot)
    packet = build_packet(dossier_path, test_context_path, snapshots)
    packet_path = output / "18_45_01_source_scope_packet.json"
    _write(packet_path, packet)
    prompt_path = output / "18_45_03_system_prompt.md"
    schema_path = output / "18_45_04_output_schema.json"
    shutil.copyfile(PROMPT, prompt_path)
    shutil.copyfile(SCHEMA, schema_path)
    result = None
    invocation = None
    status = "prepared"
    if evaluator is not None:
        invocation_dir = output / "18_45_06_invocation"
        invocation_dir.mkdir()
        result, invocation = evaluator(
            packet=packet, image_paths=[], system_prompt=prompt_path,
            schema=schema_path, workdir=invocation_dir, timeout=timeout,
        )
        validate(result, packet)
        _write(output / "18_45_07_verifier_result.json", result)
        status = "complete"
    manifest = {
        "schema_version": "source-scope-verifier-run-v1",
        "status": status,
        "candidate_id": dossier["candidate_id"],
        "model_invoked": evaluator is not None,
        "descendants_fetched": False,
        "ancestor_count": len(snapshots),
        "packet": str(packet_path), "packet_sha256": _sha(packet_path),
        "prompt_sha256": _sha(prompt_path), "schema_sha256": _sha(schema_path),
        "result": str(output / "18_45_07_verifier_result.json") if result else None,
        "result_sha256": _sha(output / "18_45_07_verifier_result.json") if result else None,
        "invocation": invocation,
    }
    _write(output / "18_45_08_run_manifest.json", manifest)
    return manifest


def audit(directory: Path, output: Path) -> dict:
    directory, output = directory.resolve(), output.resolve()
    manifest_path = directory / "18_45_08_run_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    packet_path = Path(manifest["packet"])
    if (_sha(packet_path) != manifest["packet_sha256"]
            or _sha(directory / "18_45_03_system_prompt.md") != manifest["prompt_sha256"]
            or _sha(directory / "18_45_04_output_schema.json") != manifest["schema_sha256"]):
        raise ValueError("source-scope frozen input hash changed")
    packet = json.loads(packet_path.read_text())
    if packet["traversal_policy"] != {
        "direction": "ancestors_only", "maximum_depth": 1,
        "expand_descendants": False, "expand_siblings": False,
    }:
        raise ValueError("source-scope traversal policy changed")
    snapshots = sorted(directory.glob("18_45_02_parent_issue_*.json"))
    snapshot_values = [json.loads(path.read_text()) for path in snapshots]
    if snapshot_values != packet["ancestor_issues"] or any(
        value.get("descendants_fetched") is not False
        or value.get("sub_issues_fetched") is not False
        for value in snapshot_values
    ):
        raise ValueError("source-scope snapshot inventory changed or expanded descendants")
    result_path = Path(manifest["result"]) if manifest.get("result") else None
    result = json.loads(result_path.read_text()) if result_path else None
    if result is not None:
        if _sha(result_path) != manifest["result_sha256"]:
            raise ValueError("source-scope result hash changed")
        validate(result, packet)
    request_path = Path((manifest.get("invocation") or {}).get("request", ""))
    request_text = request_path.read_text() if request_path.is_file() else ""
    if re.search(r'(?i)authorization|api[_-]?key', request_text):
        raise ValueError("source-scope request artifact contains a credential field")
    record = {
        "schema_version": "source-scope-verifier-audit-v1",
        "status": "passed",
        "candidate_id": packet["candidate_id"],
        "model_invoked": manifest["model_invoked"],
        "ancestor_inventory": [
            {"repository": item["repository"], "issue_number": item["issue_number"]}
            for item in packet["ancestor_issues"]
        ],
        "maximum_depth": 1,
        "descendants_fetched": False,
        "siblings_fetched": False,
        "result_validated": result is not None,
        "overall_decision": result.get("overall_decision") if result else None,
        "human_review_required": result.get("human_review_required") if result else None,
        "packet_sha256": _sha(packet_path),
        "result_sha256": _sha(result_path) if result_path else None,
        "credential_fields_in_request": False,
    }
    _write(output, record)
    return record


def render(directory: Path, output: Path) -> Path:
    directory, output = directory.resolve(), output.resolve()
    manifest = json.loads((directory / "18_45_08_run_manifest.json").read_text())
    packet = json.loads(Path(manifest["packet"]).read_text())
    result = json.loads(Path(manifest["result"]).read_text())
    validate(result, packet)
    issues = {item["issue_number"]: item for item in packet["ancestor_issues"]}
    cards = []
    for issue in result["ancestor_issues"]:
        source = issues[issue["issue_number"]]
        rows = []
        for requirement in issue["requirements"]:
            signals = (
                f'new={requirement["new_information"]} · '
                f'patch={requirement["patch_relevant"]} · '
                f'executable={requirement["executable"]} · '
                f'tested={requirement["currently_tested"]}'
            )
            rows.append(
                "<tr><td><b>" + html.escape(requirement["requirement"]) + "</b>"
                "<blockquote>" + html.escape(requirement["source_quote"]) + "</blockquote></td>"
                "<td><code>" + html.escape(signals) + "</code></td>"
                "<td><span class='pill'>" + html.escape(requirement["decision"]) + "</span>"
                "<br>" + html.escape(requirement["reason"]) + "</td></tr>"
            )
        cards.append(
            "<section><h2><a href='" + html.escape(source["url"]) + "'>Issue #" +
            str(issue["issue_number"]) + "</a> · " + html.escape(source["title"]) + "</h2>"
            "<p><b>父级结论：</b>" + html.escape(issue["overall_decision"]) +
            "　<b>子项展开：</b>禁止　<b>理由：</b>" + html.escape(issue["reason"]) + "</p>"
            "<table><thead><tr><th>原子要求与原文</th><th>判断信号</th><th>结论与原因</th></tr></thead>"
            "<tbody>" + "".join(rows) + "</tbody></table></section>"
        )
    invocation = manifest.get("invocation") or {}
    page = f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{html.escape(packet['candidate_id'])} · Parent Issue 范围判定</title>
<style>
:root{{--bg:#f5f6f8;--panel:#fff;--ink:#17191c;--muted:#626971;--line:#d8dce1;--accent:#14634f}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:13px/1.45 system-ui,sans-serif}}
main{{max-width:1280px;margin:auto;padding:16px}}header,section{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px;margin-bottom:12px}}
h1{{font-size:20px;margin:0 0 8px}}h2{{font-size:16px;margin:0 0 6px}}a{{color:var(--accent)}}.meta{{display:flex;gap:8px;flex-wrap:wrap}}.pill{{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:2px 7px}}table{{border-collapse:collapse;width:100%}}th,td{{text-align:left;vertical-align:top;padding:8px;border-top:1px solid var(--line)}}th:nth-child(1){{width:42%}}th:nth-child(2){{width:25%}}blockquote{{margin:6px 0 0;padding-left:8px;border-left:3px solid var(--line);color:var(--muted)}}code{{overflow-wrap:anywhere}}.note{{color:var(--muted)}}
</style></head><body><main><header><h1>{html.escape(packet['candidate_id'])} · Parent Issue 范围判定</h1>
<div class='meta'><span class='pill'>整体：{html.escape(result['overall_decision'])}</span><span class='pill'>置信度：{html.escape(result['confidence'])}</span><span class='pill'>人工复核：{'需要' if result['human_review_required'] else '不需要'}</span><span class='pill'>模型：{html.escape(str(invocation.get('model') or 'unknown'))}</span></div>
<p class='note'>只沿明确引用向上读取一跳祖先；未抓取 parent 的 descendants、sub-issues 或 siblings。模型判断不是人工确认。</p></header>
{''.join(cards)}
<section><b>冻结证据：</b> <a href='{Path(manifest['result']).resolve().as_uri()}'>结构化结果</a> · <a href='{(directory / '18_45_09_audit.json').resolve().as_uri()}'>审计记录</a> · <a href='{Path(manifest['packet']).resolve().as_uri()}'>输入包</a></section>
</main></body></html>"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page)
    return output
