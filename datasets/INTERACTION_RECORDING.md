# Recording future training examples

Recording is enabled in the repo's local `.env`. Data stays in:

`datasets/local/interactions/recordings.sqlite3`

The recorder captures actual request messages, model replies and tool definitions
for local workers and OpenRouter calls, including separate provider retry
attempts and review/correction calls. Tool results include the executed function
name, arguments and returned output. Source evidence remains inside the exact
messages/tool results shown to the model; it is not reconstructed from reports.
Agent/task/request identifiers, role, model, seed (when supplied), timestamps,
duration and failures are retained. Task completion, pipeline and peer-review
state are linked when the simulation saves updated state. SQLite uses WAL and
separate capture storage, outside the app's seven-day audit pruning.

Agent attribution applies to `/api/chat` and calls inside the server content
dispatcher. Standalone chat helpers invoked outside either context are recorded
as unattributed diagnostics and excluded from training exports. Policy decision API payloads are not conversational
training records. There is no automatic training or uploading.

## Configuration

```ini
TRAINING_CAPTURE_ENABLED=true
TRAINING_CAPTURE_PATH=datasets/local/interactions/recordings.sqlite3
TRAINING_CAPTURE_MAX_BYTES=8000000
```

Environment variables override these recording settings for tests/maintenance.
Restart after code changes. New settings are read on each recording operation.
Requests are never sent with credentials to the recorder: headers and local API
keys are excluded. Known local secret values, common token patterns, secret
fields and private keys are redacted. Binary content and oversized payloads are
omitted with flags, not silently truncated. Flagged examples cannot be approved
through the review command. Other private information can still be present in
prompts; keep the local folder private and inspect exports before sharing.
Recordings have no automatic deletion; monitor disk space. Capture storage
errors produce a warning and do not stop agent work.

## Review and export

Every export preserves existing files first, then saves a dated snapshot under
`export/history/YYYY-MM-DD_HH-MM-SS_<token-limit>-tokens_<unique-id>/`.
Dates use America/New_York. Each snapshot includes all dataset files, settings,
validation reports, and `archive.json` with file hashes. The root export files
remain the latest version; the app displays the dated snapshot path.

Training exports and the app review queue include workers only. Reviewer and
administrative roles (including directors, scrum/task masters, librarians and
risk managers) and worker review tasks are excluded even if previously approved.
Unknown roles are withheld. Existing raw recordings remain available for diagnostics.

Every export also prepares `train.studio.jsonl` and `eval.studio.jsonl` for
Qwen3.5-4B using its cached tokenizer and chat template. Tool definitions,
calls and results are materialized as model-native conversation content so
Studio does not discard nested tool fields. Import these files using `messages`.
`studio-validation.json` lists per-example token counts and exclusions, and the
required maximum sequence length. Set Studio's context to at least that value
and enable Train on completions. Confirm masking in Studio's preview/logs;
the exporter verifies response boundaries but does not run Studio's trainer.
No prompt or answer is truncated. Default character limits are disabled.
Studio files default to a 16,384-token limit. Adjust it in the app or pass
`export --max-tokens 16384`. Oversized examples are excluded with a reported
reason, never truncated. Cleanup removes only consecutive exact duplicate
system instructions. Task instructions, conversation history, evidence, tool
calls/results and final responses are preserved. Shortened-context proposals
are no longer generated; older proposals remain in dated archives.
The exporter uses the local Studio Python; `TRAINING_TOKENIZER_PYTHON` can override
it. Preparation failures clear Studio output files and report the error instead
of leaving stale files presented as ready. Original structured exports remain.

In the app, open the board and click **Training review**. Filter recordings by
review status, select a recording to read its request, response, tool definitions
and recorded task outcome, then enter a review note and approve, reject or keep
it pending. **Export approved examples** writes the files described below and
shows the training/evaluation counts and output folder. The API requires login.
Restart the server after installing this feature to load its new routes.

From the project root:

```powershell
python scripts/export_training_interactions.py stats
python scripts/export_training_interactions.py list --limit 20
python scripts/export_training_interactions.py show INTERACTION_ID
python scripts/export_training_interactions.py review INTERACTION_ID --status approved --note "Checked claims and citations against the recorded tool evidence; complete answer."
python scripts/export_training_interactions.py export
```

Replace `INTERACTION_ID` with an ID from `list`. Use `--status rejected` for
unsupported or incorrect answers. A successful model call or a task marked done
is not proof of a correct answer. The review decision is saved with a note and
an audit event. Check the recorded outcome as well as the full conversation.

Export writes `candidates.jsonl`, `train.jsonl`, `eval.jsonl`, `manifest.jsonl`
and `summary.json` into `datasets/local/interactions/export/`. Only approved,
complete, unflagged responses enter train/eval. Failures, empty replies, refusals
and outputs stopped by length are excluded. Exact conversations are deduplicated.
Requests/root stories and identical input/output groups stay in one split;
unlinked paraphrases need review. One independent group produces no evaluation
rows. The export character limit does not replace model-token length checks.

```python
from datasets import load_dataset
data = load_dataset("json", data_files={
    "train": "datasets/local/interactions/export/train.jsonl",
    "validation": "datasets/local/interactions/export/eval.jsonl",
})
```

Do not load empty splits. Each row contains `messages`; tool examples also retain
`tools`. Use the model's own Unsloth chat template and tool-formatting support.
For report-only training, select final prose responses with the source evidence
in their actual input messages. Preserve tool-result messages when they support
the answer. Train this set separately from reconstructed historical examples.
