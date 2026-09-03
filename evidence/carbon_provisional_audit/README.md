# Carbon #20978 provisional polish

This directory is a non-blocking Carbon-specific review lane. It does not
promote the candidate, does not modify the candidate task material, and does
not authorize an external model call.

## Current conclusion

Carbon #20978 remains useful as a smoke fixture, but it is not ready for formal
admission. The PR contains no author tests. The current verifier compiles the
real SCSS and reads real Chromium computed styles, but it does so through a
minimal synthetic fixture. One F2P is directly overfit to the reference patch
(`141px` and `116px`), two others unnecessarily require a particular pseudo-
element implementation, and the four P2P checks are narrow proxies rather than
a meaningful changed-scope regression contract.

The four Issue screenshots are agent-safe and visibly distinguish expected and
actual icon ordering/spacing. They support human review of visual necessity,
but they do not uniquely determine the reference CSS constants. Issue #20849
adds the gradient and divider requirements in text. The parent Issue #17992 is
curator context only.

## Artifacts

- `19_10_01_semantic_audit.json`: source/image/test mapping and blockers.
- `19_10_02_human_gate_input_checklist.json`: inputs required by the two real
  human gates and the later control/freeze gate.
- `19_10_03_kimi_k3_pass5_job.json`: secret-free Harbor job proposal using the
  official `kimi-code` adapter. It is deliberately not authorized to run.
- `19_10_04_authorization_proposal.json`: exact current checksum, model,
  budgets, expected calls, blockers, and the fresh-authorization contract.
- `19_10_05_local_validation.json`: local static checks and the preserved nop
  infrastructure failure.

The local nop control was attempted without any model call, but Docker ran out
of storage while unpacking the built image. This is an infrastructure failure
with no reward, not an empty-patch failure. Oracle was intentionally not run
after the same environmental blocker was established. No Docker cleanup was
performed because that would be destructive to shared local state.

## Required sequence before a real Kimi/K3 Pass@5

1. Replace implementation-value F2P assertions with visual geometry or
   screenshot assertions derived from the expected Issue images, with explicit
   tolerances and a faithful component/Storybook fixture.
2. Add changed-selector P2P coverage and a broader existing repository
   regression run.
3. Obtain the independent visual-necessity and F2P/P2P human approvals.
4. Rerun exact-checksum empty/oracle and negative controls.
5. Promote and freeze through the common pipeline.
6. Run an install-only `kimi-code==0.29.0` smoke, then obtain a fresh exact-
   checksum authorization and launch only through `run-frozen-pass5`.

The current task material checksum is
`7a09fc4066c86f8b6df96d1b692cbd9a4daed3219d54889b2978154f1b09499e`.
Any task-material edit invalidates this checksum and every control bound to it.
