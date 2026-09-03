"""Render a compact, read-only audit of a V4 test-construction campaign."""

from __future__ import annotations

import html
import json
from pathlib import Path


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _campaign(input_path: Path) -> tuple[Path, list[dict]]:
    source = input_path.resolve(strict=True)
    if source.is_file():
        summary = _read_json(source)
        if not summary or summary.get("schema_version") != "v4-test-construction-campaign-v1":
            raise ValueError("input is not a V4 campaign summary")
        runs = source.parent / "20_17_02_model_runs"
        records = summary.get("records") or []
    elif source.name == "20_17_02_model_runs":
        runs = source
        records = []
    else:
        runs = source / "20_17_02_model_runs"
        summary = _read_json(source / "20_17_08_summary.json")
        records = (summary or {}).get("records") or []
    if not runs.is_dir():
        raise ValueError("20_17_02_model_runs directory is missing")
    by_id = {item.get("case_id"): item for item in records if isinstance(item, dict)}
    case_ids = sorted(set(by_id) | {path.name for path in runs.iterdir() if path.is_dir()})
    if not case_ids:
        raise ValueError("campaign contains no case runs")
    return runs, [dict(by_id.get(case_id, {}), case_id=case_id) for case_id in case_ids]


def _measurement(path: Path | None) -> dict[str, dict]:
    if path is None:
        return {}
    source = path.resolve(strict=True)
    if source.is_dir():
        source = source / "20_19_06_summary.json"
    value = _read_json(source)
    if not value or value.get("schema_version") != "v4-provisional-base-gold-measurement-v1":
        raise ValueError("measurement is not a V4 Base/Gold summary")
    return {item.get("case_id"): item for item in value.get("records", [])
            if isinstance(item, dict) and item.get("case_id")}


def _text(value: object, fallback: str = "—") -> str:
    if value in (None, "", [], {}):
        return fallback
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def _list(values: object) -> str:
    if not isinstance(values, list) or not values:
        return '<span class="muted">—</span>'
    return "<ul>" + "".join(f"<li><code>{html.escape(_text(value))}</code></li>"
                              for value in values) + "</ul>"


def _contracts(values: object) -> str:
    if not isinstance(values, list) or not values:
        return '<p class="muted">No model contract available.</p>'
    rows = []
    for item in values:
        if not isinstance(item, dict):
            continue
        rows.append(
            f'<article><strong>{html.escape(_text(item.get("requirement_id")))}</strong>'
            f'<p>{html.escape(_text(item.get("observable_behavior")))}</p>'
            f'<p><b>Preserve:</b> {html.escape(_text(item.get("preserved_behavior")))}</p>'
            f'<p><b>Oracle:</b> {html.escape(_text(item.get("oracle")))}</p></article>'
        )
    return "".join(rows) or '<p class="muted">No model contract available.</p>'


def _bundle(value: object) -> str:
    if not isinstance(value, dict):
        return '<p class="muted">No generated test bundle.</p>'
    files = value.get("files") or []
    file_rows = "".join(
        f'<tr><td><code>{html.escape(_text(item.get("path")))}</code></td>'
        f'<td>{html.escape(_text(item.get("operation")))}</td></tr>'
        for item in files if isinstance(item, dict)
    ) or '<tr><td colspan="2" class="muted">No files</td></tr>'
    return f"""
      <p><b>Purpose:</b> {html.escape(_text(value.get('functional_oracle_evidence')))}</p>
      <p><b>Collection:</b> {html.escape(_text(value.get('collection_evidence')))}</p>
      <table><thead><tr><th>Generated file</th><th>Operation</th></tr></thead><tbody>{file_rows}</tbody></table>
      <p><b>Command:</b> <code>{html.escape(_text(value.get('test_command')))}</code></p>
      <p><b>Working directory:</b> <code>{html.escape(_text(value.get('working_directory')))}</code></p>
      <p><b>Stable IDs:</b></p>{_list(value.get('stable_test_ids'))}
    """


def _measurement_html(value: dict | None) -> str:
    if not value:
        return '<p class="muted">Not measured. Base/Gold results will appear here when supplied.</p>'
    transitions = value.get("transitions") or []
    rows = "".join(
        f'<tr><td><code>{html.escape(_text(item.get("test_id")))}</code></td>'
        f'<td>{html.escape(_text(item.get("base_status")))}</td>'
        f'<td>{html.escape(_text(item.get("gold_status")))}</td>'
        f'<td>{html.escape(_text(item.get("classification")))}</td></tr>'
        for item in transitions if isinstance(item, dict)
    ) or '<tr><td colspan="4" class="muted">No per-test transitions</td></tr>'
    failure = value.get("reason") or value.get("failure_class") or "—"
    return (f'<p><b>Status:</b> {html.escape(_text(value.get("status")))} · '
            f'<b>Failure:</b> {html.escape(_text(failure))}</p>'
            '<table><thead><tr><th>Test ID</th><th>Base</th><th>Gold</th><th>Transition</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>')


def render(input_path: Path, output: Path, measurement: Path | None = None) -> dict:
    runs, records = _campaign(input_path)
    measurements = _measurement(measurement)
    cards = []
    counts: dict[str, int] = {}
    for record in records:
        case_id = record["case_id"]
        case_root = runs / case_id
        status = _read_json(case_root / "20_17_07_status.json") or record
        packet = _read_json(case_root / "20_17_01_packet.json") or {}
        final = _read_json(case_root / "20_17_06_final.json") or {}
        model_status = status.get("status", "running_or_not_recorded")
        counts[model_status] = counts.get(model_status, 0) + 1
        technical = (status.get("failure_class") or status.get("error") or
                     status.get("reason") or "—")
        observations = final.get("repository_observations") or {}
        cards.append(f"""
        <details class="case" open>
          <summary><strong>{html.escape(case_id)}</strong>
            <span class="pill">{html.escape(_text(packet.get('repository') or record.get('repository')))}</span>
            <span class="pill {html.escape(str(model_status))}">{html.escape(str(model_status))}</span>
            <span class="pill">{html.escape(_text(final.get('status'), 'no model result'))}</span>
          </summary>
          <div class="grid">
            <section><h3>V4 labels</h3><pre>{html.escape(_text(packet.get('v4')))}</pre></section>
            <section><h3>Model / technical status</h3>
              <p><b>Model:</b> {html.escape(_text(status.get('model')))}</p>
              <p><b>Reasoning:</b> {html.escape(_text(status.get('reasoning_effort')))}</p>
              <p><b>Technical failure:</b> {html.escape(_text(technical))}</p>
              <p>{html.escape(_text(final.get('summary')))}</p></section>
          </div>
          <section><h3>Behavioral contract</h3>{_contracts(final.get('behavioral_contract'))}</section>
          <div class="grid"><section><h3>Existing / nearby tests</h3>{_list(observations.get('nearby_test_paths'))}</section>
          <section><h3>Author tests</h3>{_list(observations.get('author_test_paths'))}</section></div>
          <section><h3>Generated tests</h3>{_bundle(final.get('test_bundle'))}</section>
          <section><h3>Base / Gold measurement</h3>{_measurement_html(measurements.get(case_id))}</section>
        </details>""")
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>V4 test campaign audit</title>
<style>
body{{font:14px system-ui;margin:24px;color:#172033;background:#f5f7fb}}main{{max-width:1180px;margin:auto}}
.case,section{{background:white;border:1px solid #dbe1ea;border-radius:10px;padding:12px;margin:12px 0}}
summary{{cursor:pointer;font-size:16px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}}
.pill{{display:inline-block;background:#eef2f7;border-radius:12px;padding:2px 8px;margin-left:6px;font-size:12px}}
.complete,.measured_provisional{{background:#dff6e8;color:#176436}}.technical_failure{{background:#ffe7df;color:#913c20}}
table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #dbe1ea;padding:6px;text-align:left;vertical-align:top}}
th{{background:#f4f6f9}}pre{{white-space:pre-wrap}}code{{overflow-wrap:anywhere}}ul{{margin:6px 0;padding-left:22px}}.muted{{color:#697586}}
</style><main><h1>V4 test campaign audit</h1>
<p>Read-only campaign view. Model proposals are not F2P/P2P until Base/Gold execution.</p>
<p><b>Cases:</b> {len(records)} · <b>Model status:</b> {html.escape(_text(counts))}</p>{''.join(cards)}</main></html>"""
    target = output.resolve()
    if target.suffix.lower() != ".html":
        target.mkdir(parents=True, exist_ok=True)
        target = target / "20_17_09_audit.html"
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(document)
    return {"output": str(target), "case_count": len(records), "counts": counts,
            "measurement_count": len(measurements)}
