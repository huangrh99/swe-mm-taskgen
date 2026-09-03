# Combo box AI-label visual parity

Repository: `carbon-design-system/carbon`

Resolve the two linked GitHub Issues reproduced verbatim below. Work only in the
checked-out repository under `/testbed`. Preserve existing behavior outside this
bug, and do not remove or weaken existing tests. The remote image URLs in the
original text have been replaced by their immutable local paths because the task
runs without internet access.

## Issue #20120

Title: [Bug]: Combobox styling issues with `invalid` icon and `ai-label`

### Package

@carbon/web-components


### Package version

latest



### Description

The invalid icon has buggy positioning when paired with an ai-label

#### No Input

**Expected (react):**

![Image](/testbed/assets/52c704ecf729b3f7cbb1cf5773953750a4535b928f3fffcb23b0dea4e11ee68b.png)

**Actual:**

![Image](/testbed/assets/8127091bb3ef3f5f9aa75c8f72994e235de08a27d24b3fce1ad754956e63034a.png)

#### With Input

**Expected (react):**

![Image](/testbed/assets/dd773233556ff6ce11d7dac4f1a1dc5c7e4007d6db4496dbd6960c81f38e87bb.png)

**Actual:**

![Image](/testbed/assets/a529c86b1e186a5a8d946595020d9f5f59fb34315415aeaf35c96ceeb8316352.png)


### Suggested Severity

Severity 3 = User can complete task, and/or has a workaround within the user experience of a given component.

## Issue #20849

Title: `React|WC Parity: Combobox AI Label is missing gradient style`

AI label story:

- It does not have the blue AI styling/colors appearing on the input.
- There needs to be a divider between the AI decorator and the chevron in the input.
- When the close icon appears in the input, the spacing is off and there should be a divider between the AI decorator and the close icon in the input.

See parent issue https://github.com/carbon-design-system/carbon/issues/17992 for acceptance criteria.

> [!IMPORTANT]
> Every PR needs a ux and visual review from the design team to ensure consistency. Please be sure PRs have https://github.com/carbon-design-system/carbon/labels/status%3A%20visual%20review%20%F0%9F%94%8D applied and request a review from @carbon-design-system/design

The parent link and review-process note above are retained as part of the
original Issue text. This offline coding task is scoped to the concrete behavior
described in Issues #20120 and #20849 and the four local visual references.
