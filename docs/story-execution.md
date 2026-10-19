# Story execution and recovery

Each new coding story receives a private `story-…` workspace. Repairs,
validation and nested reviews resolve to that original story's workspace.
Independent stories can run concurrently; work touching the same workspace
remains serialized so reviewers see a consistent artifact.

The execution service provisions a named Docker container on demand. Agents
use the existing execution API; they do not need access to the host Docker
socket. Only the relevant workspace is bind-mounted. Containers have CPU,
memory and process limits and retain the existing internal network and egress
proxy. Packages and files are installed/downloaded inside the container, not
on the host. Other stories have separate workspace mounts and dependency volumes.

Python/package-prefix installations persist in a private dependency volume.
Container filesystem checkpoints preserve system-package installations across
staged repairs and container recreation. Checkpoints exclude mounted source
and data. Record dependency versions and setup commands in the workspace so
the environment remains reproducible. No volume or workspace is deleted by
the normal completion path.

Generated edits prefer a bounded JSON contract with relative paths, exact old
text and replacement text. All proposed edits are checked before files are
changed; Python replacements are parsed before writing. Creates cannot
overwrite existing files. Targeted tests run before the full required pipeline.
Staged changes publish only after a passing pipeline; failed work remains
checkpointed and does not replace the prior implementation.

Coding handoffs retain answered player questions, original criteria, completed
prerequisite artifact/workspace references and the selected report format.
Recovery recommendations may cite server-provided evidence IDs, which resolve
to exact saved findings. Unknown IDs and unsupported plans remain rejected by
validation and independent review. Python-only projects skip irrelevant web
probes; mixed projects retain them. Cached quality results include the actual
container image and installed Python/system package fingerprint.

The original story's model budget includes all linked repair, investigation and
review costs. The tighter remaining request or story allowance bounds model
responses, and budgeted calls use a single sample. Exhausted stories do not
keep dispatching repairs. Raising a budget preserves the original story and
its accumulated spend instead of creating an unrelated allowance. Budget
increases still require an explicit grant; deployment does not raise budgets.
The player explicitly removed the story-level limits for portfolio formatting
and trading integration. Those two stories and their linked repairs/reviews
are unlimited; their historical spend remains recorded and application-wide
limits still apply.

Completion write-ups include elapsed time, story spend and repair-attempt
counts. Approval counts remain activity metrics, not evidence of delivery.
Acceptance criteria, verified evidence and independent review remain required.

Continuous recovery past retry limits remains exclusive to Always approve.
Moderation and Strict approval retain their existing approval/recovery policy.

Validation includes focused unit/regression suites and live Docker checks of
package persistence, cross-story package/file isolation and container filesystem
checkpoint restoration. Legacy prototype tests requiring missing generated
workspace files are not part of those isolated checks.
