# Benchmark Scores Must Come From a Consistent Harness

Source: GPT 6 Astra coverage (Sep 2026). The same model scored 99.9% on
ARC-AGI 3 under OpenAI's custom Provider Adapter harness and 62.7% under the
standard ARC Prize harness. One model, two harnesses, a 37.2-point gap. The
headline number cannot be interpreted separately from the conditions that
produced it.

## The rule

Every score in `model_benchmark_scores` that feeds `refresh_model_tiers`
must come from a benchmark run under a consistent harness, with a real
`source_url` citation on the row.

This is already the design rationale behind `BAND_BENCHMARK` (serve.py):
IFEval was rejected for band ranking precisely because its cross-vendor
coverage came from different harnesses with non-comparable numbers. MMLU,
MMLU-Pro, SWE-bench Verified, Humanity's Last Exam, and MMMU were chosen
because they have consistent, comparable public reporting across the wide
range of vendors the catalog draws from.

## Applying it when curating a model

1. When a new model (e.g. GPT 6 Astra) enters the catalog, look at the
   benchmark number proposed for its band.
2. Check that the number comes from the standard harness for that benchmark,
   not a vendor-custom adapter or a single-run cherry-pick.
3. If the only public numbers are vendor-harness numbers (like the 99.9%
   ARC-AGI result), do not enter the score. Wait for an independent,
   standard-harness result, or leave the model out of the band.
4. Record the `source_url` on every score row so the pick can be audited.

## Why it matters here

`refresh_model_tiers` picks "best score within the quality floor gap, then
cheapest." A single inflated vendor-harness score can silently win a band
for a model that would lose under a fair harness, and cost the think tank
real money on every call in that band. The floor gap (BENCHMARK_QUALITY_FLOOR_GAP)
protects against a meaningfully worse model winning on price; harness
consistency protects against a meaningfully worse score winning the ranking.