# Think Tank — Master Backlog / PRD

Status: **living reference** · Owner: player · Updated: 2026-10-02
Scope: everything the think tank may be asked to build, research, or run — prioritized, with the LLM-vs-tool split per task.

> This is the master catalog. Individual items become real sprint/backlog work only when pulled into the tank's own task queue with a concrete spec. It lives at repo root so the agent teams can read it (seed a copy into `library/` if they should treat it as an always-consulted reference).

---

## 0. Operating principles (every workstream obeys these)

1. **LLM decides, tool executes, verifier checks.** LLMs are poor at exact geometry, strict meter, deterministic math, and long-horizon spatial consistency. Route those to non-LLM models / classic tooling; keep the LLM as orchestrator + writer + planner. Every task needs a **ground-truth verify step** (tests, backtest P&L, diff, replay).
2. **Reusable tools & skills first.** Everything below is "an agent team builds a tool, then reuses it." No one-off scripts; build once, register in the skills registry, let any team consume.
3. **Cost-aware by construction.** Every model call accrues to the Bank (`__general__`, `__jev__`, `__hire_names__`, per-product). Prefer the cheapest model that clears the verify loop. Non-LLM models (embeddings, classifiers, ASR) should be self-hosted via the Laya/Colab pattern to avoid per-token cost.
4. **HF-on-Colab when a non-LLM model is needed.** The tank knows which HF model a task needs, retrieves it via the **HuggingFace API** it holds, and runs it on a **sharded Google Colab** runtime (the existing Laya/Colab standby pattern is the template). Includes transfer-learning / fine-tuning workflows.
5. **Grounded, no hallucinations on numbers.** Finance math, measurements, dimensions, and P&L are computed by engines, never estimated by the LLM.
6. **Security posture everywhere.** Credentials stay server-side (`.env`, capability handles, attribution keys). External actions (X/LinkedIn posting, job applications, email deletion, VPN/DO traffic) are gated, logged, and reversible.
7. **Your voice, not AI voice.** Anything written that you might sign, post, or publish — social posts, job applications, emails, lyrics, reports — matches your distinct writing style and is scanned for common AI-writing tells (em dashes, filler phrasing, over-polish) before it ships (see WS-13).

---

## 1. Foundations (cross-cutting infrastructure — build before capability work)

| ID | Foundation | What it is | Needed by |
|----|-----------|------------|-----------|
| **F1** | **Tools & skills engine** | Skills registry + "fabricator" workflow: an agent team takes a spec, researches the right model/tool, builds it as a reusable skill, registers it with a contract + tests, and any team can consume it. Includes discoverability + reuse tracking. | Every workstream |
| **F2** | **Colab + HF model runner** | A shared skill: given a task needing a non-LLM model, it picks the HF model, pulls it via the HF API, provisions/uses a Colab runtime (sharded when training), runs inference/training, returns artifacts. Template = existing Laya/Colab standby. | Perception, Finance data, Music, ML-eng, ASR |
| **F3** | **VPN / DigitalOcean layer** | Connect via the player's VPN (out-of-state exit for bug bounty; country exits for geo-exploration) + DigitalOcean droplet access. Gated, logged. | Security ops, Geo/anywhere work, Research |
| **F4** | **Ground-truth verify harness** | Standard verify loop: unit tests, backtest engine, geometry solver checks, ASR word-error-rate, classifier holdouts (by time). Every skill ships with one. | All |
| **F5** | **Reporting & audio pipeline** | Scheduled digest/report → TTS/voice synthesis → audio file → wake-up delivery (phone check-in exists). | Personal daily systems, Morning show |
| **F6** | **KB retrieval upgrade** | Library embeddings + BM25 hybrid + reranker (replaces substring search, serve.py:10700). Chunk by heading; numpy+cosine is enough at this corpus size. | All research/report tasks |

**F1 + F2 + F6 first** — they unlock everything else.

---

## 2. Workstreams

### WS-1 Tool & skill fabrication *(your stated #1 priority)*
- Goal: the tank builds and reuses tools/skills, by and across agent teams, to do tasks on your behalf.
- Sample tasks: plant-knowledge skill (learn a plant → reusable Q&A + care skill); photo→measurement tool; PDF→markdown skill; backtest engine skill; music-stem tool; weekly-scan skill.
- Split: LLM plans/designs; code + tests are the deliverable; verify = tests pass.
- Deps: F1, F4, F6. **Priority: P0.**

### WS-2 Perception & geometry
- Goal: infer real-world dimensions from photographs and recreate objects/artifacts in a reproducible form.
- Tasks:
  - Photo → heights/radius/diameter (ShaneFanX-style; push beyond: any measurable dimension from perspective geometry).
  - Garment recreation: analyze a clothing photo → decompose panels/folds → draw construction lines on a blank canvas (parametric SVG/polyline output).
  - Origami: given a target object, derive crease patterns / recreation steps.
  - Exercise stick figures: routine → joint-consistent stick-figure animation (LLM designs routine, kinematics renderer draws).
- Split: LLM orchestrates + writes up; OpenCV/perspective math, SVG geometry solver, skeletal-animation renderer execute.
- Deps: F2 (image models), F4 (geometric verify: reproject and compare). **Priority: P1** — start with ONE proof (photo→height, or exercise stick figures).

### WS-3 Finance
- Goal: backtest trading strategies (equities, crypto, FX), generate new strategies, review markets.
- Tasks:
  - Backtest engine (deterministic fills; LLM never computes P&L). Paper mode first.
  - Live tick data source — **open question** (see §5).
  - Strategy generation + parameter search (LLM proposes, engine verifies, walk-forward validation).
  - Crypto + FX backtests.
  - Polymarket review: find top-performing accounts, analyze their markets/win-profile.
  - Research JEV × algorithmic trading; produce research and/or a build for review.
- Split: LLM researches/writes/plans; backtest engine + data pipeline execute; verify = P&L/sharpe/drawdown.
- Deps: F2 (data ingestion), F4. **Priority: P1** (research + paper backtest before any live money).

### WS-4 Research & intelligence
- Goal: deep, grounded research and reconstruction over public sources.
- Tasks:
  - Google Patents research (e.g., fruit-fly brain connectome → build something inspired by it).
  - Site architecture teardown + rebuild: skillmelo.ai, nerim.ai, dreyx.com.
  - X + LinkedIn search: find relevant posts, review, and respond (e.g., AI-security hiring posts); apply to new job postings using your resume + supplied info.
  - Course re-creation research: NahamSec "Hacking AI" (app.hackinghub.io) — extract lesson content, research each lesson, reconstruct curriculum + materials.
  - Character/historical-figure consolidation: aggregate every appearance/fact; use the profile to inform decisions or review content (Steve Jobs "talk to a historical figure" idea).
  - Secret writing / cryptogram decoding: try multiple decoding avenues per character set.
- Split: LLM orchestrates + writes; scrapers/APIs (X, LinkedIn, patents), OCR/PDF (Nougat/SmolDocling), solver libraries execute; verify = source-cited, fetched-and-stored corpus.
- Deps: F1, F2, F3, F6. **Priority: P1** (start with the X/LinkedIn engagement + research digest; social posting is gated).

### WS-5 Music & audio
- Goal: MIDI-centric music tooling and generative music.
- Tasks:
  - Stem → MIDI: split a track into stems, transcribe each to MIDI, recombine. **(All your music → MIDI.)**
  - Finger-drumming game: Web MIDI, Guitar-Hero-for-16-pads, connect MPC, upload a MIDI drum track and play along, sample packs.
  - Metrical lyrics: given lyrics, generate new lyrics on a topic preserving strong/weak syllable structure (syllabifier + LLM content).
  - MIDI drummer model: feed a drumless MIDI track → generate drums matching the groove/style; use the song-title syllabic rhythm as the guiding prior.
  - KeyLattice extension: new features for the hex-keyboard web app.
- Split: LLM analyzes structure/writes lyrics; DSP/stem separation + MIDI transcription models (e.g., demucs, mdq/MT3-style) + rhythm engine execute; verify = listenback + MIDI diff (note/velocity/time alignment).
- Deps: F2 (audio models on Colab), F4. **Priority: P2** except KeyLattice (P1 if you want it sooner).

### WS-6 Security ops
- Goal: scheduled scanning, bug bounty, and intel maintenance.
- Tasks:
  - Weekly scans on schedule over `~/Desktop/bug_bounty` and `~/Desktop/ai_governance_corpus`.
  - Bug bounty hunting over the VPN (out-of-state exit) + DigitalOcean droplet access.
  - Create/maintain intel forges from `~/Downloads/adversarial-ai-swarm-6.zip` and `~/Downloads/intel-forge-main.zip`; update them regularly.
  - Reuse harvested knowledge (e.g., plant intelligence) across tasks.
- Split: LLM triages findings + writes reports; scanners/linters/VPN/DO tooling execute; verify = reproducible scan, diff over time.
- Deps: F3, F1, F4. **Priority: P1** (scans first; bounty behind the VPN gate).

### WS-7 Agentic sports
- Goal: a basketball analogue of AWS agentic football, with each of 5 players' actions driven by agent instructions, teamwork = cohesion.
- Tasks: study `~/Downloads/afc-arena-run`; build the basketball sim using AWS Nova models (the competition's models).
- Split: LLM is the player brain (per-player policy); the sim/engine executes the action; verify = match outcomes + cohesion metrics.
- Deps: F1, F4. **Priority: P2.**

### WS-8 Personal daily systems
- Goal: daily automated personal intelligence.
- Tasks:
  - Morning report: world news + personal/family items, generated into a report, turned into an AI-vocals audio morning show that wakes you.
  - Nutrition: diet/recipes/grocery plans to lower triglycerides, adjusting daily/weekly.
  - Travel guides: research destinations, daily itineraries, unsafe areas, Google Street View "live like a local", Reddit/X local research.
  - VPN geo-exploration: connect to other countries, enumerate region-locked content, flag anything of value.
- Split: LLM writes/plans; fetch + Street View + TTS pipelines execute; verify = sources linked.
- Deps: F1, F2, F3, F5, F6. **Priority: P2** (morning report could be P1 — biggest daily value).

### WS-9 ML engineering
- Goal: apply transfer learning on your behalf.
- Tasks:
  - Pick an HF model, gather data, fine-tune (transfer learning) on sharded Colab runtimes for a specific use case.
  - Email review: classify safe-to-delete by your criteria (and execute deletions gated).
  - Illustration style fine-tune: ~100 illustrations → generative model in that style.
- Split: LLM plans + evaluates; HF Trainer/LLaMA-Factory/SetFit + Colab execute; verify = held-out evals.
- Deps: F2, F4, F6. **Priority: P1** (email triage classifier is a quick, high-value win; style fine-tune is the marquee).

### WS-10 Family
- Goal: parenting support.
- Tasks: activities for 3-year-old Miles; parenting tips on demand.
- Split: LLM generate; verify = age-appropriateness checklist.
- Deps: F6. **Priority: P2.**

### WS-11 Writing
- Goal: writing that applies researched techniques.
- Tasks:
  - Research generative engine optimization (GEO) and apply it.
  - Malcolm Gladwell Masterclass PDF: research topics, then produce a piece applying one technique.
  - "Advice-as-recipe" column (letter + advice + a metaphorically-related recipe).
- Split: LLM writes; verify = checklist of the technique's criteria.
- Deps: F6. **Priority: P2.**

### WS-12 App builds
- Goal: standalone web apps.
- Tasks:
  - **Legwork**: drop a pin, pay a nearby local to be your eyes on the ground (photo/check/attend/live video) — payments + geo + review.
  - **Trefoil**: Rubik's cube as a graph — sticker positions as concentric-circle intersections + 3D model.
  - **Handwriting-deformation tool**: neural net trains live in-browser on letters you draw, learns a deformation field, predicts your personal style for the whole alphabet.
  - **Data-licensing smart contract**: people contribute data for AI training; get paid when their dataset is licensed.
- Split: LLM designs + codes; verify = functional tests; the in-browser NN (WS-12 handwriting) is a real ML training loop in JS/WebGL.
- Deps: F1, F4. **Priority: P2** (each is a discrete project; pick one to start).

### WS-13 Writing style & voice *(cross-cutting — applies to every text you might sign)*
- Goal: identify your distinct writing style, research AI-writing patterns, and avoid them when writing on your behalf.
- Tasks:
  - **Style fingerprint:** build a durable profile from your writing samples — word choice, sentence length and rhythm, punctuation habits (e.g., whether/how you use em dashes), capitalization, contractions, idioms, paragraphing, and tone. Stored as a reusable skill (WS-1/F1).
  - **AI-writing tells catalog:** research and maintain a living checklist of patterns common in AI prose (em dashes, hedge words, "delve"-style filler, formulaic openers/transitions, over-polished symmetry, bullet-itis, hyperbole) so they can be detected and avoided.
  - **Voice enforcer skill:** before anything ships that you might sign — X/LinkedIn posts, job applications, emails, lyrics, reports — the draft is rewritten to the style fingerprint and run through the tells scanner.
  - **Verify:** a deterministic telltale scan (regex/metrics) plus a benchmark of player-approved samples the style profile is measured against; flag-and-fix, not just flag.
- Split: LLM drafts in the profile; a deterministic telltale scanner + style metrics catch the tells; verify = scan clean + profile distance within tolerance of approved samples.
- Deps: F1, F6, F4. **Priority: P1** (cheap, and improves the quality of WS-4 posts/applications, WS-5 lyrics, WS-8 reports, WS-11 writing).

---

## 3. Recommended build order

### Phase 0 — Foundations (do these first, in this order)
1. **F1 Tools & skills engine** (you already named this first; everything consumes it).
2. **F2 Colab + HF model runner** (perception/finance/music/ML-eng/ASR all block on it).
3. **F6 KB retrieval upgrade** (embeddings + reranker; cheap, improves every research/report task daily).
4. **F4 verify harness** (each of the above ships with a verify loop; formalize it early).

### Phase 1 — High-value quick wins (reuse Phase 0)
5. **Decision-tape classifier** (cut Jev LLM spend on closed-label decisions; data already exists).
6. **Tabular predictors on telemetry** (predict agent drop/fire risk, spend burn-rate forecast — your DS sweet spot, data in the DB).
7. **WS-13 style fingerprint + tells scanner** (cheap, cross-cutting: every post, application, lyric, and report you sign gets written in your voice, free of AI tells).
8. **WS-9 email triage** (classifier over your mail; gated deletion).
9. **WS-3 JEV × algo research + paper backtest engine** (research first, engine second, no live money yet).

### Phase 2 — First capability proof (one from each bucket)
9. **WS-2 proof:** photo→height (simplest geometry proof) or exercise stick figures (needs F2).
10. **WS-4 X/LinkedIn engagement** (gated posting) + **daily research digest**.
11. **WS-6 weekly scans** on bug_bounty / ai_governance_corpus (behind the VPN gate).

### Phase 3 — Marquees & systems
12. **WS-5 MIDI reconstruction** (stem→MIDI for your library).
13. **WS-8 morning report + audio show** (F5).
14. **WS-9 illustration style fine-tune** (F2).
15. **WS-12 apps** — recommend **Legwork** or **Trefoil** as the first standalone build.

Rationale: foundations unblock everything; quick wins build trust + save money (classifier) while proving the skills engine; one proof per capability bucket de-risks the big bets (finance, perception, security) before you scale them.

---

## 4. Risks & guardrails

- **Live money / trading:** paper-mode backtests only until the engine is validated on out-of-sample data. No live orders without your explicit go-ahead.
- **External actions (X/LinkedIn, job applications, email deletion, bounty traffic):** all gated, logged, reversible (draft-for-approval on social; soft-delete + trash on email; VPN only via your provider's credentials).
- **Model/cost drift:** every call accrues to the Bank; non-LLM models self-hosted via Colab/Laya to avoid per-token cost; the Jev gate and budget alerts already fail closed.
- **Training data hygiene (decision-tape classifier):** hold out by time (sequential data — leakage risk); re-verify stale labels (decision drift).
- **VPN jurisdiction:** geo-exploration and bounty traffic obey your VPN terms; nothing runs on an exit you didn't authorize.
- **Copyright / course re-creation:** reconstruct curriculum *from research*, don't redistribute purchased content.
- **Heavy Colab usage:** sharded runtimes and the FREE-plan cap apply (Apify-style spend caps on Colab compute; reuse cached artifacts).

---

## 5. Open questions to resolve (for later design sessions)

1. **Live tick data source** for equities/crypto/FX backtests (Polygon? Alpaca? Binance? FXCM/OANDA?) and whether live feeds are needed yet.
2. **Broker/exchange** for any eventual live trading (likely none for a long time).
3. **VPN provider/exit** to standardize on for bounty + geo work; DO droplet spec + region.
4. **Colab plan** for sharded training (free caps vs. paid) and how often heavy training runs.
5. **Morning show voice** (which TTS/voice-cloning service) and delivery channel (phone check-in vs. file).
6. **Email access** (Gmail API? IMAP?) and the exact deletion criteria.
7. **X/LinkedIn accounts** and posting approval workflow (draft-for-approval?).
8. **Resume + job-search inputs** — what you provide, and the apply gate.
9. **Which WS-12 app first** (Legwork needs payments + geo + local network; Trefoil is self-contained; handwriting tool is self-contained + an ML showcase; the smart-contract needs a chain + legal review).
10. **KeyLattice extension scope** — which features, and whether it's P1 for you.
11. **Style samples** — which writing of yours to fingerprint (posts? emails? reports?), and whether you'll curate approved samples as the benchmark for WS-13.

---

## Appendix A — LLM-struggle map (why the tool split exists)

| Task | Why LLMs fail (2026) | Execution layer |
|---|---|---|
| Photo → real-world measurements | No reliable pixel→units grounding | Perspective/vanishing-point geometry (OpenCV) |
| Photo → garment recreation | Can't emit spline-consistent geometry | Parametric SVG/polyline generator + panel solver |
| Origami crease patterns | Combinatorial constraint satisfaction | Crease-pattern solver (graph problem) |
| Exercise stick figures | Joint-angle consistency across frames | Kinematics/skeletal-animation renderer |
| MIDI drummer from drumless track | Approximates groove, not exact | Rhythm/velocity engine + transcription model |
| Metrical lyrics | Miscounts strong/weak syllables | Syllabifier (CMU-style) + LLM content |
| Cryptograms | Can't exhaust decoding avenues | Cipher/solver library + LLM orchestration |
| Algo backtesting | Fudges numbers | Deterministic backtest engine |
| Style transfer (100 illustrations) | Can't learn style in-context | Fine-tune a generative model (WS-9) |