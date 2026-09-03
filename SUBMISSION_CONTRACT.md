# Exam submission contract

This file freezes the repository layout derived from the six-page written-test
brief. It is a packaging contract, not evidence that every requirement is
already complete. The PDF's explicit requirements are labelled **exam-required**;
the audit and reproducibility files added by this project are labelled
**project extension**. Project extensions must never change the Harbor-facing
interface of a task.

## Required root layout

```text
report/
├── README.md
├── SUBMISSION_CONTRACT.md
├── pipeline_design.svg
├── code/                         # exam-required construction code
├── evidence/                     # project extension: machine-readable evidence
├── reproducibility/              # project extension: frozen versions/images
├── schemas/                      # project extension: sidecar schemas
├── manifests/                    # project extension: repository hygiene records
└── cases/
    └── <owner>__<repo>-<number>/  # immutable Harbor task root
```

At least five iid task directories are required. An iid/ood construction plan is
required, but the brief does not require an executable ood task.

The PDF requires the README, construction code, pipeline design diagram, and at
least five task directories. `evidence/`, `reproducibility/`, `schemas/`, and
`manifests/` are curator-facing extensions that make those claims auditable.

Each named case directory has this fixed Harbor-facing interface. The labels mean:

- **EXAM-REQUIRED**: stated by the written test;
- **HARBOR-RUNTIME**: needed by the selected Harbor execution path;
- **PROJECT-EXTENSION**: provenance, hardening, or reproducibility metadata.

Project extensions are not additional inputs that an agent must understand.

```text
cases/<owner>__<repo>-<number>/
├── environment/
│   ├── Dockerfile                 # exam-required
│   ├── assets/                   # project convention; copied to /testbed/assets
│   ├── base_image.json           # extension: immutable image binding
│   └── docker-compose.yaml       # extension: runtime hardening
├── instruction.md               # issue text and image paths; edit /testbed
├── solution/
│   ├── solve.sh                 # applies the gold patch with git apply
│   └── gold.patch               # gold patch; agent-hidden
├── task.toml                    # schema_version 1.2; allow_internet=false
└── tests/
    ├── config.json              # repo/base/F2P/P2P/log_parser
    ├── sweb_grade.py            # vendored parsing and functional grading
    ├── test.patch               # repository-relative hidden test patch
    ├── test.sh                  # applies test.patch, runs grading, writes reward.txt
    ├── payload/                 # optional hidden author tests and pixel oracles
    └── test_manifest.json        # extension: exact ordered test inventory
```

No exporter manifest, PR archive, VLM response, model credential, model
trajectory, or human-review form may be placed inside a task directory. An
exporter's curator sidecar belongs at
`evidence/export_manifests/<instance_id>.json`; source archives and raw
run records stay outside the formal submission tree as described below.

## Stable storage boundaries

These locations are frozen for the remainder of the project:

| Data class | Required location | Lifetime |
| --- | --- | --- |
| Formal pipeline code and tests | `code/` | submitted |
| Final Harbor tasks | `cases/<owner>__<repo>-<number>/` | submitted |
| Per-case curator material and runtime records | `cases/<owner>__<repo>-<number>/{meta,outputs}/` | submitted |
| Compact acceptance, oracle, Pass@5 and provenance sidecars | `evidence/` | submitted |
| Schemas, version pins and image bindings | `schemas/`, `reproducibility/` | submitted |
| Complete PR/source archives, raw verifier outputs and raw Harbor jobs | `crawler-output/multimodal-2025/` | durable audit, not copied into tasks |
| Disposable exports, task variants, downloads and install probes | `tmp/multimodal-2025/` | transient, never submitted |

The five or more final task directories therefore remain portable Harbor units,
while large/raw provenance is retained without becoming agent-visible or
inflating the exam repository.

## Required file contracts

The exam-required minimum for `task.toml` is schema `1.2` plus:

- task identity and description;
- `[environment]` a Docker build/image binding, CPU, memory, storage and
  `allow_internet = false`;
- `[agent] timeout_sec` and `[verifier] timeout_sec`.

This project also freezes authors, keywords, benchmark/repo/instance metadata,
build timeout and setup timeout. Those fields appear in the PDF example and are
useful for reproducibility, but are not misrepresented as universal Harbor or
exam-minimum fields.

`tests/config.json` must contain `repo`, `instance_id`, the 40-character
`base_commit`, non-empty and disjoint `FAIL_TO_PASS` and `PASS_TO_PASS` arrays,
and `log_parser`. Test IDs are stable observable behaviors, not line numbers.

`instruction.md` contains only Issue-safe problem information and references
every task image by `/testbed/assets/<file>`. `tests/test.sh` applies the frozen
`tests/test.patch`, runs every required test through `tests/sweb_grade.py`, parses the output, and writes
`/logs/verifier/reward.txt`. `solution/solve.sh` applies the gold patch with
`git apply`.

When a functional judge needs source tests or expected images, the exporter
copies them to `tests/payload/` only. This directory is mounted with the verifier
after the agent turn, is not baked into the baseline image, and is included
file-by-file in the generated integrity check and task checksum.

Formal exports set `[verifier].environment_mode = "separate"`. A root-owned
Harbor collect hook stops residual unprivileged agent processes, then records
the tracked and non-ignored untracked workspace delta against the immutable
squashed baseline. Harbor stops the agent container before verification and
builds `tests/Dockerfile` as a fresh verifier image containing `/tests`; the
verifier applies the transported delta there. Problem-input assets and ignored
dependency/build files are not solution output and are not transported. A
missing or invalid transport artifact is a contract failure with reward zero.
Payload files are snapshotted before Docker inspection, have bounded count and
size, and must be regular non-symlink files.

The verifier applies the patch and publishes results as root, but executes the
functional runner as UID/GID 10002. Before execution it makes the verifier log
directory root-only; afterwards it stops residual UID 10002 processes and then
writes `test_results.json` and `reward.txt`. The baked `/tests` tree is readable
but not writable by the runner.

## Per-task acceptance

- The image contains baseline code, at least one referenced visual asset, and all
  dependencies required to run the judge without downloading packages.
- The agent edits `/testbed`; visual files are available under
  `/testbed/assets/`.
- `tests/config.json` lists stable `FAIL_TO_PASS` and `PASS_TO_PASS` IDs. A listed
  but unobserved test is a failure.
- Reward is `1.0` only when every F2P passes and no P2P regresses. A judge crash
  does not silently become a behavioral zero.
- Empty patch yields `0.0`; `solution/solve.sh` plus the same judge yields `1.0`.
- **PROJECT-EXTENSION:** source Git history and remotes are removed before agent
  execution. The task contains a fresh single baseline commit, so the upstream
  fix cannot be found with `git log`.

Runtime results are evidence about a task, not files that modify the task. Every
acceptance record must bind the task's canonical material checksum, exact test
inventory, environment/image identity, command, tool versions, outcome and
exception state. Human visual-necessity calibration and human F2P/P2P semantic
calibration are independent gates.

## Current submission status

Seven reviewed IID task roots are stored directly under `cases/`. Each
`cases/<instance_id>/` is a standalone Harbor task; curator/source material
and runtime evidence live outside its checksum boundary under
`cases/<instance_id>/{meta,outputs}/`. Formal freezing additionally requires the
measured F2P/P2P inventory and checksum-bound empty=0/gold=1 controls. The README
records final aggregate results only after those controls complete.
