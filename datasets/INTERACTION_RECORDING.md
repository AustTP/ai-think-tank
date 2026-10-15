# Recording future training examples

Recording is enabled in the repo's local `.env`. Data stays in:

`datasets/local/interactions/recordings.sqlite3` (gitignored; it
regenerates and accumulates, like the other runtime state).

The recorder captures the actual request messages, model replies
and tool definitions for every OpenRouter call that flows through
the server's single provider choke point (`serve._openrouter_chat_post`),
including separate provider retry attempts, reasoning-recovery
retries, test-time-compute samples and the Jev judge pass. The
Authorization header never enters the record: only the request
body is captured. Agent/task/request identifiers, role, model,
seed (when supplied), timestamps, duration and failures are
retained. Task completion, pipeline and peer-review state are
linked when the simulation saves state (`serve.save_state_to_db`).
SQLite uses WAL and separate capture storage, outside the app's
seven-day audit pruning.

Attribution is set at the request and task entry points: `/api/chat`
and `/api/decide` (agentId/taskId from the request body) and
`sim._server_content_dispatcher` (the agent and task a content
executor is running). Standalone provider calls outside any of
those contexts are recorded as unattributed diagnostics and
excluded from training exports. There is no automatic training
or uploading.

## Configuration

```ini
TRAINING_CAPTURE_ENABLED=true
TRAINING_CAPTURE_PATH=datasets/local/interactions/recordings.sqlite3
TRAINING_CAPTURE_MAX_BYTES=8000000
```

Environment variables override these recording settings for
tests/maintenance. New settings are read on each recording
operation. Capture is off by default and adds no state-read
cost when disabled.

Requests are never recorded with credentials: headers and local
API keys are excluded. Known local secret values, common token
patterns, secret fields and private keys are redacted. Binary
content and oversized payloads are omitted with flags, not
silently truncated. Flagged examples cannot be approved through
the review command. Other private information can still be
present in prompts; keep the local folder private and inspect
exports before sharing. Recordings have no automatic deletion;
monitor disk space. Capture storage errors produce a warning and
do not stop agent work.

## Review and export

From the project root:

```sh
python3 scripts/export_training_interactions.py stats
python3 scripts/export_training_interactions.py list --limit 20
python3 scripts/export_training_interactions.py show INTERACTION_ID
python3 scripts/export_training_interactions.py review INTERACTION_ID --status approved --note "Checked claims and citations against the recorded tool evidence; complete answer."
python3 scripts/export_training_interactions.py export
```

Replace `INTERACTION_ID` with an ID from `list`. Use `--status rejected`
for unsupported or incorrect answers. A successful model call or a task
marked done is not proof of a correct answer. The review decision is
saved with a note and an audit event. Check the recorded outcome as
well as the full conversation.

Export writes `candidates.jsonl`, `train.jsonl`, `eval.jsonl`,
`manifest.jsonl` and `summary.json` into `datasets/local/interactions/export/`.
Only approved, complete, unflagged responses enter train/eval.
Failures, empty replies, refusals and outputs stopped by length
are excluded. Exact conversations are deduplicated.
Requests/root stories and identical input/output groups stay in
one split; unlinked paraphrases need review. One independent group
produces no evaluation rows. The export character limit does not
replace model-token length checks.

```python
from datasets import load_dataset
data = load_dataset("json", data_files={
    "train": "datasets/local/interactions/export/train.jsonl",
    "validation": "datasets/local/interactions/export/eval.jsonl",
})
```

Do not load empty splits. Each row contains `messages`; tool
examples also retain `tools`. Use the target model's own chat
template and tool-formatting support. For report-only training,
select final prose responses with the source evidence in their
actual input messages. Preserve tool-result messages when they
support the answer. Train this set separately from reconstructed
historical examples.
