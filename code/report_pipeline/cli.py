"""One supported CLI for the formal report pipeline.

Retained ``analysis.scripts.step_*`` modules are internal implementations, not
public entrypoints. Obsolete repository-specific trial modules are not shipped.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import runpy
import re
import sys
from pathlib import Path


CODE_ROOT = Path(__file__).resolve().parents[1]


STAGES = {
    "export-indexed-prs": ("analysis.scripts.step_00_01_export_indexed_prs", "Export a date-bounded collector index to provenance-bearing JSONL."),
    "screen-images": ("analysis.scripts.step_01_screen_pr_body_images", "Find image-bearing PR bodies."),
    "type-media": ("analysis.scripts.step_04_01_type_attachments", "Probe and classify referenced media."),
    "filter-merged": ("analysis.scripts.step_06_filter_merged_default_branch", "Keep PRs merged to the collected default branch."),
    "select-balanced-recall": ("analysis.scripts.step_08_03_select_balanced_visual_recall", "Select a six-bucket recall batch before source archival and V3 classification."),
    "probe-linked-issue-media": ("analysis.scripts.step_08_03_probe_linked_issue_media", "Recall linked Issues without requiring PR visual-text signals."),
    "verify-visual": ("analysis.scripts.step_09_03_run_visual_verifiers", "Prepare or run the visual-context verifier."),
    "archive-sources": ("analysis.scripts.step_11_archive_pr_sources", "Archive complete Issue and PR evidence."),
    "archive-selection-waves": ("analysis.scripts.step_11_02_archive_selected_candidate_waves", "Archive a hash-bound candidate selection in bounded waves."),
    "audit-source-archives": ("analysis.scripts.step_11_03_audit_source_archives", "Validate Stage-11 hashes and classify technical readiness without semantic rejection."),
    "recall-repairs": ("analysis.scripts.step_12_recall_repair_relations", "Recall possible follow-up repair relations."),
    "visual-index": ("analysis.scripts.step_16_02_export_visual_verifier_index", "Build a visual-verifier evidence index."),
    "text-sufficiency": ("analysis.scripts.step_16_06_prepare_and_run_archived_text_only_verifier", "Prepare or run the isolated text-only verifier."),
    "aggregate-text-runs": ("analysis.scripts.step_16_07_aggregate_runs", "Aggregate frozen text-only runs and resolve bound retries."),
    "human-review": ("analysis.scripts.step_16_04_export_human_review", "Render the two-axis human-review queue."),
    "audit-review": ("analysis.scripts.step_16_05_audit_and_import_human_review", "Audit and import human-review decisions."),
    "render-funnel": ("analysis.scripts.step_17_02_export_cross_repo_funnel", "Render the compact cross-repository funnel."),
    "audit-funnel": ("analysis.scripts.step_17_03_audit_cross_repo_funnel", "Audit the rendered funnel."),
}

ORCHESTRATED_STAGES = (
    "prepare-pr-pool",
    "recall-and-archive",
    "construct-solver-inputs",
    "screen-multimodal-candidates",
    "review-visual-gate",
)


def _orchestrate_stage(stage: str, arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=f"report-pipeline {stage}",
        description=("Validate or execute one plan-bound high-level stage. "
                     "Internal commands retain their own evidence and failure semantics."),
    )
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true",
                        help="Execute the validated plan; omission writes a dry-run manifest")
    parser.add_argument("--resume", action="store_true",
                        help="Retry unresolved steps and reuse content-identical completed outputs")
    args = parser.parse_args(arguments)
    from report_pipeline.stage_orchestration import run
    result = run(stage, args.plan, args.output, execute=args.execute, resume=args.resume)
    print(json.dumps({"stage": stage, "status": result["status"],
                      "output": str((args.output / "stage_manifest.json").resolve()),
                      "counts": result.get("counts", {})}, ensure_ascii=False))
    return 0 if result["status"] in {"planned", "complete"} else 1


def _formal_origin(module: str) -> Path:
    spec = importlib.util.find_spec(module)
    if spec is None or spec.origin is None:
        raise RuntimeError(f"formal module is unavailable: {module}")
    origin = Path(spec.origin).resolve()
    if not origin.is_relative_to(CODE_ROOT):
        raise RuntimeError(
            f"refusing non-formal module {module}: {origin}; use `python3 report/run.py`"
        )
    return origin


def _dispatch_module(module: str, arguments: list[str]) -> int:
    _formal_origin(module)
    previous = sys.argv
    try:
        sys.argv = [module, *arguments]
        runpy.run_module(module, run_name="__main__")
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        print(exc.code, file=sys.stderr)
        return 1
    finally:
        sys.argv = previous
    return 0


def _collect(arguments: list[str]) -> int:
    _formal_origin("pr_crawler")
    from pr_crawler.__main__ import main as collect_main

    return collect_main(arguments)


def _export_visual(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline export-visual", description="Revalidate and export an existing visual-verifier run without a model call.")
    parser.add_argument("run_directory", type=Path)
    args = parser.parse_args(arguments)
    from analysis.scripts.step_09_03_run_visual_verifiers import export_results

    print(json.dumps(export_results(args.run_directory), ensure_ascii=False, indent=2))
    return 0


def _candidate_dossier(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline candidate-dossier")
    parser.add_argument("--verifier", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    admission = parser.add_mutually_exclusive_group(required=True)
    admission.add_argument("--classification", type=Path,
                           help="Required bound V3 classification for formal admission")
    admission.add_argument(
        "--legacy-migration", action="store_true",
        help="Inspect a legacy verifier as review-only; cannot enter Harbor admission",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    from report_pipeline.candidate import write
    result = write(
        args.verifier, args.archive, args.output, args.classification,
        allow_legacy_migration=args.legacy_migration,
    )
    print(json.dumps({"output": str(args.output.resolve()), "status": result["status"]}, ensure_ascii=False))
    return 0


def _classify_before_review(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline classify-before-review",
        description="Freeze reference change scale and prepare or run the v3 visual-capability classifier.",
    )
    parser.add_argument("--run-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run", action="store_true", help="Perform one authorized VLM call per eligible case")
    parser.add_argument("--backend", choices=("gemini", "k3"), default="gemini")
    parser.add_argument("--model")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--attempts", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--timeout", type=int, default=480)
    parser.add_argument("--resume-from", type=Path,
                        help="Reuse source-bound complete/ineligible results and retry unresolved cases")
    parser.add_argument(
        "--authorization", type=Path,
        help="Required with --run; must exactly match the dry-run authorization proposal",
    )
    parser.add_argument("--run-id", help="Dry-run proposal identity")
    parser.add_argument("--nonce", help="Dry-run single-use authorization nonce")
    parser.add_argument("--expires-at", help="Dry-run authorization expiry (timezone-aware ISO-8601)")
    parser.add_argument("--authorized-output", type=Path,
                        help="Canonical future model-run output bound by the dry-run proposal")
    args = parser.parse_args(arguments)
    if args.run and args.authorization is None:
        parser.error("--run requires --authorization from an exact dry-run proposal")
    if not args.run and args.authorization:
        parser.error("--authorization requires --run")
    proposal_values = (args.run_id, args.nonce, args.expires_at, args.authorized_output)
    if args.run and any(value is not None for value in proposal_values):
        parser.error("proposal identity options are only valid without --run")
    if not args.run and any(value is None for value in proposal_values):
        parser.error("dry run requires --run-id, --nonce, --expires-at, and --authorized-output")
    from pr_crawler.api_engines import ApiEvaluator
    evaluator = ApiEvaluator(args.backend, args.model, args.key_file, args.attempts)
    from report_pipeline.pre_review_classification import run
    result = run(args.run_directory, args.output, run_model=args.run,
                 evaluator=evaluator, timeout=args.timeout, resume_from=args.resume_from,
                 authorization_path=args.authorization,
                 authorization_identity=(None if args.run else {
                     "run_id": args.run_id, "nonce": args.nonce,
                     "expires_at": args.expires_at}),
                 canonical_output=args.authorized_output)
    print(json.dumps({"output": str(args.output.resolve()),
                      "human_review_ready": result["human_review_ready"],
                      "model_invoked": result["model_invoked"]}, ensure_ascii=False))
    return 0


def _authorize_classification(arguments: list[str]) -> int:
    """Materialize an exact V3 authorization from a validated dry-run proposal."""
    parser = argparse.ArgumentParser(
        prog="report-pipeline authorize-classification",
        description="Approve one exact, already-rendered V3 dry-run proposal.",
    )
    parser.add_argument("--proposal", type=Path, required=True,
                        help="Dry-run 16_03_08_pre_review_classifications.json")
    parser.add_argument("--output", type=Path, required=True,
                        help="New authorization JSON under report/evidence")
    args = parser.parse_args(arguments)
    proposal_path = args.proposal.resolve(strict=True)
    output = args.output.resolve()
    from report_pipeline.paths import REPORT_ROOT
    evidence_root = (REPORT_ROOT / "evidence").resolve()
    if output.exists():
        raise ValueError(f"authorization output already exists: {output}")
    if not output.is_relative_to(evidence_root):
        raise ValueError("classification authorization must be under report/evidence")
    value = json.loads(proposal_path.read_text())
    source_run = Path(value.get("source_run", "")).resolve(strict=True)
    from report_pipeline.pre_review_classification import validate_classification_run
    validate_classification_run(source_run, proposal_path)
    proposal = value.get("authorization_proposal")
    if not isinstance(proposal, dict):
        raise ValueError("classification dry run has no authorization proposal")
    authorization = dict(proposal)
    authorization.update(
        schema_version="classification-run-authorization-v1", authorized=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    from report_pipeline.atomic import write_json
    write_json(output, authorization)
    print(json.dumps({"output": str(output), "expected_case_calls":
                      authorization["expected_case_calls"]}, ensure_ascii=False))
    return 0


def _verify_test_coverage(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline verify-test-coverage",
        description=("Inspect measured functional coverage and propose only missing "
                     "executable tests; final F2P/P2P remains measurement-derived."),
    )
    parser.add_argument("--human-review", type=Path, required=True)
    parser.add_argument("--classification", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--case", type=Path)
    source.add_argument("--harbor-task", type=Path)
    parser.add_argument("--transition-audit", type=Path)
    parser.add_argument("--source-measurement", type=Path)
    parser.add_argument("--browser-measurement", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--backend", choices=("gemini", "k3"), default="gemini")
    parser.add_argument("--model")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--attempts", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--timeout", type=int, default=480)
    args = parser.parse_args(arguments)
    evaluator = None
    if args.run:
        from pr_crawler.api_engines import ApiEvaluator
        evaluator = ApiEvaluator(args.backend, args.model, args.key_file, args.attempts)
    elif args.model or args.key_file:
        parser.error("model/key options require --run")
    from report_pipeline.test_extension_verifier import run, run_harbor
    if args.case:
        if not args.transition_audit or args.source_measurement or args.browser_measurement:
            parser.error("--case requires only --transition-audit")
        result = run(args.human_review, args.classification, args.case,
                     args.transition_audit, args.output, evaluator=evaluator,
                     timeout=args.timeout)
    else:
        if (not args.source_measurement or not args.browser_measurement
                or args.transition_audit):
            parser.error("--harbor-task requires --source-measurement and --browser-measurement")
        result = run_harbor(args.human_review, args.classification, args.harbor_task,
                            args.source_measurement, args.browser_measurement,
                            args.output, evaluator=evaluator, timeout=args.timeout)
    print(json.dumps({"output": str(args.output.resolve()),
                      "status": result["status"],
                      "audit": result["audit"]["path"]}, ensure_ascii=False))
    return 0 if result["status"] != "failed" else 1


def _prepare_test_context(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline prepare-test-context",
        description="Bind a Verifier packet to exact Base git blobs and fail closed on missing context.",
    )
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--base-commit")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-files", type=int, default=160)
    parser.add_argument("--max-bytes", type=int, default=2_500_000)
    parser.add_argument("--max-dependency-depth", type=int, default=1)
    args = parser.parse_args(arguments)
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    from report_pipeline.test_context_builder import assemble_repository_test_context
    from report_pipeline.atomic import write_json
    packet = json.loads(args.packet.resolve(strict=True).read_text())
    result = assemble_repository_test_context(
        packet, args.repository, base_commit=args.base_commit,
        max_files=args.max_files, max_bytes=args.max_bytes,
        max_dependency_depth=args.max_dependency_depth)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, result)
    completeness = result["repository_test_context"]["completeness"]
    print(json.dumps({"output": str(args.output.resolve()), **completeness}, ensure_ascii=False))
    return 0 if completeness["status"] == "complete" else 1


def _audit_test_contexts(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline audit-test-contexts")
    parser.add_argument("--packet-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    packets = sorted(args.packet_dir.resolve(strict=True).glob("*.json"))
    if not packets:
        parser.error("packet directory contains no JSON packets")
    from report_pipeline.test_context_audit import render
    result = render(packets, args.output)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["all_complete"] else 1


def _construct_v4_tests(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline construct-v4-tests",
        description="Run provisional V4 executable-test construction with GPT-5.6 Luna Max.",
    )
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--repositories", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--case-id", action="append", default=[],
                        help="Run only this frozen V4 case; repeat for a retry subset")
    args = parser.parse_args(arguments)
    if args.workers < 1 or args.workers > 14:
        parser.error("--workers must be between 1 and 14")
    from report_pipeline.v4_test_campaign import run
    result = run(args.payload, args.repositories.resolve(strict=True), args.output,
                 workers=args.workers, timeout=args.timeout,
                 case_ids=set(args.case_id) if args.case_id else None)
    complete = sum(item.get("status") == "complete" for item in result["records"])
    print(json.dumps({"output": str(args.output.resolve()), "complete": complete,
                      "total": len(result["records"])}, ensure_ascii=False))
    return 0 if complete == len(result["records"]) else 1


def _measure_v4_tests(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline measure-v4-tests",
        description=("Provisionally execute one frozen V4 test bundle on isolated exact "
                     "Base and Base+reference-diff trees."),
    )
    parser.add_argument("--campaign", type=Path, required=True,
                        help="Completed or partial 20_17 test-construction campaign directory")
    parser.add_argument("--repositories", type=Path,
                        help="Local repository root (required by the default clone backend)")
    parser.add_argument("--backend", choices=("clone", "docker"), default="clone",
                        help="Execution backend; docker uses an existing case base image")
    parser.add_argument("--image-prefix", default="visual-env-build",
                        help="Docker base-image repository prefix")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=1800,
                        help="Seconds allowed for each Base or Gold test command")
    parser.add_argument("--setup-timeout", type=int, default=600,
                        help="Seconds allowed for each local clone, checkout, or patch step")
    args = parser.parse_args(arguments)
    if args.workers < 1 or args.workers > 14:
        parser.error("--workers must be between 1 and 14")
    if args.timeout < 1 or args.setup_timeout < 1:
        parser.error("timeouts must be positive")
    if args.backend == "clone" and args.repositories is None:
        parser.error("--repositories is required by the clone backend")
    from report_pipeline.v4_test_measurement import run
    repositories = (args.repositories.resolve(strict=True)
                    if args.repositories is not None else None)
    result = run(args.campaign, repositories, args.output,
                 workers=args.workers, timeout=args.timeout,
                 setup_timeout=args.setup_timeout, backend=args.backend,
                 image_prefix=args.image_prefix)
    print(json.dumps({"output": str(args.output.resolve()),
                      "counts": result["counts"]}, ensure_ascii=False))
    return 0 if result["counts"].get("technical_failure", 0) == 0 else 2


def _render_v4_test_campaign(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline render-v4-test-campaign",
        description="Render a compact read-only audit of a summary or live V4 model-runs directory.",
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--measurement", type=Path,
                        help="Optional 20_19_06_summary.json or its containing directory")
    args = parser.parse_args(arguments)
    from report_pipeline.v4_test_campaign_audit import render
    result = render(args.input, args.output, args.measurement)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _plan_v4_environments(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline plan-v4-environments",
        description="Plan reusable frozen dependency environments from exact local Git objects.",
    )
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--campaign", type=Path,
                        help="Optional campaign summary, root, or 20_17_02_model_runs directory")
    parser.add_argument("--repositories", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    from report_pipeline.v4_environment_plan import run
    result = run(args.payload, args.repositories.resolve(), args.output, args.campaign)
    print(json.dumps({"output": str(args.output.resolve()), "case_count": result["case_count"],
                      "reuse_groups": result["reusable_group_count"],
                      "unsupported": result["unsupported_count"]}, ensure_ascii=False))
    return 0


def _classify_pr_images(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline classify-pr-images",
        description="Classify Issue/PR image chronology and leakage before constructing solver-visible input.",
    )
    parser.add_argument("--archive", type=Path, action="append", default=[])
    parser.add_argument("--archive-manifest", type=Path, action="append", default=[])
    parser.add_argument("--archive-orchestration", type=Path, action="append", default=[])
    parser.add_argument("--retry-from", type=Path,
                        help="Run only failed records from a completed image-role run.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--backend", choices=("gemini", "k3"), default="gemini")
    parser.add_argument("--model")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--attempts", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--timeout", type=int, default=480)
    args = parser.parse_args(arguments)
    if not args.archive and not args.archive_manifest and not args.archive_orchestration and not args.retry_from:
        parser.error("at least one archive input is required")
    if args.retry_from and (args.archive or args.archive_manifest or args.archive_orchestration):
        parser.error("--retry-from cannot be combined with archive inputs")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    evaluator = None
    if args.run:
        from pr_crawler.api_engines import ApiEvaluator
        evaluator = ApiEvaluator(args.backend, args.model, args.key_file, args.attempts)
    from report_pipeline.pr_image_roles import run
    if args.retry_from:
        from report_pipeline.pr_image_roles import validate_run
        previous = validate_run(args.retry_from)
        args.archive = [Path(record["source_archive"]) for record in previous["records"]
                        if record["status"] == "failed"]
        if not args.archive:
            parser.error("retry source contains no failed records")
    result = run(archives=args.archive, archive_manifests=args.archive_manifest,
                 archive_orchestrations=args.archive_orchestration,
                 output=args.output, evaluator=evaluator, timeout=args.timeout)
    print(json.dumps({"output": str(args.output.resolve()), "counts": result["counts"],
                      "audit_html": result["audit_html"]}, ensure_ascii=False))
    return 0


def _audit_pr_images(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline audit-pr-images")
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args(arguments)
    from report_pipeline.pr_image_roles import validate_run
    value = validate_run(args.run)
    print(json.dumps({"run": str(args.run.resolve()), "status": value["status"],
                      "counts": value["counts"]}, ensure_ascii=False))
    return 0


def _render_visual_gate_review(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline render-visual-gate-review",
        description="Render the dedicated visual-necessity and leakage human gate.")
    parser.add_argument("--distribution", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    from report_pipeline.visual_gate_ui import render
    result = render(args.distribution, args.output)
    print(json.dumps({"output": result["output"],
                      "candidate_count": result["candidate_count"],
                      "audit_status": result["audit"]["status"]}, ensure_ascii=False))
    return 0


def _audit_visual_gate_review(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline audit-visual-gate-review",
        description="Audit a visual-only review bundle and optional human export.")
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--decisions", type=Path)
    args = parser.parse_args(arguments)
    from report_pipeline.visual_gate_ui import audit
    print(json.dumps(audit(args.run, args.decisions), ensure_ascii=False, indent=2))
    return 0


def _serve_visual_review(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline serve-visual-review",
        description="Serve a live manifest-backed visual human-review UI.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(arguments)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    from report_pipeline.visual_review_server import serve
    serve(args.config, args.state_root, args.host, args.port)
    return 0


def _unify_visual_review(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline unify-visual-review",
        description=("Preserve the V3 review inventory, attach deterministic V4 labels, "
                     "and merge native multi-label V4 candidates."))
    parser.add_argument("--live-config", type=Path, required=True)
    parser.add_argument("--capability-pool", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state-root", type=Path,
                        help="Migrate still-bound server decisions from this review state")
    parser.add_argument("--activate", action="store_true",
                        help="Atomically point the live-review config at the unified bundle")
    args = parser.parse_args(arguments)
    from report_pipeline.atomic import write_json
    from report_pipeline.paths import WORKSPACE_ROOT
    from report_pipeline.unified_visual_review import build
    from report_pipeline.visual_gate_ui import audit

    config_path = args.live_config.resolve(strict=True)
    config = json.loads(config_path.read_text())
    if config.get("schema_version") != "visual-review-live-config-v1":
        parser.error("--live-config has an unsupported schema_version")
    state_root = (args.state_root.resolve() if args.state_root
                  else config_path.parent.resolve())
    distribution_value = Path(config.get("distribution", ""))
    distribution = (distribution_value if distribution_value.is_absolute()
                    else WORKSPACE_ROOT / distribution_value).resolve(strict=True)
    distribution_sha = hashlib.sha256(distribution.read_bytes()).hexdigest()
    candidates = []
    cache_root = state_root / "16_04_02_bundle_cache"
    prebuilt_value = Path(config.get("prebuilt_bundle", ""))
    if prebuilt_value:
        candidates.append(prebuilt_value if prebuilt_value.is_absolute()
                          else WORKSPACE_ROOT / prebuilt_value)
    if cache_root.is_dir():
        candidates.extend(path for path in cache_root.iterdir() if path.is_dir())
    valid = []
    for candidate in candidates:
        try:
            checked = audit(candidate.resolve(strict=True))
            manifest = json.loads((candidate / "16_04_04_review_manifest.json").read_text())
            if manifest.get("distribution", {}).get("sha256") == distribution_sha:
                valid.append((manifest.get("created_at", ""), checked["candidate_count"],
                              candidate.resolve()))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    if not valid:
        parser.error("no hash-valid cached review bundle matches the live distribution")
    base_bundle = max(valid)[2]
    result = build(base_bundle, args.capability_pool, args.output, state_root)
    if args.activate:
        old_sha = hashlib.sha256(config_path.read_bytes()).hexdigest()
        output = args.output.resolve(strict=True)
        try:
            config["distribution"] = (output / "16_14_01_unified_index.json").relative_to(
                WORKSPACE_ROOT.resolve()).as_posix()
            config["prebuilt_bundle"] = (output / "16_14_02_visual_gate_review").relative_to(
                WORKSPACE_ROOT.resolve()).as_posix()
        except ValueError:
            parser.error("--output must be inside the workspace when --activate is used")
        write_json(config_path, config)
        activation = {
            "schema_version": "unified-visual-review-activation-v1",
            "status": "active",
            "live_config": str(config_path),
            "previous_config_sha256": old_sha,
            "active_config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "distribution": config["distribution"],
            "prebuilt_bundle": config["prebuilt_bundle"],
        }
        write_json(output / "16_14_05_activation_audit.json", activation)
        result["activation"] = activation
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _select_solver_inputs(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline select-solver-inputs")
    parser.add_argument("--image-role-run", type=Path, action="append", required=True)
    parser.add_argument("--retry-image-role-run", type=Path, action="append", default=[])
    parser.add_argument("--case-id", action="append", default=[],
                        help="Repeat to select only exact case IDs from the supplied runs")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    from report_pipeline.solver_input_selection import run
    result = run(args.image_role_run, args.output, args.retry_image_role_run,
                 args.case_id or None)
    print(json.dumps({"output": str(args.output.resolve()),
                      "selected_count": result["selected_count"],
                      "human_followup_count": result["human_followup_count"]},
                     ensure_ascii=False))
    return 0


def _audit_category_distribution(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline audit-category-distribution")
    parser.add_argument("--classification", type=Path, action="append", required=True,
                        help="Repeat to combine disjoint, independently validated V3 runs")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exclusions", type=Path)
    args = parser.parse_args(arguments)
    from report_pipeline.category_audit import run
    result = run(args.classification, args.output, args.exclusions)
    print(json.dumps({"output": str(args.output.resolve()),
                      "gate_passed": result["gate_passed"]}, ensure_ascii=False))
    return 0


def _build_capability_pool(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline build-capability-pool")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--required-per-category", type=int, default=5)
    args = parser.parse_args(arguments)
    if args.required_per_category < 1:
        parser.error("--required-per-category must be positive")
    from report_pipeline.capability_candidate_pool import build
    result = build(args.config, args.output,
                   required_per_category=args.required_per_category)
    print(json.dumps({"output": str(args.output.resolve()),
                      "quota_met": result["quota_met"],
                      "distribution": result["distribution"]},
                     ensure_ascii=False))
    return 0 if result["quota_met"] else 2


def _render_capability_pool(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline render-capability-pool",
        description="Render a portable filtered view from a frozen candidate-pool manifest.")
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--translations", type=Path, action="append", default=[])
    args = parser.parse_args(arguments)
    from report_pipeline.capability_candidate_pool import render_snapshot
    result = render_snapshot(args.source_run, args.output, args.translations)
    print(json.dumps({"output": str(args.output.resolve()),
                      "model_invoked": result["model_invoked"],
                      "asset_count": result["asset_count"],
                      "translation_count": result["translation_count"]}, ensure_ascii=False))
    return 0


def _convert_v3_capabilities(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline convert-v3-capabilities",
        description="Deterministically convert frozen V3 evidence to V4 capabilities.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    from report_pipeline.v3_v4_conversion import run
    result = run(args.config, args.output)
    print(json.dumps({"output": str(args.output.resolve()),
                      "model_invoked": result["model_invoked"],
                      "converted_count": result["converted_count"],
                      "needs_review_count": result["needs_review_count"],
                      "distribution": result["distribution"]}, ensure_ascii=False))
    return 0


def _classify_capabilities(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline classify-capabilities",
        description="Run the narrow four-label, multi-label visual-capability verifier.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--backend", choices=("gemini", "k3"), default="gemini")
    parser.add_argument("--model")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--attempts", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--timeout", type=int, default=480)
    parser.add_argument("--workers", type=int, choices=range(1, 17), default=10,
                        help="Parallel verifier calls (default: 10)")
    args = parser.parse_args(arguments)
    evaluator = None
    if args.run:
        from pr_crawler.api_engines import ApiEvaluator
        evaluator = ApiEvaluator(args.backend, args.model, args.key_file, args.attempts)
    from report_pipeline.capability_verifier import run
    result = run(args.config, args.output, evaluator=evaluator,
                 timeout=args.timeout, workers=args.workers)
    print(json.dumps({"output": str(args.output.resolve()), **result["counts"]},
                     ensure_ascii=False))
    return 0 if result["counts"]["failed"] == 0 else 2


def _audit_provisional_chain(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline audit-provisional-chain")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--category-audit", type=Path, required=True)
    parser.add_argument("--source-measurement", type=Path, required=True)
    parser.add_argument("--browser-measurement", type=Path, required=True)
    parser.add_argument("--controls", type=Path, required=True)
    parser.add_argument("--smoke", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    from report_pipeline.provisional_chain_audit import run
    result = run(args.instance_id, args.category_audit, args.source_measurement,
                 args.browser_measurement, args.controls, args.smoke, args.output)
    print(json.dumps({"output": str(args.output.resolve()), "status": result["status"],
                      "valid_k3_trials": result["k3"]["valid_trial_count"]},
                     ensure_ascii=False))
    return 0


def _verify_source_scope(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline verify-source-scope",
        description="Fetch explicit one-hop parent Issues and optionally run the bound scope verifier.",
    )
    parser.add_argument("--dossier", type=Path, required=True)
    parser.add_argument("--test-context", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run", action="store_true", help="Perform one authorized model call")
    parser.add_argument("--backend", choices=("gemini", "k3"), default="gemini")
    parser.add_argument("--model")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--attempts", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--timeout", type=int, default=480)
    args = parser.parse_args(arguments)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    evaluator = None
    if args.run:
        from pr_crawler.api_engines import ApiEvaluator
        evaluator = ApiEvaluator(
            args.backend, model=args.model, key_file=args.key_file,
            attempts=args.attempts,
        )
    from pr_crawler.api import credential
    from report_pipeline.source_scope import run
    result = run(
        args.dossier, args.output, args.test_context, evaluator,
        token=credential(), timeout=args.timeout,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _audit_source_scope(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline audit-source-scope")
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    from report_pipeline.source_scope import audit
    result = audit(args.run, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _render_source_scope(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline render-source-scope")
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    from report_pipeline.source_scope import render
    print(render(args.run, args.output))
    return 0


def _apply_human_calibration(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline apply-human-calibration")
    for name in ("dossier", "measurement", "decision", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--task", type=Path)
    parser.add_argument("--test-context", type=Path)
    args = parser.parse_args(arguments)
    from report_pipeline.calibration import apply
    result = apply(args.dossier, args.measurement, args.decision, args.output,
                   args.manifest, args.task, args.test_context)
    print(json.dumps({"output": str(args.output.resolve()),
                      "may_enter_final_taskset": result["benchmark_eligibility"]["may_enter_final_taskset"]}))
    return 0


def _render_human_calibration(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline render-human-calibration")
    for name in ("dossier", "manifest", "measurement", "task", "test-context", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--source-scope", type=Path)
    parser.add_argument("--queue", type=Path)
    args = parser.parse_args(arguments)
    from report_pipeline.calibration_ui import render
    result = render(args.dossier, args.manifest, args.measurement, args.task,
                    args.test_context, args.output, args.source_scope, args.queue)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _audit_human_calibration(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline audit-human-calibration")
    parser.add_argument("--html", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    from report_pipeline.calibration_ui import audit
    result = audit(args.html, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _measure_source_tests(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline measure-source-tests")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    from report_pipeline.source_tests import compare, run
    before = run(args.manifest, args.repo, args.baseline)
    after = run(args.manifest, args.repo, args.reference)
    result = {"baseline": before, "reference": after,
              "measurement": compare(args.manifest, before, after)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(args.output.resolve()),
                      "all_transitions_match": result["measurement"]["all_transitions_match"]}))
    return 0


def _record_harbor_measurement(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline record-harbor-measurement",
        description="Normalize repeated raw Harbor trials into formal F2P/P2P measurement evidence.")
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--dossier", type=Path, required=True)
    parser.add_argument("--baseline-result", type=Path, action="append", required=True)
    parser.add_argument("--reference-result", type=Path, action="append", required=True)
    parser.add_argument("--oracle-quality", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    from report_pipeline.harbor_measurement import build
    result = build(args.task, args.dossier, args.baseline_result,
                   args.reference_result, args.oracle_quality, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _validate_oracle_quality(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline validate-oracle-quality",
        description="Validate curator-only negative and equivalent-positive oracle controls.")
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--dossier", type=Path, required=True)
    parser.add_argument("--oracle-quality", type=Path, required=True)
    args = parser.parse_args(arguments)
    from report_pipeline.harbor_measurement import validate_oracle_quality
    dossier = json.loads(args.dossier.read_text())
    result = validate_oracle_quality(args.oracle_quality, args.task, dossier)
    print(json.dumps({"instance_id": result["instance_id"],
                      "status": result["status"],
                      "negative_variant_count": len(result["negative_variants"])},
                     ensure_ascii=False, indent=2))
    return 0


def _render_candidate(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline render-candidate-review")
    parser.add_argument("--dossier", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--measurement", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--instruction", type=Path)
    parser.add_argument("--harbor-controls", type=Path)
    parser.add_argument("--run-proposal", type=Path)
    parser.add_argument("--negative-controls", type=Path)
    args = parser.parse_args(arguments)
    from report_pipeline.review_html import render
    print(render(args.dossier, args.manifest, args.measurement, args.output,
                 args.instruction, args.harbor_controls, args.run_proposal,
                 args.negative_controls).resolve())
    return 0


def _audit_candidate_review(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline audit-candidate-review")
    parser.add_argument("--html", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    from report_pipeline.review_html import audit
    result = audit(args.html, args.output)
    print(json.dumps({"output": str(args.output.resolve()), "status": result["status"]}))
    return 0


def _compare_test_runs(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline compare-test-runs")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    from report_pipeline.source_tests import compare
    result = compare(args.manifest, json.loads(args.baseline.read_text()), json.loads(args.reference.read_text()))
    result["oracle_kind"] = "chromium_computed_style"
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(args.output.resolve()), "all_transitions_match": result["all_transitions_match"]}))
    return 0


def _export_harbor_task(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline export-harbor-task")
    for name in ("dossier", "manifest", "measurement", "repo", "instruction", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--functional-runner", type=Path)
    parser.add_argument("--test-payload", type=Path)
    args = parser.parse_args(arguments)
    from report_pipeline.harbor_export import export
    result = export(args.dossier, args.manifest, args.measurement, args.repo,
                    args.instruction, args.base_image, args.output, args.functional_runner,
                    args.test_payload)
    print(json.dumps({"output": str(args.output.resolve()), "status": result["status"],
                      "task_material_sha256": result["task_material_sha256"]}))
    return 0


def _audit_harbor_controls(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline audit-harbor-controls")
    for name in ("task", "baseline-job", "oracle-job", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--negative-controls", type=Path, required=True)
    parser.add_argument("--pass5-config", type=Path)
    parser.add_argument("--mode", choices=("real", "simulation"), default="real")
    args = parser.parse_args(arguments)
    from report_pipeline.harbor_controls import audit
    result = audit(args.task, args.baseline_job, args.oracle_job, args.output,
                   args.mode, args.negative_controls, args.pass5_config)
    print(json.dumps({"output": str(args.output.resolve()),
                      "schema_version": result["schema_version"]}))
    return 0


def _run_harbor_negative_controls(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline run-harbor-negative-controls")
    for name in ("task", "harbor", "variants", "jobs", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--mode", choices=("real", "simulation"), default="real")
    parser.add_argument("--pass5-config", type=Path)
    args = parser.parse_args(arguments)
    from report_pipeline.harbor_negative_controls import run
    result = run(args.task, args.harbor, args.variants, args.jobs, args.output,
                 simulation=args.mode == "simulation", pass5_config=args.pass5_config)
    print(json.dumps({"output": str(args.output.resolve()),
                      "controls": len(result["controls"])}, ensure_ascii=False))
    return 0


def _validate_submission(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline validate-submission")
    parser.add_argument("--root", type=Path, default=Path("report"))
    parser.add_argument("--minimum-tasks", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(arguments)
    from report_pipeline.submission_contract import validate
    result = validate(args.root, args.minimum_tasks)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")
    return 0 if result["status"] == "static_layout_complete_not_exam_ready" else 2


def _migrate_case_layout(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline migrate-case-layout")
    parser.add_argument("--cases-root", type=Path, default=Path("cases"))
    parser.add_argument("--report-meta-root", type=Path,
                        default=Path("cases"),
                        help="Deprecated compatibility argument; metadata remains inside each case.")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(arguments)
    from report_pipeline.case_layout import migrate_all
    result = migrate_all(args.cases_root, args.report_meta_root)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")
    return 0


def _build_case_environments(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline build-case-environments")
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--output", type=Path, default=Path("meta/environment_build.json"))
    args = parser.parse_args(arguments)
    from report_pipeline.environment_builder import CASES, build_all
    selected = args.case_id or list(CASES)
    unknown = sorted(set(selected) - set(CASES))
    if unknown or not 1 <= args.workers <= 3:
        parser.error(f"invalid cases/workers: unknown={unknown}")
    result = build_all(selected, args.workers)
    from report_pipeline.atomic import write_json
    write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result["failures"] else 2


def _audit_case_environments(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline audit-case-environments")
    parser.add_argument("--cases-root", type=Path, default=Path("cases"))
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--output", type=Path, default=Path("meta/environment_audit.json"))
    args = parser.parse_args(arguments)
    from report_pipeline.environment_audit import audit_all
    cases = ([args.cases_root / item for item in args.case_id] if args.case_id else
             [item for item in args.cases_root.iterdir() if item.is_dir() and not item.name.startswith("_")])
    result = audit_all(cases, args.workers)
    from report_pipeline.atomic import write_json
    write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "complete" else 2


def _audit_completion(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline audit-completion")
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    from report_pipeline.completion_gate import run
    result = run(args.packet, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 2


def _promote_harbor_task(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline promote-harbor-task")
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--simulation", action="store_true")
    args = parser.parse_args(arguments)
    from report_pipeline.workflow import promote
    result = promote(args.packet, args.output_root, args.record, simulation=args.simulation)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "completed" else 2


def _run_frozen_pass5(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline run-frozen-pass5")
    parser.add_argument("--frozen", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--simulation", action="store_true")
    parser.add_argument("--mock-trials", type=Path)
    parser.add_argument("--authorization", type=Path,
                        help="Required for a real run; binds the exact freeze and Harbor-attempt budget")
    args = parser.parse_args(arguments)
    from report_pipeline.workflow import run_pass5
    try:
        result = run_pass5(args.frozen, args.output, simulation=args.simulation,
                           mock_trials_path=args.mock_trials,
                           authorization_path=args.authorization)
    except Exception as exc:
        from report_pipeline.paths import CASES_ROOT, RUNS_ROOT
        from report_pipeline.atomic import write_json
        output = args.output.resolve()
        if args.simulation:
            allowed_root = RUNS_ROOT.resolve()
        else:
            instance_id = None
            try:
                instance_id = json.loads(args.frozen.resolve().read_text()).get("instance_id")
            except Exception:
                pass
            allowed_root = ((CASES_ROOT / instance_id / "outputs" / "07_pass5").resolve()
                            if isinstance(instance_id, str) else CASES_ROOT.resolve())
        allowed = output != allowed_root and output.is_relative_to(allowed_root)
        rejection_path = output / "pass5_rejection.json"
        if allowed and not rejection_path.exists():
            output.mkdir(parents=True, exist_ok=True)
            def binding(path: Path) -> dict | None:
                resolved = path.resolve()
                if not resolved.is_file():
                    return None
                try:
                    relative = resolved.relative_to(Path.cwd().resolve()).as_posix()
                except ValueError:
                    relative = str(resolved)
                return {"path": relative,
                        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest()}
            raw_code = str(exc)
            code = (raw_code if isinstance(exc, ValueError)
                    and re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,160}", raw_code)
                    else f"pass5_preflight_{type(exc).__name__}")
            rejection = {"schema_version": "pass5-rejection-v1", "status": "rejected",
                         "from": "frozen", "to": "pass5_completed", "code": code,
                         "output_path": str(args.output),
                         "frozen_manifest": binding(args.frozen),
                         "authorization": binding(args.authorization) if args.authorization else None,
                         "valid_trial_count": 0, "infrastructure_invalid_count": 0,
                         "answer_leakage_invalid_count": 0,
                         "attempts": []}
            write_json(rejection_path, rejection)
        public_code = (str(exc) if isinstance(exc, ValueError)
                       and re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,160}", str(exc))
                       else f"pass5_preflight_{type(exc).__name__}")
        print(json.dumps({"status": "rejected", "code": public_code}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _audit_case_batch(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="report-pipeline audit-case-batch")
    parser.add_argument("--cases-root", type=Path, required=True)
    parser.add_argument("--case-id", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    from report_pipeline.case_batch_audit import run
    result = run(args.cases_root, args.case_id, args.output)
    print(json.dumps({"output": str(args.output.resolve()),
                      "case_count": result["case_count"],
                      "valid_archive_count": result["valid_archive_count"]},
                     ensure_ascii=False))
    return 0 if result["valid_archive_count"] == result["case_count"] else 2


def _audit_case_pass5(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline audit-case-pass5",
        description="Audit valid Harbor Pass@5 trials and classify infrastructure failures separately.",
    )
    parser.add_argument("--job-dir", type=Path, action="append", required=True,
                        help="Harbor job directory; repeat for replacement jobs")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--task-checksum", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    from report_pipeline.pass5_audit import run
    result = run(args.job_dir, args.output, instance_id=args.instance_id,
                 task_checksum=args.task_checksum, agent=args.agent, model=args.model)
    print(json.dumps({"output": str(args.output.resolve()), "status": result["status"],
                      "valid_trial_count": result["valid_trial_count"],
                      "replacement_trials_needed": result["replacement_trials_needed"]},
                     ensure_ascii=False))
    return 0 if result["status"] == "complete" else 2


def _audit_seven_case_runtime(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline audit-seven-case-runtime",
        description="Render a fail-closed aggregate of controls and Pass@5 evidence.",
    )
    parser.add_argument("--cases-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    from report_pipeline.seven_case_runtime_audit import run
    result = run(args.cases_root, args.output)
    print(json.dumps({"output": str(args.output.resolve()), "status": result["status"],
                      **result["summary"]}, ensure_ascii=False))
    return 0 if result["status"] == "complete" else 2


def _convert_kimi_trace(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline convert-kimi-trace",
        description="Convert native Kimi wire.jsonl traces into Harbor ATIF trajectory.json files.",
    )
    parser.add_argument("--source", type=Path, required=True,
                        help="A wire.jsonl, Harbor trial, job, or results directory")
    parser.add_argument("--output", type=Path,
                        help="Explicit output for a single selected wire trace")
    parser.add_argument("--force", action="store_true",
                        help="Replace an existing generated trajectory.json")
    args = parser.parse_args(arguments)
    from report_pipeline.kimi_atif import convert
    results = convert(args.source, args.output, force=args.force)
    print(json.dumps({"converted": len(results), "results": results},
                     ensure_ascii=False, indent=2))
    return 0


def _prepare_network_isolated_pass5(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="report-pipeline prepare-network-isolated-pass5",
        description=("Regenerate pending Pass@1/Pass@5 Harbor and frozen-config "
                     "artifacts without invoking a model or claiming freeze readiness."),
    )
    parser.add_argument(
        "--case", action="append", dest="case_ids",
        help="Case instance_id; repeat as needed. Omission prepares the fixed seven cases.",
    )
    parser.add_argument(
        "--harbor-executable", type=Path,
        help="Harbor executable under tmp/; omission uses the pinned local Harbor 0.22 path.",
    )
    args = parser.parse_args(arguments)
    from report_pipeline.pass5_preparation import DEFAULT_HARBOR_EXECUTABLE, run
    result = run(
        args.case_ids,
        harbor_executable=args.harbor_executable or DEFAULT_HARBOR_EXECUTABLE,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="report-pipeline", description=__doc__)
    root.add_argument("command", nargs="?", help="Semantic pipeline command")
    root.add_argument("arguments", nargs=argparse.REMAINDER, help="Arguments passed to that command")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.command or args.command == "list":
        print("Supported commands:")
        print("High-level visual-candidate stages:")
        for name in ORCHESTRATED_STAGES:
            print(f"  {name:32} Run one plan-bound, resumable pipeline stage.")
        print("Internal and downstream commands:")
        print(f"  {'collect':20} Collect or export a resumable GitHub PR archive.")
        for name, (_, description) in sorted(STAGES.items()):
            print(f"  {name:20} {description}")
        print(f"  {'export-visual':20} Revalidate and export a visual-verifier run.")
        print(f"  {'verify-source-scope':20} Judge explicit parent-Issue requirements without expanding siblings.")
        print(f"  {'audit-source-scope':20} Audit source-scope hashes, inventory and traversal bounds.")
        print(f"  {'render-source-scope':20} Render a compact source-scope verifier report.")
        print(f"  {'candidate-dossier':20} Bind auto admission and two independent calibration states.")
        print(f"  {'classify-pr-images':20} Classify Issue/PR image roles and leakage before solver-input construction.")
        print(f"  {'audit-pr-images':20} Revalidate PR-image source, model-attempt and HTML bindings.")
        print(f"  {'render-visual-gate-review':20} Render the visual-necessity/leakage human gate without F2P/P2P.")
        print(f"  {'audit-visual-gate-review':20} Audit the visual-only UI and an optional human export.")
        print(f"  {'serve-visual-review':20} Serve live manifest-backed one-case visual review.")
        print(f"  {'unify-visual-review':20} Preserve V3 evidence and merge V4 multi-label candidates.")
        print(f"  {'select-solver-inputs':20} Build Issue-derived V3 proposals and explicit human follow-up queues.")
        print(f"  {'classify-before-review':20} Freeze change scale and obtain visual-capability labels before review.")
        print(f"  {'authorize-classification':20} Approve one exact validated V3 dry-run proposal.")
        print(f"  {'verify-test-coverage':20} Propose missing functional tests; derive F2P/P2P only by execution.")
        print(f"  {'prepare-test-context':20} Bind exact Base source, test templates, imports and command evidence.")
        print(f"  {'audit-test-contexts':20} Validate hashes and render compact context-completeness evidence.")
        print(f"  {'construct-v4-tests':20} Propose executable tests for the frozen 39-case V4 pool with Luna Max.")
        print(f"  {'measure-v4-tests':20} Provisionally classify V4 tests by isolated exact Base/Gold execution.")
        print(f"  {'render-v4-test-campaign':20} Render a compact audit of completed or running V4 test construction.")
        print(f"  {'plan-v4-environments':20} Plan reusable frozen dependency environments from exact Git objects.")
        print(f"  {'audit-category-distribution':20} Audit five atomic categories plus the required mixed bucket.")
        print(f"  {'classify-capabilities':20} Run the four-label multi-label capability-only VLM verifier.")
        print(f"  {'build-capability-pool':20} Validate and render the provisional four-capability recall pool.")
        print(f"  {'render-capability-pool':20} Re-render a frozen pool with interactive category filters.")
        print(f"  {'convert-v3-capabilities':20} Convert frozen V3 evidence to V4 without a model call.")
        print(f"  {'audit-provisional-chain':20} Bind tests, Harbor controls and invalid K3 smoke evidence without promotion.")
        print(f"  {'apply-human-calibration':20} Apply a bound two-axis human calibration record.")
        print(f"  {'render-human-calibration':20} Render the bound two-gate human calibration UI.")
        print(f"  {'audit-human-calibration':20} Audit the offline calibration UI and evidence links.")
        print(f"  {'measure-source-tests':20} Measure identical frozen source assertions before/after.")
        print(f"  {'record-harbor-measurement':20} Normalize repeated Harbor trials for formal admission.")
        print(f"  {'validate-oracle-quality':20} Validate hidden negative and equivalent-positive oracle controls.")
        print(f"  {'render-candidate-review':20} Render compact two-axis candidate evidence.")
        print(f"  {'audit-candidate-review':20} Statically audit candidate HTML and image links.")
        print(f"  {'compare-test-runs':20} Compare frozen test IDs across two run records.")
        print(f"  {'export-harbor-task':20} Export a deterministic measured visual task for Harbor.")
        print(f"  {'audit-harbor-controls':20} Audit a nop/oracle Harbor control pair.")
        print(f"  {'run-harbor-negative-controls':20} Run isolated baseline/oracle and negative Harbor controls.")
        print(f"  {'promote-harbor-task':20} Apply both human gates and controls before formal task publication.")
        print(f"  {'run-frozen-pass5':20} Validate a frozen task/config and obtain five valid trials.")
        print(f"  {'prepare-network-isolated-pass5':20} Regenerate pending isolated Pass@1/Pass@5 configs without a model call.")
        print(f"  {'audit-case-batch':20} Validate archived candidate evidence and render test/Harbor status.")
        print(f"  {'audit-case-pass5':20} Audit Pass@5 validity, rewards, traces, and replacement needs.")
        print(f"  {'audit-seven-case-runtime':20} Aggregate seven-case controls and audited Pass@5 evidence.")
        print(f"  {'convert-kimi-trace':20} Convert Kimi wire.jsonl traces to Harbor ATIF trajectory.json.")
        print(f"  {'validate-submission':20} Validate the written-test layout and five-task minimum.")
        print(f"  {'migrate-case-layout':20} Expose named task roots and move evidence outside their checksum boundary.")
        print(f"  {'build-case-environments':20} Build content-addressed base images for archived cases.")
        print(f"  {'audit-case-environments':20} Offline-probe base and final task images for every case.")
        print(f"  {'audit-completion':20} Fail closed over every final category/task/review/freeze/Pass@5 gate.")
        return 0
    if args.command in ORCHESTRATED_STAGES:
        return _orchestrate_stage(args.command, args.arguments)
    if args.command == "export-visual":
        return _export_visual(args.arguments)
    if args.command == "candidate-dossier":
        return _candidate_dossier(args.arguments)
    if args.command == "classify-pr-images":
        return _classify_pr_images(args.arguments)
    if args.command == "audit-pr-images":
        return _audit_pr_images(args.arguments)
    if args.command == "render-visual-gate-review":
        return _render_visual_gate_review(args.arguments)
    if args.command == "audit-visual-gate-review":
        return _audit_visual_gate_review(args.arguments)
    if args.command == "serve-visual-review":
        return _serve_visual_review(args.arguments)
    if args.command == "unify-visual-review":
        return _unify_visual_review(args.arguments)
    if args.command == "select-solver-inputs":
        return _select_solver_inputs(args.arguments)
    if args.command == "classify-before-review":
        return _classify_before_review(args.arguments)
    if args.command == "authorize-classification":
        return _authorize_classification(args.arguments)
    if args.command == "verify-test-coverage":
        return _verify_test_coverage(args.arguments)
    if args.command == "prepare-test-context":
        return _prepare_test_context(args.arguments)
    if args.command == "audit-test-contexts":
        return _audit_test_contexts(args.arguments)
    if args.command == "construct-v4-tests":
        return _construct_v4_tests(args.arguments)
    if args.command == "measure-v4-tests":
        return _measure_v4_tests(args.arguments)
    if args.command == "render-v4-test-campaign":
        return _render_v4_test_campaign(args.arguments)
    if args.command == "plan-v4-environments":
        return _plan_v4_environments(args.arguments)
    if args.command == "audit-category-distribution":
        return _audit_category_distribution(args.arguments)
    if args.command == "build-capability-pool":
        return _build_capability_pool(args.arguments)
    if args.command == "render-capability-pool":
        return _render_capability_pool(args.arguments)
    if args.command == "convert-v3-capabilities":
        return _convert_v3_capabilities(args.arguments)
    if args.command == "classify-capabilities":
        return _classify_capabilities(args.arguments)
    if args.command == "audit-provisional-chain":
        return _audit_provisional_chain(args.arguments)
    if args.command == "verify-source-scope":
        return _verify_source_scope(args.arguments)
    if args.command == "audit-source-scope":
        return _audit_source_scope(args.arguments)
    if args.command == "render-source-scope":
        return _render_source_scope(args.arguments)
    if args.command == "apply-human-calibration":
        return _apply_human_calibration(args.arguments)
    if args.command == "render-human-calibration":
        return _render_human_calibration(args.arguments)
    if args.command == "audit-human-calibration":
        return _audit_human_calibration(args.arguments)
    if args.command == "measure-source-tests":
        return _measure_source_tests(args.arguments)
    if args.command == "record-harbor-measurement":
        return _record_harbor_measurement(args.arguments)
    if args.command == "validate-oracle-quality":
        return _validate_oracle_quality(args.arguments)
    if args.command == "render-candidate-review":
        return _render_candidate(args.arguments)
    if args.command == "audit-candidate-review":
        return _audit_candidate_review(args.arguments)
    if args.command == "compare-test-runs":
        return _compare_test_runs(args.arguments)
    if args.command == "export-harbor-task":
        return _export_harbor_task(args.arguments)
    if args.command == "audit-harbor-controls":
        return _audit_harbor_controls(args.arguments)
    if args.command == "run-harbor-negative-controls":
        return _run_harbor_negative_controls(args.arguments)
    if args.command == "validate-submission":
        return _validate_submission(args.arguments)
    if args.command == "migrate-case-layout":
        return _migrate_case_layout(args.arguments)
    if args.command == "build-case-environments":
        return _build_case_environments(args.arguments)
    if args.command == "audit-case-environments":
        return _audit_case_environments(args.arguments)
    if args.command == "audit-completion":
        return _audit_completion(args.arguments)
    if args.command == "promote-harbor-task":
        return _promote_harbor_task(args.arguments)
    if args.command == "run-frozen-pass5":
        return _run_frozen_pass5(args.arguments)
    if args.command == "prepare-network-isolated-pass5":
        return _prepare_network_isolated_pass5(args.arguments)
    if args.command == "audit-case-batch":
        return _audit_case_batch(args.arguments)
    if args.command == "audit-case-pass5":
        return _audit_case_pass5(args.arguments)
    if args.command == "audit-seven-case-runtime":
        return _audit_seven_case_runtime(args.arguments)
    if args.command == "convert-kimi-trace":
        return _convert_kimi_trace(args.arguments)
    if args.command == "collect":
        return _collect(args.arguments)
    selected = STAGES.get(args.command)
    if selected is None:
        parser().error(f"unknown command: {args.command}; run `report-pipeline list`")
    return _dispatch_module(selected[0], args.arguments)
