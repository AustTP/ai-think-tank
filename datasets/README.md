# Research/report fine-tuning dataset

## Curated starter export

`datasets/local/research-labelled/` contains the assistant-labelled export:
8 training examples and 2 evaluation examples across 5 independent groups.
Two original reports were corrected; eight new source-based examples were
authored from local evidence. The remaining 453 reports are withheld because
the retrieved evidence needed to substantiate them was not preserved.

Reproduce the export with `python scripts/label_research_dataset.py`. The
`reviewed.jsonl` records evidence labels, source paths, line ranges, content
hashes and review notes. `manifest.jsonl` records the split and fingerprints.
`historical-data-audit.json` records local CSV integrity checks performed during
curation. Historical quality logs are labelled saved results, not new test runs.
Unverified storage comparisons remain explicitly attributed source assertions.

This is a smoke-test dataset for report writing from supplied evidence, not
enough data to establish a useful fine-tune. It does not teach browsing or tool
execution. Add more verified, varied examples before substantial training.

```python
from datasets import load_dataset

data = load_dataset("json", data_files={
    "train": "datasets/local/research-labelled/train.jsonl",
    "validation": "datasets/local/research-labelled/eval.jsonl",
})
```

Apply your model's own chat template as shown below and check actual token
lengths against the training context limit. The exporter never truncates text.

Local generated data lives in `datasets/local/` (git ignored). The exporter uses
a read-only SQLite snapshot and does not start the server or call an API.

```powershell
python scripts/prepare_unsloth_dataset.py
```

Open `datasets/local/research/review.html` in your browser. Review each task and
report, supply source excerpts (with source identifiers and dates), correct the
target answer, and mark it approved or rejected. Download `reviewed.jsonl`.
Copy that file into `datasets/local/research/`, then run:

```powershell
python scripts/prepare_unsloth_dataset.py --reviewed datasets/local/research/reviewed.jsonl
```

This creates `train.jsonl`, `eval.jsonl`, `manifest.jsonl`, and `summary.json`.
Each training row has a `messages` array with system, user, and assistant roles.
There are no training rows until examples are approved with source evidence.
Examples containing redaction markers are skipped until repaired.

## What these examples teach

The task is **write a report from supplied evidence**. The user message includes
the task requirements and evidence; the assistant message contains the report.
These examples do not teach autonomous browsing, coding, or tool use. The original
research conversations and source snippets were not retained, so the exporter
does not invent them. Evidence must be supplied during review.

`done` does not mean a report was good. All artifacts start pending. Failure text,
unverified claims, credential redactions, and task exceptions are flagged, but
heuristics cannot verify facts. Reject incomplete deliverables and unsupported
claims, including invented browsing or test results. Do not approve a report just
because it has citations. Check the source and match its claims to actual excerpts.

Local `.env` values and common token patterns are redacted without being displayed.
This is best-effort credential removal; review for personal data and other secrets.
Candidates may still contain private project information. Nothing is uploaded.

Train/eval splitting keeps a request (or root story when no request exists) in one
split. Exact duplicate inputs and outputs also link groups before splitting;
identical conversations are deduplicated. Unlinked paraphrases need manual review.
With fewer than two independent groups, evaluation is empty. Add more independent
tasks before evaluating. Do not reuse evaluation examples to select training data.

## Load in an Unsloth notebook

After approval/export, with `datasets` installed:

```python
from datasets import load_dataset

data = load_dataset("json", data_files={
    "train": "datasets/local/research/train.jsonl",
    "validation": "datasets/local/research/eval.jsonl",
})
# Use the tokenizer/processor from your Unsloth model-loading notebook.
def format_batch(batch):
    return {"text": [tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=False
    ) for messages in batch["messages"]]}

formatted = data.map(format_batch, batched=True)
```

Only include `validation` in `data_files` when it contains examples. For a
multimodal Qwen notebook, use its supported processor/template path for text-only
examples. Apply the model's own template; do not hard-code another model's tokens.
Measure token lengths with that tokenizer before training. The export's character
limit is not a token limit. Do not silently truncate evidence or target reports
to fit 2,048 tokens: shorten examples during review or choose a tested larger limit.

See [Unsloth dataset formats](https://unsloth.ai/docs/get-started/fine-tuning-llms-guide/datasets-guide)
and [chat templates](https://unsloth.ai/docs/basics/chat-templates).
