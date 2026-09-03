# Dropped benchmark candidate

This directory preserves the historical construction and Harbor evidence for
`carbon-design-system__carbon-22019`, but it is excluded from the submitted IID
task set and all active Pass@5 batches.

Reason: the broad linked Issue #21567 asks for consistent tooltips on both the
Search and Clear controls, while PR #22019 only changes the collapsed Search
trigger. The narrower duplicate Issue #21572 matches that patch, but its text
explicitly names `.cds--icon-tooltip`, making the repair text-solvable without
the visual input. Neither source yields both faithful PR scope and strict visual
necessity.

The internal `instance_id`, task files, checksums, and prior outputs remain
unchanged so the historical evidence is not rewritten after the fact. The
`-drop` directory suffix is the exclusion marker.
