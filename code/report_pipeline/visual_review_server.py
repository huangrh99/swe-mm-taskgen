"""Live manifest-backed server for the one-case visual human-review UI."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import parse_qs, quote, unquote, urlparse

from report_pipeline.paths import WORKSPACE_ROOT
from report_pipeline.visual_gate_ui import (
    SCHEMA as VISUAL_GATE_SCHEMA,
    audit,
    render,
    translation_bindings,
    validate_human_export,
)


MAX_DECISION_BYTES = 4 * 1024 * 1024
CASE_ID = re.compile(r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[1-9][0-9]*$")
JOB_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
STANDALONE_SCHEMA = "self-contained-visual-review-v1"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _workspace_path(value: str) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else WORKSPACE_ROOT / path).resolve(strict=True)


class ReviewState:
    def __init__(self, config_path: Path, state_root: Path):
        self.config_path = config_path.resolve(strict=True)
        self.state_root = state_root.resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        if self.state_root.is_symlink():
            raise ValueError("review server state root must not be a symlink")
        self.translation_lock = threading.Lock()
        self.test_coverage_lock = threading.Lock()
        coverage = self._config().get("test_coverage_config")
        self.test_coverage_slots = threading.BoundedSemaphore(
            int((coverage or {}).get("max_concurrency", 4)))

    def _config(self) -> dict:
        value = json.loads(self.config_path.read_text())
        if value.get("schema_version") != "visual-review-live-config-v1":
            raise ValueError("unsupported live review config")
        standalone = value.get("standalone_bundle")
        if standalone is not None:
            standalone = _workspace_path(str(standalone))
            if not standalone.is_dir() or standalone.is_symlink():
                raise ValueError("standalone review bundle must be a real directory")
            distribution = None
        else:
            distribution = _workspace_path(value.get("distribution", ""))
            if distribution.is_symlink():
                raise ValueError("distribution manifest must not be a symlink")
        translation = value.get("translation")
        if translation is not None:
            if not isinstance(translation, dict):
                raise ValueError("translation config must be an object")
            required = {"backend", "key_file"}
            if not required.issubset(translation):
                raise ValueError("translation config lacks backend or key_file")
            key_file = _workspace_path(str(translation["key_file"]))
            translation = {**translation, "key_file_path": key_file}
        coverage = value.get("test_coverage")
        if coverage is not None:
            if not isinstance(coverage, dict) or not isinstance(coverage.get("cases"), dict):
                raise ValueError("test_coverage config must contain a cases object")
            if not {"backend", "key_file"}.issubset(coverage):
                raise ValueError("test_coverage config lacks backend or key_file")
            key_file = _workspace_path(str(coverage["key_file"]))
            concurrency = int(coverage.get("max_concurrency", 4))
            if not 1 <= concurrency <= 32:
                raise ValueError("test_coverage max_concurrency must be between 1 and 32")
            coverage = {**coverage, "key_file_path": key_file,
                        "max_concurrency": concurrency}
        evidence_root = value.get("test_evidence_root")
        if evidence_root is not None:
            evidence_root = _workspace_path(str(evidence_root))
            if not evidence_root.is_dir() or evidence_root.is_symlink():
                raise ValueError("test evidence root must be a real directory")
        return {**value, "distribution_path": distribution,
                "standalone_bundle_path": standalone,
                "translation_config": translation,
                "test_coverage_config": coverage,
                "test_evidence_root_path": evidence_root}

    @staticmethod
    def _standalone_bundle(bundle: Path) -> tuple[dict, dict, Path]:
        manifest_path = bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("schema_version") != STANDALONE_SCHEMA:
            raise ValueError("unsupported standalone visual-review manifest")
        entrypoint = bundle / str(manifest.get("entrypoint", ""))
        metadata_path = bundle / str(manifest.get("metadata", ""))
        if (_sha(entrypoint) != manifest.get("entrypoint_sha256")
                or _sha(metadata_path) != manifest.get("metadata_sha256")):
            raise ValueError("standalone visual-review binding changed")
        payload = json.loads(metadata_path.read_text())
        cases = payload.get("cases") or []
        case_ids = [item.get("case_id") for item in cases]
        if (len(cases) != manifest.get("candidate_count")
                or len(case_ids) != len(set(case_ids))
                or payload.get("source_manifest_sha256")
                != manifest.get("source_manifest_sha256")):
            raise ValueError("standalone visual-review inventory changed")
        asset_count = 0
        for case in cases:
            for asset in case.get("assets") or []:
                relative = Path(str(asset.get("path", "")))
                target = (bundle / relative).resolve(strict=True)
                if (relative.is_absolute() or not target.is_relative_to(bundle.resolve())
                        or not target.is_file() or target.is_symlink()
                        or _sha(target) != asset.get("sha256")):
                    raise ValueError("standalone visual-review asset binding changed")
                asset_count += 1
        if asset_count != manifest.get("asset_file_count"):
            raise ValueError("standalone visual-review asset count changed")
        return manifest, payload, entrypoint

    def _validate_human_export(self, bundle: Path, decisions: Path) -> dict:
        config = self._config()
        if config.get("standalone_bundle_path") is None:
            return validate_human_export(bundle, decisions)
        import jsonschema

        manifest, payload, _ = self._standalone_bundle(bundle)
        value = json.loads(decisions.resolve(strict=True).read_text())
        schema = WORKSPACE_ROOT / "schemas/visual_gate_review_v1.schema.json"
        jsonschema.validate(value, json.loads(schema.read_text()))
        if value["source_manifest_sha256"] != manifest["source_manifest_sha256"]:
            raise ValueError("human export belongs to a different visual-gate manifest")
        by_case = {item["case_id"]: item for item in payload["cases"]}
        seen = set()
        counts = {"keep": 0, "exclude": 0, "needs_review": 0}
        for row in value["rows"]:
            case = by_case.get(row["case_id"])
            if case is None or row["case_id"] in seen:
                raise ValueError("human export contains unknown or duplicate case")
            seen.add(row["case_id"])
            if (row["candidate_binding_sha256"] != case["candidate_binding_sha256"]
                    or row["source_route"] != case["source_route"]):
                raise ValueError("human export candidate binding changed")
            expected_assets = [item["asset_id"] for item in case["assets"]]
            if [item["asset_id"] for item in row["images"]] != expected_assets:
                raise ValueError("human export image inventory changed")
            if row["decision"] == "keep":
                visible = [item for item in row["images"] if item["solver_visible"]]
                if (not row["problem_statement_leak_free"]
                        or row["text_only_sufficient"] != "no"
                        or row["ocr_replaceable"] != "no"
                        or not row["non_text_visual_fact"].strip() or not visible):
                    raise ValueError("kept visual candidate did not pass necessity/leakage fields")
                for item in visible:
                    if (item["role"] in {"after_only", "before_after_composite", "unclear"}
                            or item["contains_fixed_after"]
                            or item["contains_solution_evidence"] or item["crop_required"]):
                        raise ValueError("kept solver-visible image is unsafe or unresolved")
            counts[row["decision"]] += 1
        return {"schema_version": "visual-gate-human-export-audit-v1",
                "status": "passed", "reviewed_count": len(seen),
                "unreviewed_count": len(by_case) - len(seen), "counts": counts,
                "decisions_sha256": _sha(decisions)}

    def _decision_for_case(self, bundle: Path, manifest: dict,
                           case_id: str) -> tuple[Path | None, dict | None]:
        decisions = self.state_root / "16_04_06_human_decisions"
        if not decisions.is_dir():
            return None, None
        for path in sorted(decisions.glob("16_04_06_decisions_*.json"), reverse=True):
            try:
                value = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if value.get("source_manifest_sha256") != manifest["source_manifest_sha256"]:
                continue
            rows = [row for row in value.get("rows", []) if row.get("case_id") == case_id]
            if not rows:
                continue
            # Once the newest matching record is found, a changed or malformed
            # file must block authorization rather than falling back to an older keep.
            self._validate_human_export(bundle, path)
            return path, rows[0]
        return None, None

    def _coverage_jobs_root(self) -> Path:
        root = self.state_root / "20_11_test_coverage_jobs"
        root.mkdir(exist_ok=True)
        if root.is_symlink():
            raise ValueError("test coverage jobs root must not be a symlink")
        return root

    def _write_coverage_job(self, job: dict) -> None:
        path = self._coverage_jobs_root() / job["job_id"] / "status.json"
        path.parent.mkdir(exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".status_", suffix=".json", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(job, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _latest_coverage_job(self, case_id: str) -> dict | None:
        paths = sorted(self._coverage_jobs_root().glob("*/status.json"), reverse=True)
        for path in paths:
            try:
                value = json.loads(path.read_text())
                if value.get("case_id") == case_id:
                    return value
            except (OSError, json.JSONDecodeError):
                continue
        return None

    def test_coverage_status(self, case_id: str) -> dict:
        if not CASE_ID.fullmatch(case_id):
            raise ValueError("invalid test coverage case identity")
        bundle, manifest, payload, _ = self.bundle()
        config = self._config().get("test_coverage_config")
        configured = bool(config and case_id in config["cases"])
        decision_path, row = self._decision_for_case(bundle, manifest, case_id)
        eligible = bool(row and row.get("decision") == "keep")
        case = next((item for item in payload["cases"] if item["case_id"] == case_id), None)
        if case is None:
            raise ValueError("test coverage case is not in the active manifest")
        latest = self._latest_coverage_job(case_id)
        return {
            "case_id": case_id,
            "status": ((latest or {}).get("status") if latest else
                       ("ready" if configured and eligible else
                        "test_inputs_missing" if not configured else "ready_provisional")),
            "test_inputs_configured": configured,
            "eligible": eligible,
            "visual_gate_approved": eligible,
            "decision_sha256": _sha(decision_path) if decision_path else None,
            "job": latest,
            "boundary": "Verifier output is a coverage proposal; final F2P/P2P requires execution.",
        }

    def _archived_test_evidence(self, case_id: str) -> tuple[Path, dict, dict] | None:
        root = self._config().get("test_evidence_root_path")
        if root is None:
            return None
        case_root = (root / case_id).resolve()
        if not case_root.is_dir() or not case_root.is_relative_to(root):
            return None
        metadata_root = case_root / "meta"
        if not metadata_root.is_dir():
            metadata_root = case_root
        runs = sorted((metadata_root / "03_test_construction").glob(
            "20_11_verifier_run_*/20_11_09_manifest.json"), reverse=True)
        from report_pipeline.test_review_ui import _load_case

        for manifest_path in runs:
            try:
                evidence = _load_case(manifest_path.parent)
                case_manifest_path = metadata_root / "00_case_manifest.json"
                case_manifest = (json.loads(case_manifest_path.read_text())
                                 if case_manifest_path.is_file() else {})
                return manifest_path.parent, evidence, case_manifest
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
        return None

    def _live_job_test_evidence(self, case_id: str) -> tuple[Path, dict, dict] | None:
        job = self._latest_coverage_job(case_id)
        if not job or job.get("status") != "complete" or not job.get("output"):
            return None
        root = self._coverage_jobs_root().resolve()
        output = Path(job["output"]).resolve()
        if (not output.is_dir() or not output.is_relative_to(root)
                or not (output / "20_11_09_manifest.json").is_file()):
            return None
        from report_pipeline.test_review_ui import _load_case

        try:
            return output, _load_case(output), {}
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def test_flow_evidence(self, case_id: str) -> dict:
        status = self.test_coverage_status(case_id)
        live = self._live_job_test_evidence(case_id)
        resolved = live or self._archived_test_evidence(case_id)
        if resolved is None:
            return {
                **status,
                "evidence_available": False,
                "pipeline": {
                    "candidate": "complete",
                    "visual_approved": "complete" if status["visual_gate_approved"] else "pending",
                    "tests_measured": "not_started",
                    "tests_approved": "not_started",
                    "harbor_controls_passed": "not_started",
                    "frozen": "not_started",
                    "pass5_completed": "not_started",
                },
            }
        directory, evidence, case_manifest = resolved
        pipeline_status = case_manifest.get("pipeline_status") or {}
        measurement = evidence["measurement"]
        measured = bool(measurement["transitions"])
        harbor_values = [pipeline_status.get("harbor_empty"),
                         pipeline_status.get("harbor_oracle")]
        harbor_complete = all(value in {"complete", "passed", "pass"}
                              for value in harbor_values)
        state = case_manifest.get("state", "candidate")
        sections = case_manifest.get("sections") or {}
        artifact_groups = {
            name: [{
                "path": item.get("path"),
                "sha256": item.get("sha256"),
                "size_bytes": item.get("size_bytes"),
                "storage": item.get("storage"),
                "url": (f"/case-evidence/{case_id}/"
                        + quote(str(item.get("path", "")), safe="/")),
            } for item in sections.get(name, [])]
            for name in ("test_construction", "measurements", "harbor", "frozen", "pass5")
            if sections.get(name)
        }
        return {
            **status,
            "evidence_available": True,
            "evidence_source": "live_job" if live else "case_archive",
            "evidence_directory": str(directory),
            "verifier": {
                "status": evidence["verifier_status"],
                "summary": evidence["verifier_summary"],
                "constraints": evidence["constraints"],
                "repository_context_files": evidence.get(
                    "repository_context_files", evidence.get("existing_test_files", [])),
                "pr_author_test_files": evidence.get("pr_author_test_files", []),
                "verifier_generated_test_files": evidence.get(
                    "verifier_generated_test_files",
                    evidence.get("pre_verifier_generated_test_files", [])),
                "test_semantics": evidence.get("test_semantics", []),
                "generated_bundles": evidence["bundles"],
            },
            "measurement": measurement,
            "archive_pipeline_status": pipeline_status,
            "archive_blockers": case_manifest.get("blockers") or [],
            "archive_artifacts": artifact_groups,
            "pipeline": {
                "candidate": "complete",
                "visual_approved": "complete" if status["visual_gate_approved"] else "pending",
                "tests_measured": "complete" if measured else "not_started",
                "tests_approved": ("complete" if measurement["approval_eligible"]
                                   else "blocked" if measured else "not_started"),
                "harbor_controls_passed": "complete" if harbor_complete else "not_started",
                "frozen": "complete" if state in {"frozen", "pass5_completed"} else "not_started",
                "pass5_completed": "complete" if state == "pass5_completed" else "not_started",
            },
        }

    def case_artifact(self, case_id: str, relative: str) -> Path:
        if not CASE_ID.fullmatch(case_id):
            raise FileNotFoundError(relative)
        root = self._config().get("test_evidence_root_path")
        if root is None:
            raise FileNotFoundError(relative)
        case_root = (root / case_id).resolve(strict=True)
        metadata_root = case_root / "meta"
        if not (metadata_root / "00_case_manifest.json").is_file():
            metadata_root = case_root
        manifest_path = metadata_root / "00_case_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        registered = {
            item.get("path"): item
            for name in ("test_construction", "measurements", "harbor", "frozen", "pass5")
            for item in (manifest.get("sections") or {}).get(name, [])
        }
        binding = registered.get(relative)
        if not binding:
            raise FileNotFoundError(relative)
        target = ((case_root / relative.removeprefix("@task/"))
                  if relative.startswith("@task/") else (metadata_root / relative)).resolve(strict=True)
        if (not target.is_file() or target.is_symlink()
                or not (target.is_relative_to(metadata_root) or target.is_relative_to(case_root))
                or _sha(target) != binding.get("sha256")):
            raise FileNotFoundError(relative)
        return target

    def _run_test_coverage_job(self, job_id: str, evaluator_factory=None,
                               verifier_runner=None) -> None:
        status_path = self._coverage_jobs_root() / job_id / "status.json"
        with self.test_coverage_slots:
            job = json.loads(status_path.read_text())
            job.update(status="running", started_at=datetime.now(timezone.utc).isoformat())
            self._write_coverage_job(job)
            try:
                from pr_crawler.api_engines import ApiEvaluator
                from report_pipeline import test_extension_verifier

                config = self._config()["test_coverage_config"]
                case_config = config["cases"][job["case_id"]]
                evaluator = (evaluator_factory(config) if evaluator_factory else ApiEvaluator(
                    config["backend"], model=config.get("model"),
                    key_file=config["key_file_path"], attempts=int(config.get("attempts", 2)),
                    min_interval=float(config.get("min_interval", 1.0)),
                    max_tokens=int(config.get("max_tokens", 32768)),
                    cooldown_path=self._coverage_jobs_root() / "cooldown.json"))
                runner = verifier_runner or (
                    test_extension_verifier.run_harbor
                    if case_config.get("mode") == "harbor" else test_extension_verifier.run)
                classification = _workspace_path(job["classification"])
                common = {
                    "human_review": Path(job["decision_path"]),
                    "classification": classification,
                    "output": Path(job["output"]), "evaluator": evaluator,
                    "timeout": int(config.get("timeout", 480)),
                    "provisional": bool(job.get("provisional")),
                }
                if case_config.get("mode") == "harbor":
                    manifest = runner(
                        task=_workspace_path(case_config["task"]),
                        source_measurement=_workspace_path(case_config["source_measurement"]),
                        browser_measurement=_workspace_path(case_config["browser_measurement"]),
                        **common)
                else:
                    manifest = runner(
                        case=_workspace_path(case_config["case"]),
                        transition_audit=_workspace_path(case_config["transition_audit"]),
                        **common)
                audit_path = Path(manifest["audit"]["path"]).resolve(strict=True)
                if not audit_path.is_relative_to(Path(job["output"]).resolve()):
                    raise ValueError("test coverage audit escaped its job output")
                review_url = None
                required = ["20_11_01_packet.json", "20_11_06_result.json",
                            "20_11_09_manifest.json"]
                if all((Path(job["output"]) / name).is_file() for name in required):
                    from report_pipeline.test_review_ui import render as render_test_review
                    review = render_test_review(
                        Path(job["output"]), Path(job["output"]) / "20_12_review")
                    review_path = Path(review["page"]["path"]).resolve(strict=True)
                    if not review_path.is_relative_to(Path(job["output"]).resolve()):
                        raise ValueError("test review page escaped its job output")
                    review_url = (f"/test-coverage/{job_id}/"
                                  "20_12_review/20_12_02_test_review.html")
                job.update(status=manifest["status"],
                           finished_at=datetime.now(timezone.utc).isoformat(),
                           manifest_path=str(Path(job["output"]) / "20_11_09_manifest.json"),
                           verifier_audit_url=(
                               f"/test-coverage/{job_id}/20_11_08_audit.html"),
                           audit_url=(review_url or
                                      f"/test-coverage/{job_id}/20_11_08_audit.html"))
            except Exception as exc:
                job.update(status="failed", finished_at=datetime.now(timezone.utc).isoformat(),
                           error=f"{type(exc).__name__}: {str(exc)[:1200]}")
            self._write_coverage_job(job)

    def trigger_test_coverage(self, value: object, *, launch: bool = True,
                              evaluator_factory=None, verifier_runner=None) -> dict:
        if not isinstance(value, dict) or set(value) != {"source_manifest_sha256", "case_id"}:
            raise ValueError("test coverage request must contain manifest hash and case_id")
        case_id = str(value["case_id"])
        if not CASE_ID.fullmatch(case_id):
            raise ValueError("invalid test coverage case identity")
        with self.test_coverage_lock:
            bundle, manifest, payload, _ = self.bundle()
            if value["source_manifest_sha256"] != manifest["source_manifest_sha256"]:
                raise ValueError("test coverage request belongs to another manifest")
            config = self._config().get("test_coverage_config")
            if not config or case_id not in config["cases"]:
                raise ValueError("test_inputs_missing")
            case = next(item for item in payload["cases"] if item["case_id"] == case_id)
            decision_path, row = self._decision_for_case(bundle, manifest, case_id)
            if row and row.get("candidate_binding_sha256") != case["candidate_binding_sha256"]:
                raise ValueError("saved visual decision binding changed")
            approved = bool(row and row.get("decision") == "keep")
            if not approved:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                provisional_root = self._coverage_jobs_root() / (
                    f"{stamp}_{case_id}_provisional")
                decision_path = provisional_root / "provisional_visual_input.json"
                provisional_row = row or {
                    "case_id": case_id,
                    "candidate_binding_sha256": case["candidate_binding_sha256"],
                    "source_route": case["source_route"],
                    "problem_statement": case["problem_statement"],
                    "problem_statement_leak_free": False,
                    "text_only_sufficient": "unclear",
                    "ocr_replaceable": "unclear",
                    "non_text_visual_fact": "",
                    "images": [{
                        "asset_id": asset["asset_id"],
                        "role": asset.get("v3_suggestion", {}).get(
                            "seed_temporal_role", "unclear"),
                        "solver_visible": True,
                        "contains_fixed_after": False,
                        "contains_solution_evidence": False,
                        "crop_required": False,
                        "reason": "unreviewed provisional input",
                    } for asset in case["assets"]],
                    "decision": "needs_review",
                    "reason": "Test planning requested before visual human approval.",
                    "reviewed_at": "",
                }
                decision_path.parent.mkdir(parents=True, exist_ok=False)
                decision_path.write_text(json.dumps({
                    "schema_version": "visual-gate-provisional-input-v1",
                    "source_manifest_sha256": manifest["source_manifest_sha256"],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "rows": [provisional_row],
                    "boundary": "This snapshot is not a human visual approval.",
                }, ensure_ascii=False, indent=2) + "\n")
            previous = self._latest_coverage_job(case_id)
            decision_sha = _sha(decision_path)
            if (previous and previous.get("decision_sha256") == decision_sha
                    and previous.get("status") in {"prepared", "running"}):
                return previous
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            job_id = f"{stamp}_{case_id}_{decision_sha[:12]}"
            root = self._coverage_jobs_root() / job_id
            output = root / "output"
            job = {
                "schema_version": "visual-review-test-coverage-job-v1",
                "job_id": job_id, "case_id": case_id, "status": "prepared",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_manifest_sha256": manifest["source_manifest_sha256"],
                "decision_path": str(decision_path.resolve()),
                "decision_sha256": decision_sha,
                "provisional": not approved,
                "visual_gate_status": "approved" if approved else "pending",
                "classification": case["source_bindings"]["classification"],
                "output": str(output.resolve()),
                "audit_url": None,
                "boundary": ("pre-human-review coverage planning only; predicted transitions "
                             "are never final F2P/P2P labels" if not approved else
                             "predicted transitions are never final F2P/P2P labels"),
            }
            self._write_coverage_job(job)
            if launch:
                threading.Thread(target=self._run_test_coverage_job,
                                 args=(job_id,), daemon=True).start()
            else:
                self._run_test_coverage_job(job_id, evaluator_factory, verifier_runner)
            return json.loads((root / "status.json").read_text())

    def _translation_cache_path(self, source_manifest_sha256: str,
                                case_id: str) -> Path:
        if (not re.fullmatch(r"[0-9a-f]{64}", source_manifest_sha256)
                or not CASE_ID.fullmatch(case_id)):
            raise ValueError("invalid translation cache identity")
        root = self.state_root / "16_04_07_on_demand_translations" / source_manifest_sha256
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink():
            raise ValueError("translation cache root must not be a symlink")
        return root / f"{case_id}.json"

    @staticmethod
    def _translation_source(case: dict) -> dict:
        return {
            "case_id": case["case_id"],
            "pr_title": case["pr_title"],
            "problem_statement": case["problem_statement"],
        }

    def _apply_cached_translations(self, payload: dict) -> int:
        from analysis.scripts.step_16_04_translate_human_review import text_digest

        count = 0
        source_manifest = payload["source_manifest_sha256"]
        for case in payload["cases"]:
            path = self._translation_cache_path(source_manifest, case["case_id"])
            if not path.is_file():
                continue
            value = json.loads(path.read_text())
            source = self._translation_source(case)
            if (value.get("schema_version") != "visual-review-on-demand-translation-v1"
                    or value.get("source_manifest_sha256") != source_manifest
                    or value.get("candidate_binding_sha256")
                    != case["candidate_binding_sha256"]
                    or value.get("source_text_sha256") != text_digest(source)):
                raise ValueError(f"cached translation binding changed: {case['case_id']}")
            case["pr_title_zh"] = value["pr_title_zh"]
            case["problem_statement_zh"] = value["problem_statement_zh"]
            case["translation"] = {
                "status": "available", "curator_only": True,
                "source": "on_demand_cache", "file": str(path),
                "file_sha256": _sha(path),
                "source_text_sha256": value["source_text_sha256"],
            }
            count += 1
        return count

    def bundle(self) -> tuple[Path, dict, dict, Path]:
        config = self._config()
        standalone = config.get("standalone_bundle_path")
        if standalone is not None:
            portable, payload, _ = self._standalone_bundle(standalone)
            categories: dict[str, int] = {}
            for case in payload["cases"]:
                for capability in case.get("v4", {}).get("visual_capabilities", []):
                    label = capability.get("category")
                    if label:
                        categories[label] = categories.get(label, 0) + 1
            manifest = {
                "source_manifest_sha256": portable["source_manifest_sha256"],
                "candidate_count": portable["candidate_count"],
                "category_counts": categories,
                "case_ids": [case["case_id"] for case in payload["cases"]],
            }
            cached_translation_count = self._apply_cached_translations(payload)
            server_decision_count = 0
            for case in payload["cases"]:
                decision_path, decision = self._decision_for_case(
                    standalone, manifest, case["case_id"])
                if decision is None:
                    continue
                case["server_decision"] = decision
                case["server_decision_sha256"] = _sha(decision_path)
                server_decision_count += 1
            payload["live_meta"] = {
                "distribution_path": str(standalone.relative_to(WORKSPACE_ROOT)),
                "distribution_sha256": portable["metadata_sha256"],
                "loaded_at": datetime.now(timezone.utc).isoformat(),
                "qualified_count": portable["candidate_count"],
                "category_counts": categories,
                "gate_passed": False,
                "bundle_manifest_sha256": _sha(standalone / "manifest.json"),
                "on_demand_translation_count": cached_translation_count,
                "server_decision_count": server_decision_count,
            }
            return standalone, manifest, payload, standalone / portable["metadata"]
        distribution = config["distribution_path"]
        distribution_sha = _sha(distribution)
        cache_root = self.state_root / "16_04_02_bundle_cache"
        cache_root.mkdir(exist_ok=True)
        renderer = Path(render.__code__.co_filename).resolve(strict=True)
        translations = translation_bindings(distribution)
        translation_contract = hashlib.sha256(json.dumps(
            translations, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        render_contract = hashlib.sha256(
            f"{distribution_sha}:{translation_contract}:{_sha(VISUAL_GATE_SCHEMA)}:{_sha(renderer)}".encode()
        ).hexdigest()[:16]
        bundle = cache_root / f"{distribution_sha}-{render_contract}"
        prebuilt = config.get("prebuilt_bundle")
        if not bundle.exists() and prebuilt:
            candidate = _workspace_path(prebuilt)
            try:
                audit(candidate)
                candidate_manifest = json.loads(
                    (candidate / "16_04_04_review_manifest.json").read_text())
                if (candidate_manifest.get("distribution", {}).get("sha256") == distribution_sha
                        and candidate_manifest.get("schema_sha256") == _sha(VISUAL_GATE_SCHEMA)
                        and candidate_manifest.get("runner_sha256") == _sha(renderer)
                        and candidate_manifest.get("translations", []) == translations):
                    bundle = candidate
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                # A stale prebuilt bundle is only an optimization hint.  The
                # configured distribution remains authoritative and is rebuilt.
                pass
        if not bundle.exists():
            render(distribution, bundle)
        audit_result = audit(bundle)
        manifest = json.loads((bundle / "16_04_04_review_manifest.json").read_text())
        payload = json.loads((bundle / "16_04_01_review_payload.json").read_text())
        if manifest.get("distribution", {}).get("sha256") != distribution_sha:
            raise ValueError("review bundle does not match configured distribution")
        cached_translation_count = self._apply_cached_translations(payload)
        server_decision_count = 0
        for case in payload["cases"]:
            decision_path, decision = self._decision_for_case(
                bundle, manifest, case["case_id"])
            if decision is None:
                continue
            case["server_decision"] = decision
            case["server_decision_sha256"] = _sha(decision_path)
            server_decision_count += 1
        payload["live_meta"] = {
            "distribution_path": str(distribution),
            "distribution_sha256": distribution_sha,
            "loaded_at": datetime.now(timezone.utc).isoformat(),
            "qualified_count": manifest["candidate_count"],
            "category_counts": manifest["category_counts"],
            "gate_passed": json.loads(distribution.read_text()).get("gate_passed") is True,
            "bundle_manifest_sha256": audit_result["manifest_sha256"],
            "on_demand_translation_count": cached_translation_count,
            "server_decision_count": server_decision_count,
        }
        return bundle, manifest, payload, distribution

    def translate(self, value: object, evaluator=None) -> dict:
        from analysis.scripts.step_16_04_translate_human_review import (
            PROMPT, SCHEMA, text_digest, validate,
        )
        from pr_crawler.api_engines import ApiEvaluator

        if not isinstance(value, dict) or set(value) != {
                "source_manifest_sha256", "case_id"}:
            raise ValueError("translation request must contain manifest hash and case_id")
        with self.translation_lock:
            _, _, payload, _ = self.bundle()
            if value["source_manifest_sha256"] != payload["source_manifest_sha256"]:
                raise ValueError("translation request belongs to another manifest")
            matches = [case for case in payload["cases"]
                       if case["case_id"] == value["case_id"]]
            if len(matches) != 1:
                raise ValueError("translation case is not in the active manifest")
            case = matches[0]
            if case.get("problem_statement_zh"):
                return {
                    "status": "cached", "case_id": case["case_id"],
                    "pr_title_zh": case.get("pr_title_zh", ""),
                    "problem_statement_zh": case["problem_statement_zh"],
                    "translation": case["translation"],
                }

            config = self._config().get("translation_config")
            if config is None:
                raise ValueError("on-demand translation is not configured")
            source = self._translation_source(case)
            cache_path = self._translation_cache_path(
                payload["source_manifest_sha256"], case["case_id"])
            attempt_root = cache_path.parent / "attempts"
            attempt_root.mkdir(exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            workdir = attempt_root / f"{case['case_id']}_{stamp}"
            workdir.mkdir()
            if evaluator is None:
                evaluator = ApiEvaluator(
                    config["backend"], model=config.get("model"),
                    key_file=config["key_file_path"],
                    attempts=int(config.get("attempts", 2)),
                    min_interval=float(config.get("min_interval", 1.0)),
                    max_tokens=int(config.get("max_tokens", 32768)),
                    cooldown_path=attempt_root / "cooldown.json",
                )
            result, invocation = evaluator(
                packet={"items": [source]}, image_paths=[],
                system_prompt=PROMPT, schema=SCHEMA, workdir=workdir,
                timeout=int(config.get("timeout", 480)),
            )
            translations = result.get("translations") or []
            if len(translations) != 1:
                raise ValueError("translation response must contain exactly one item")
            translated = translations[0]
            validate(source, translated)
            record = {
                "schema_version": "visual-review-on-demand-translation-v1",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_manifest_sha256": payload["source_manifest_sha256"],
                "candidate_binding_sha256": case["candidate_binding_sha256"],
                "case_id": case["case_id"],
                "source_text_sha256": text_digest(source),
                "pr_title_zh": translated["pr_title_zh"],
                "problem_statement_zh": translated["problem_statement_zh"],
                "prompt": str(PROMPT), "prompt_sha256": _sha(PROMPT),
                "schema": str(SCHEMA), "schema_sha256": _sha(SCHEMA),
                "model": {
                    "backend": getattr(evaluator, "backend", config["backend"]),
                    "profile": getattr(evaluator, "profile", None),
                    "attempts": getattr(evaluator, "attempts", config.get("attempts", 2)),
                },
                "invocation": invocation,
            }
            fd, temporary_name = tempfile.mkstemp(
                prefix=".translation_", suffix=".json", dir=cache_path.parent)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(record, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, cache_path)
            finally:
                temporary.unlink(missing_ok=True)
            return {
                "status": "translated", "case_id": case["case_id"],
                "pr_title_zh": record["pr_title_zh"],
                "problem_statement_zh": record["problem_statement_zh"],
                "translation": {
                    "status": "available", "curator_only": True,
                    "source": "on_demand_cache", "file": str(cache_path),
                    "file_sha256": _sha(cache_path),
                    "source_text_sha256": record["source_text_sha256"],
                },
            }

    def save(self, value: object) -> dict:
        bundle, manifest, _, _ = self.bundle()
        decisions_root = self.state_root / "16_04_06_human_decisions"
        decisions_root.mkdir(exist_ok=True)
        encoded = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
        if len(encoded) > MAX_DECISION_BYTES:
            raise ValueError("human decision export is too large")
        fd, temporary_name = tempfile.mkstemp(
            prefix=".decision_", suffix=".json", dir=decisions_root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            result = self._validate_human_export(bundle, temporary)
            digest = _sha(temporary)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            destination = decisions_root / (
                f"16_04_06_decisions_{stamp}_{digest[:12]}.json")
            os.replace(temporary, destination)
            return {
                "status": "saved", "path": str(destination), "sha256": digest,
                "source_manifest_sha256": manifest["source_manifest_sha256"],
                "audit": result,
            }
        finally:
            temporary.unlink(missing_ok=True)


def _dynamic_page(static_page: str) -> str:
    page = re.sub(r'<template id="payload">.*?</template>',
                  '<template id="payload"></template>', static_page, flags=re.S)
    page = re.sub(r'<code>manifest sha256: [0-9a-f]+</code>',
                  '<code id="source-meta">正在读取分类 manifest…</code>', page)
    original = ("const payloadNode=document.querySelector('#payload');\n"
                "const encodedPayload=(payloadNode.content||payloadNode).textContent.trim();\n"
                "const payloadBytes=Uint8Array.from(atob(encodedPayload),character=>character.charCodeAt(0));\n"
                "const DATA=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(payloadBytes));")
    if original not in page:
        raise ValueError("visual-gate page bootstrap changed")
    bootstrap = """(async()=>{\nconst response=await fetch('/api/data',{cache:'no-store'});if(!response.ok)throw Error(await response.text());const DATA=await response.json();\nconst meta=DATA.live_meta;document.querySelector('#source-meta').textContent='source: '+meta.distribution_path+' · sha256 '+meta.distribution_sha256+' · loaded '+meta.loaded_at;document.querySelector('header').insertAdjacentHTML('beforeend',`<span id=live-counts class=badge>${Object.entries(meta.category_counts).map(([k,v])=>k+': '+v).join(' · ')} · total ${meta.qualified_count} · gate ${meta.gate_passed}</span><button id=server-save>原子保存到服务器</button>`);"""
    page = page.replace(original, bootstrap)
    local_state = ("const KEY='visual-gate:'+DATA.source_manifest_sha256;"
                   "let storageAvailable=true;\n"
                   "function readSaved(){try{return JSON.parse(localStorage.getItem(KEY)||'{}')}"
                   "catch(_){storageAvailable=false;return {}}}\n"
                   "let saved=readSaved(),filtered=[...DATA.cases],at=0;")
    if local_state not in page:
        raise ValueError("visual-gate local state bootstrap changed")
    page = page.replace(
        local_state,
        local_state + "for(const c of DATA.cases){if(c.server_decision)"
        "saved[c.case_id]=c.server_decision;}")
    interaction_hook = (
        "document.querySelectorAll('.issue-link').forEach(a=>"
        "a.addEventListener('click',event=>event.stopPropagation()));"
    )
    if interaction_hook not in page:
        raise ValueError("visual-gate interaction hook changed")
    page = page.replace(
        interaction_hook,
        "$('#case').insertAdjacentHTML('beforeend','<section id=test-coverage-panel><style>#test-coverage-panel{grid-column:1/-1}.flow{display:flex;align-items:stretch;gap:5px;overflow:auto;margin:10px 0}.flow i{align-self:center;color:#87909d}.flow-step{min-width:105px;padding:6px 8px;border:1px solid #d8dde6;border-radius:7px;background:#f7f8fa}.flow-step b{display:block;font-size:11px}.flow-step.complete{background:#eaf7ee;border-color:#99d2aa}.flow-step.blocked{background:#fff0ee;border-color:#e7aaa2}.flow-step.pending{background:#fff8df;border-color:#e2c86f}.flow-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.flow-grid section,.test-bundle{border:1px solid #dde2ea;border-radius:7px;padding:8px;margin:6px 0}#test-flow-evidence pre{max-height:420px;overflow:auto;white-space:pre-wrap}#test-flow-evidence table{width:100%;border-collapse:collapse}#test-flow-evidence th,#test-flow-evidence td{border-bottom:1px solid #e3e6eb;padding:5px;text-align:left}.source-path{display:block;overflow-wrap:anywhere}@media(max-width:850px){.flow-grid{grid-template-columns:1fr}}</style><h2>统一流程与测试证据</h2><p class=warn>模型只整理现有测试并提出可执行的补充测试；预测绝不是最终 F2P/P2P。未完成人工视觉审计也可以预运行，但结果仅为 provisional；新增测试必须在修改前后实际运行后分类。</p><div class=row><button id=run-test-coverage type=button>运行测试覆盖 Verifier</button><span id=test-coverage-status>正在检查…</span></div><div id=test-coverage-link></div><div id=test-flow-evidence>正在加载全流程证据…</div></section>');\n"
        + interaction_hook)
    translation_script = r"""
const translationAction=$('#translation-action');
if(translationAction){translationAction.innerHTML=`<button id="translate-case" type="button">${c.problem_statement_zh?'重新加载中文翻译':'翻译当前题面'}</button>`;const translateButton=$('#translate-case');translateButton.onclick=async()=>{translateButton.disabled=true;translateButton.textContent='翻译中…';$('#errors').textContent='';try{const response=await fetch('/api/translate',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({source_manifest_sha256:DATA.source_manifest_sha256,case_id:c.case_id})}),out=await response.json();if(!response.ok)throw Error(out.error||'翻译失败');c.pr_title_zh=out.pr_title_zh;c.problem_statement_zh=out.problem_statement_zh;c.translation=out.translation;$('#pr-title-zh').innerHTML=c.pr_title_zh?`<p class="muted">${esc(c.pr_title_zh)}</p>`:'';$('#translation-content').innerHTML=`<pre>${esc(c.problem_statement_zh)}</pre>`;translateButton.disabled=false;translateButton.textContent='重新加载中文翻译';$('#errors').textContent=out.status==='cached'?'已加载现有中文翻译。':'中文翻译已生成并缓存。'}catch(error){translateButton.disabled=false;translateButton.textContent='重试翻译';$('#errors').textContent=String(error)}};}
"""
    coverage_script = r"""
const flowLabel={complete:'已完成',pending:'待视觉确认',blocked:'阻塞',not_started:'未执行'};
function renderTestFlow(out){const host=$('#test-flow-evidence');if(!host)return;const p=out.pipeline||{},order=[['candidate','候选'],['visual_approved','视觉通过'],['tests_measured','Base/Gold 实测'],['tests_approved','测试批准'],['harbor_controls_passed','Harbor controls'],['frozen','冻结'],['pass5_completed','Pass@5']];let stages=order.map(([k,n])=>`<span class="flow-step ${esc(p[k]||'not_started')}">${esc(n)}<b>${esc(flowLabel[p[k]]||p[k]||'未执行')}</b></span>`).join('<i>→</i>');if(!out.evidence_available){host.innerHTML=`<div class=flow>${stages}</div><p class=muted>尚无测试 Verifier 证据。运行后会在这里按来源展示测试和实测结果。</p>`;return}const v=out.verifier||{},m=out.measurement||{};const fileList=(files,empty)=>files.length?files.map(f=>`<details><summary>${esc(f.path)} · ${esc(f.sha256||'无哈希')}</summary><pre>${esc(f.content||'（packet 只保存了路径/哈希，没有内嵌代码）')}</pre></details>`).join(''):`<p class=muted>${esc(empty)}</p>`;const semanticTable=rows=>rows.length?`<table><tr><th>测试名称</th><th>测试目的</th><th>F2P/P2P</th><th>证据性质</th></tr>${rows.map(x=>`<tr><td>${esc(x.display_name)}</td><td>${esc(x.purpose)}${(x.requirement_ids||[]).length?`<small>覆盖：${x.requirement_ids.map(esc).join('、')}</small>`:''}</td><td>${esc(x.classification)}</td><td>${esc(x.classification_basis==='base_gold_measured'?'Base/Gold 实测':'尚未实测/预测')}</td></tr>`).join('')}</table>`:'<p class=muted>尚无可绑定的测试语义记录。</p>';const contextFiles=v.repository_context_files||[],authorFiles=v.pr_author_test_files||[],priorGenerated=v.pre_verifier_generated_test_files||[],semantics=v.test_semantics||[],priorSemantics=semantics.filter(x=>x.origin==='vlm_generated_test'),authorSemantics=semantics.filter(x=>!['vlm_generated_test','current_verifier_generated_bundle'].includes(x.origin));let generated=(v.generated_bundles||[]).map(b=>`<article class=test-bundle><b>${esc(b.bundle_id)} · 预测 ${esc(b.predicted_transition)}</b><p><b>测试目的：</b>${esc(b.why_assertions_measure_requirements)}</p><div><b>稳定 test ID：</b>${(b.stable_test_ids||[]).map(esc).join('、')||'未提供'}</div><details open><summary>完整生成 test patch</summary><pre>${esc(b.unified_test_patch||'')}</pre></details>${(b.files||[]).map(f=>`<details><summary>${esc(f.operation)} ${esc(f.path)}</summary><pre>${esc(f.content||'')}</pre></details>`).join('')}</article>`).join('')||'<p class=muted>本轮 Verifier 没有再生成新增测试 bundle。</p>';let transitions=(m.transitions||[]).map(t=>`<tr><td>${esc(t.display_name||t.test_id)}</td><td>${esc(t.class)}</td><td>${esc(t.base_status||'未测')}</td><td>${esc(t.gold_status||'未测')}</td><td>${t.matches===true?'✓':'—'}</td></tr>`).join('')||'<tr><td colspan=5>尚未完成同一测试在 Base/Gold 上的实际运行。</td></tr>';let blockers=(m.approval_blockers||[]).concat((out.archive_blockers||[]).map(x=>x.code+': '+x.detail)).map(x=>`<li>${esc(x)}</li>`).join('');let artifacts=Object.entries(out.archive_artifacts||{}).map(([group,items])=>`<details><summary><b>${esc(group)} 证据（${items.length}）</b></summary>${items.map(a=>`<div><a href="${esc(a.url)}" target="_blank" rel="noopener noreferrer">${esc(a.path)}</a> · ${esc(a.size_bytes)} bytes · ${esc(a.sha256)}</div>`).join('')}</details>`).join('');host.innerHTML=`<div class=flow>${stages}</div><div class=flow-grid><section><h3>Verifier 覆盖结论</h3><b>${esc(v.status)}</b><p>${esc(v.summary)}</p><details><summary>决策关键视觉约束与覆盖映射</summary>${(v.constraints||[]).map(x=>`<div class=constraint><b>${esc(x.constraint_id)} · ${esc(x.description)}</b><p>${esc((x.coverage||{}).assertion_summary)}</p><small>${esc((x.coverage||{}).reason)}</small></div>`).join('')}</details></section><section><h3>阶段阻塞项</h3>${blockers?`<ul>${blockers}</ul>`:'<p class=ok>当前归档未记录阻塞项。</p>'}</section></div><details><summary><b>仓库与测试运行上下文（${contextFiles.length} 个文件）</b></summary><p class=muted>这些文件只用于解释测试框架或生产上下文，不自动算作 PR 测试。</p>${fileList(contextFiles,'没有额外上下文文件。')}</details><details open><summary><b>PR 作者提交/修改测试文件中的测试（${authorFiles.length} 个文件）</b></summary><p class=muted>同时包含 PR 新增/修改的断言与同一测试文件中的既有回归用例；逐项以 Base/Gold 实跑区分 F2P 和 P2P。</p><h3>测试目的与实测类型</h3>${semanticTable(authorSemantics)}${fileList(authorFiles,'PR 没有提交或修改测试文件。')}</details><details open><summary><b>本轮 Verifier 前由造题流程生成的测试（${priorGenerated.length} 个文件）</b></summary><h3>测试目的与实测类型</h3>${semanticTable(priorSemantics)}${fileList(priorGenerated,'本轮 Verifier 前没有生成测试。')}</details><details open><summary><b>本轮 Verifier 新生成测试（${(v.generated_bundles||[]).length} 个 bundle）</b></summary>${generated}</details><details open><summary><b>Base/Gold 实测分类</b></summary><table><tr><th>test id</th><th>分类</th><th>Base</th><th>Gold</th><th>一致</th></tr>${transitions}</table><p>${esc(m.f2p||0)} F2P · ${esc(m.p2p||0)} P2P · 每个状态 ${esc(m.repeats_per_state??'未测')} 次</p></details>${artifacts}<p class=muted>证据来源：${out.evidence_source==='live_job'?'页面实时 Verifier 任务':'report/cases 正式归档'}</p><code class=source-path>${esc(out.evidence_directory)}</code>`}
async function refreshTestCoverage(c){const status=$('#test-coverage-status'),button=$('#run-test-coverage'),link=$('#test-coverage-link');if(!status||!button)return;try{const response=await fetch('/api/test-flow?case_id='+encodeURIComponent(c.case_id),{cache:'no-store'}),out=await response.json();if(!response.ok)throw Error(out.error||'状态读取失败');if(!current()||c.case_id!==current().case_id||out.case_id!==c.case_id)return;const job=out.job||{};const labels={ready:'多模态审核已通过，可生成测试',ready_provisional:'多模态审核未确认；可生成 provisional 测试',test_inputs_missing:'当前题未配置一键生成；已有归档脚本仍会展示',prepared:'prepared：已排队'+(job.provisional?'（provisional）':''),running:'running：Verifier 正在整理测试'+(job.provisional?'（provisional）':''),complete:'complete：可审计结果'+(job.provisional?'（provisional，未准入）':''),failed:'failed：'+(job.error||'运行失败')};status.textContent=out.evidence_available?(out.evidence_source==='case_archive'?'已加载归档测试证据':'已加载实时 Verifier 测试证据'):(labels[out.status]||out.status);button.disabled=['prepared','running'].includes(out.status)||!out.test_inputs_configured;button.textContent=out.status==='failed'?'重试生成测试脚本':'生成 / 重新整理测试脚本';link.innerHTML=job.audit_url?`<a href="${esc(job.audit_url)}" target="_blank" rel="noopener noreferrer">打开独立测试审核页 ↗</a>`:'';renderTestFlow(out);const refreshDelay=['prepared','running'].includes(out.status)?1500:(!out.evidence_available?5000:null);if(refreshDelay!==null)setTimeout(()=>{if(current()&&c.case_id===current().case_id)refreshTestCoverage(c)},refreshDelay)}catch(error){if(!current()||c.case_id!==current().case_id)return;status.textContent=String(error);button.disabled=false}}
async function startTestCoverage(c){const button=$('#run-test-coverage'),status=$('#test-coverage-status');button.disabled=true;status.textContent='prepared：正在提交';try{const response=await fetch('/api/test-coverage',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({source_manifest_sha256:DATA.source_manifest_sha256,case_id:c.case_id})}),out=await response.json();if(!response.ok)throw Error(out.error||'启动失败');await refreshTestCoverage(c)}catch(error){status.textContent=String(error);button.disabled=false}}
const coverageButton=$('#run-test-coverage');if(coverageButton){coverageButton.onclick=()=>startTestCoverage(c);refreshTestCoverage(c);}
"""
    coverage_script = coverage_script.replace(
        "priorGenerated=v.pre_verifier_generated_test_files||[],semantics=v.test_semantics||[],priorSemantics=semantics.filter(x=>x.origin==='vlm_generated_test'),authorSemantics=semantics.filter(x=>!['vlm_generated_test','current_verifier_generated_bundle'].includes(x.origin))",
        "generatedFiles=v.verifier_generated_test_files||v.pre_verifier_generated_test_files||[],semantics=v.test_semantics||[],generatedSemantics=semantics.filter(x=>['verifier_generated','vlm_generated_test','current_verifier_generated_bundle'].includes(x.origin)),authorSemantics=semantics.filter(x=>!['verifier_generated','vlm_generated_test','current_verifier_generated_bundle'].includes(x.origin))",
    )
    coverage_script = coverage_script.replace(
        "<details open><summary><b>本轮 Verifier 前由造题流程生成的测试（${priorGenerated.length} 个文件）</b></summary><h3>测试目的与实测类型</h3>${semanticTable(priorSemantics)}${fileList(priorGenerated,'本轮 Verifier 前没有生成测试。')}</details><details open><summary><b>本轮 Verifier 新生成测试（${(v.generated_bundles||[]).length} 个 bundle）</b></summary>${generated}</details>",
        "<details open><summary><b>Verifier 生成的候选测试（${generatedFiles.length} 个文件/版本）</b></summary><p class=muted>统一展示历史轮次与当前轮次；底层仍保留生成批次和哈希。</p><h3>测试目的与实测类型</h3>${semanticTable(generatedSemantics)}${fileList(generatedFiles,'尚无 Verifier 生成测试文件。')}${generated}</details>",
    )
    coverage_script = coverage_script.replace(
        "本轮 Verifier 没有再生成新增测试 bundle。",
        "当前记录中没有新增测试 bundle。",
    )
    coverage_script = coverage_script.replace(
        "PR 作者提交/修改测试文件中的测试（${authorFiles.length} 个文件）</b></summary><p class=muted>同时包含 PR 新增/修改的断言与同一测试文件中的既有回归用例；逐项以 Base/Gold 实跑区分 F2P 和 P2P。",
        "PR 作者随本 PR 提交/修改的测试（${authorFiles.length} 个文件）</b></summary><p><b>${authorFiles.length?'检测到作者测试':'未检测到作者测试'}</b></p><p class=muted>这里只统计 PR patch 新增或修改的测试文件，不把仓库原有测试基础设施算成作者测试；逐项以 Base/Gold 实跑区分 F2P 和 P2P。",
    )
    page = page.replace(interaction_hook, interaction_hook + translation_script + coverage_script)
    export_hook = "$('#export').onclick="
    if export_hook not in page:
        raise ValueError("visual-gate export hook changed")
    save_script = """document.querySelector('#server-save').onclick=async()=>{const rows=DATA.cases.map(c=>saved[c.case_id]).filter(Boolean),bad=rows.flatMap(s=>validate(s).map(e=>s.case_id+': '+e));if(bad.length){$('#errors').textContent=bad.join('\\n');return}const body={schema_version:'visual-gate-human-export-v1',source_manifest_sha256:DATA.source_manifest_sha256,exported_at:new Date().toISOString(),rows};const r=await fetch('/api/decisions',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});const out=await r.json();if(!r.ok){$('#errors').textContent=out.error||'保存失败';return}$('#errors').textContent='已原子保存：'+out.path+'\\nsha256 '+out.sha256;await refreshTestCoverage(current())};\n"""
    page = page.replace(export_hook, save_script + export_hook)
    end = page.rfind("</script>")
    if end < 0:
        raise ValueError("visual-gate script is missing")
    return (page[:end]
            + "\n})().catch(error=>{document.body.innerHTML='<pre style=padding:20px;color:#b42318>'+String(error)+'</pre>'});\n"
            + page[end:])


def _bundle_page(bundle: Path) -> Path:
    legacy = bundle / "16_04_03_visual_gate_review.html"
    return legacy if legacy.is_file() else bundle / "index.html"


def make_handler(state: ReviewState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "VisualReview/1"

        def _json(self, value: object, status: int = HTTPStatus.OK) -> None:
            body = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                route = parsed.path
                if route == "/api/test-coverage/status":
                    values = parse_qs(parsed.query)
                    case_ids = values.get("case_id", [])
                    if len(case_ids) != 1:
                        raise ValueError("test coverage status requires one case_id")
                    self._json(state.test_coverage_status(case_ids[0]))
                    return
                if route == "/api/test-flow":
                    values = parse_qs(parsed.query)
                    case_ids = values.get("case_id", [])
                    if len(case_ids) != 1:
                        raise ValueError("test flow requires one case_id")
                    self._json(state.test_flow_evidence(case_ids[0]))
                    return
                match = re.fullmatch(
                    r"/test-coverage/([A-Za-z0-9_.-]+)/20_11_08_audit\.html", route)
                if match:
                    job_id = match.group(1)
                    if not JOB_ID.fullmatch(job_id):
                        raise FileNotFoundError(route)
                    target = (state._coverage_jobs_root() / job_id / "output"
                              / "20_11_08_audit.html").resolve(strict=True)
                    root = (state._coverage_jobs_root() / job_id).resolve(strict=True)
                    if not target.is_file() or not target.is_relative_to(root):
                        raise FileNotFoundError(route)
                    body = target.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("content-type", "text/html; charset=utf-8")
                    self.send_header("cache-control", "no-store")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                match = re.fullmatch(
                    r"/test-coverage/([A-Za-z0-9_.-]+)/"
                    r"20_12_review/20_12_02_test_review\.html", route)
                if match:
                    job_id = match.group(1)
                    if not JOB_ID.fullmatch(job_id):
                        raise FileNotFoundError(route)
                    root = (state._coverage_jobs_root() / job_id).resolve(strict=True)
                    target = (root / "output/20_12_review/20_12_02_test_review.html"
                              ).resolve(strict=True)
                    if not target.is_file() or not target.is_relative_to(root):
                        raise FileNotFoundError(route)
                    body = target.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("content-type", "text/html; charset=utf-8")
                    self.send_header("cache-control", "no-store")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                match = re.fullmatch(
                    r"/case-evidence/([A-Za-z0-9_.-]+__"
                    r"[A-Za-z0-9_.-]+-[1-9][0-9]*)/(.+)", route)
                if match:
                    target = state.case_artifact(match.group(1), unquote(match.group(2)))
                    body = target.read_bytes()
                    content_type = (mimetypes.guess_type(target.name)[0]
                                    or "application/octet-stream")
                    if content_type.startswith("text/") or content_type == "application/json":
                        content_type += "; charset=utf-8"
                    self.send_response(HTTPStatus.OK)
                    self.send_header("content-type", content_type)
                    self.send_header("cache-control", "no-store")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                bundle, _, payload, _ = state.bundle()
                if route == "/api/data":
                    self._json(payload)
                    return
                if route in {
                    "/",
                    "/index.html",
                    "/visualizations/visual_review",
                    "/visualizations/visual_review/",
                    "/visualizations/visual_review/index.html",
                    "/16_04_live_review.html",
                }:
                    body = _dynamic_page(
                        _bundle_page(bundle).read_text()).encode()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("content-type", "text/html; charset=utf-8")
                    self.send_header("cache-control", "no-store")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                asset_route = route
                review_prefix = "/visualizations/visual_review/"
                if asset_route.startswith(review_prefix + "16_04_02_assets/"):
                    asset_route = "/" + asset_route[len(review_prefix):]
                if asset_route.startswith("/16_04_02_assets/"):
                    relative = Path(unquote(asset_route.lstrip("/")))
                    target = (bundle / relative).resolve(strict=True)
                    if not target.is_file() or not target.is_relative_to(bundle.resolve()):
                        raise FileNotFoundError(route)
                    body = target.read_bytes()
                    content_type = (mimetypes.guess_type(target.name)[0]
                                    or "application/octet-stream")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("content-type", content_type)
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_error(HTTPStatus.NOT_FOUND)
            except FileNotFoundError:
                self.send_error(HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"},
                           HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self) -> None:  # noqa: N802
            route = urlparse(self.path).path
            if route not in {"/api/decisions", "/api/translate", "/api/test-coverage"}:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                if not self.headers.get("content-type", "").lower().startswith(
                        "application/json"):
                    raise ValueError("decision payload must use application/json")
                length = int(self.headers.get("content-length", "0"))
                if length <= 0 or length > MAX_DECISION_BYTES:
                    raise ValueError("invalid decision payload size")
                value = json.loads(self.rfile.read(length))
                if route == "/api/translate":
                    self._json(state.translate(value), HTTPStatus.CREATED)
                elif route == "/api/test-coverage":
                    self._json(state.trigger_test_coverage(value), HTTPStatus.ACCEPTED)
                else:
                    self._json(state.save(value), HTTPStatus.CREATED)
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"},
                           HTTPStatus.BAD_REQUEST)

        def log_message(self, format: str, *args) -> None:
            print("visual-review", self.address_string(), format % args, flush=True)

    return Handler


def serve(config: Path, state_root: Path, host: str = "127.0.0.1",
          port: int = 8765) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("visual review server must bind to loopback")
    state = ReviewState(config, state_root)
    _, manifest, _, distribution = state.bundle()
    server = ThreadingHTTPServer((host, port), make_handler(state))
    print(json.dumps({
        "status": "serving",
        "url": f"http://127.0.0.1:{server.server_port}/visualizations/visual_review/",
        "distribution": str(distribution),
        "candidate_count": manifest["candidate_count"],
    }, ensure_ascii=False), flush=True)
    server.serve_forever()
