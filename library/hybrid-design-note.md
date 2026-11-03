# Hybrid reasoning protocol (NORA-182)

Source: arxiv 2610.18861, "Hybrid Reasoning Agents" (Stanford / Together AI /
UIUC / Meta). Operator request 2026-11-02: implement within the think tank.

## What was adopted, and why the paper's architecture was not

The paper trains a HybridLink adapter (R(h)=h+W2*sigma(W1*h), ~10M params at
4B scale) plus continuous-token generation and KV-cache working memory, using
HybridSFT and HybridRL. That requires white-box model access and a
post-training pipeline. The tank's agents call API models through /api/chat;
the model boundary is hard. What the tank CAN carry is the paper's behavioral
contract, and that is what shipped:

| Paper concept | Tank implementation |
|---|---|
| Latent mode | A private reasoning pass (`_hybrid_latent_plan`, low tier) whose output never enters the visible collaboration channel |
| Text mode | The existing model calls (tool use, messages, deliverables) |
| Working memory | `library/hybrid/<agent>/<task>.md` -- bounded, newest-first distilled notes, plus an optional rag_index embedding (source='hybrid') |
| HybridLink (latent -> next input) | The digest compiler: /api/chat prepends the memory digest as a system message on every later call for that task |
| HybridRL (when to think vs act) | Heuristic mode policy v0 (`_hybrid_task_eligible`); every decision logged as `hybrid_mode` rows in action_log; the tank's grade/completion outcomes are the reward signal for tuning |
| Interpretable latent thoughts | The memory file IS readable pseudo-text, kept deliberately outside the collaboration channel |

## Mechanics

1. Dispatch: `_server_content_dispatcher` calls `_hybrid_prepare` before any
   executor runs. Eligible lanes (research, distill, spike, note, report --
   NOT code/bug/meeting/review) get one private planning pass per 5 minutes
   (cooldown), on the low tier, max 400 tokens.
2. Bridge: /api/chat checks for the task's memory file (one os.path.isfile)
   and injects up to 2000 chars as a trailing system message. The latent pass
   itself sends `hybridBypass: true` so it never sees its own output echoed.
3. Kill switches: `HYBRID_MODE=off` disables both halves; per-task
   `task['hybrid']=True` forces, `task['hybridOff']=True` opts out.

## Honest framing for the A/B evaluation

The paper reports +10.4% average performance, 35.5-54.7% token reduction,
1.8-2.6x faster inference, 2.7-3.7x cost savings. Those numbers depend on
TRAINED latent dynamics; an emulation inherits none of them automatically.
The evaluation the tank should run (criterion 3): comparable baseline vs
hybrid cards, measured on tokens, spend (kv_spend / storySpendUsd), grade,
and wall time -- report the emulation's own deltas, never the paper's.
