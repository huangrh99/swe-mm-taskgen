# Evidence layout

This directory contains compact curator-facing records that support claims in
the exam README. It is not mounted into the coding-agent phase of a Harbor task.

```text
evidence/
├── export_manifests/       # deterministic source/task bindings, one per task
├── oracle/                 # per-task empty/gold control summaries
├── model_evaluation/       # frozen Pass@5 group summaries and failure analyses
├── review/                 # dual human-gate decisions and audit summaries
└── reproducibility/        # clean-host/runtime verification summaries
```

Large raw jobs, full PR archives, raw VLM responses, and downloadable review
pages remain under `crawler-output/multimodal-2025/`. Disposable task variants
and install probes remain under `tmp/multimodal-2025/`. Credentials are never
stored in any of these locations.

Every per-task evidence record must bind the task's canonical material checksum,
the exact ordered test inventory, and the relevant environment/tool versions.
An absent, skipped, crashed, timed-out, or infrastructure-invalid run is not a
behavioral failure and must remain distinguishable from reward `0`.
