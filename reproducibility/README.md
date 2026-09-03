# Reproducibility freeze

`09_pipeline_freeze_manifest.json` is the current machine-readable source of
truth for the frozen general state machine, two public commands, schemas,
installed dependency snapshots, Docker/Harbor policy, and unresolved
candidate-specific bindings. `02_freeze_manifest.json` is retained only as a
historical pre-state-machine snapshot and still refers to the earlier Carbon
prototype location. `realtime_verified`,
`historical_evidence`, `pending`, and `unavailable` are deliberately distinct;
a `partial` freeze cannot certify exam readiness.

## Current boundary

| Area | Frozen now | Still pending |
| --- | --- | --- |
| Models | screening model/prompt/schema observations and proposed official Harbor `kimi-code` provider packet | renewed authorization bound to the current task checksum; five valid trials |
| Dependencies | exact installed Harbor and verifier distribution snapshots; four direct constraint files | clean resolver-produced transitive hash locks |
| Schemas/prompts | every formal verifier prompt/schema and both human-calibration schemas by SHA-256 | future edits require a new freeze revision |
| Harbor | installed `0.22.0`, Python `3.12.13`, exam task schema `1.2`; source revision retained as historical evidence | independent wheel-to-source-commit provenance binding |
| Docker | client `29.6.1`, daemon `29.5.2`, Compose `5.2.0`, Colima `0.10.3`; exact baseline tree; content-derived base tag; image ID; offline archive SHA | independent clean-host archive reload |
| Task | general promotion/Pass@5 contracts only | Carbon's two human gates, fresh controls/image freeze, real Pass@5, and five admitted iid tasks |

The base image is not called immutable merely because its local tag contains a
digest. Before a Harbor build, verify both the 3.05 GB archive and the tag's
actual Docker image ID:

```bash
python3 reproducibility/08_verify_base_image.py
# On a new daemon where the tag is absent:
python3 reproducibility/08_verify_base_image.py --restore
```

Validate the manifest without contacting Docker or an external API:

```bash
PYTHONDONTWRITEBYTECODE=1 \
  tmp/multimodal-2025/09_verifier/venv/bin/python \
  reproducibility/05_verify_freeze.py
PYTHONDONTWRITEBYTECODE=1 \
  tmp/multimodal-2025/09_verifier/venv/bin/python \
  -m unittest reproducibility/test_verify_freeze.py
```

The installed-distribution snapshots are evidence, not clean-resolution
lockfiles. No credential files, environment values, Docker configuration,
sockets, or API keys belong in this directory. A prior model authorization does
not transfer across task checksums.
