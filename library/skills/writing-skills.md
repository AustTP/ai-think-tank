# Writing skills

Source: obra/superpowers @ 2ce8aa1 (MIT), skills/writing-skills; adapted to house format 2026-10-31.

## Purpose
Reach for this whenever you are about to author a skill candidate for the pending_review
lane, or edit an existing library skill. It exists because this tank's quarantine fills with
candidates that describe one incident instead of teaching a repeatable method, and because
an untested skill is a guess wearing a manual's clothes.

## Key facts
- Writing a skill is TDD applied to process documentation. If you did not watch an agent
  fail WITHOUT the skill, you do not know the skill teaches the right thing.
- The mapping: baseline run (no skill) = RED; document the exact rationalizations and
  mistakes observed; write the skill addressing THOSE violations; re-run the same scenario
  with the skill present = GREEN; every new loophole found becomes the next RED.
- A skill is a reusable technique, pattern, or reference. A skill is NOT a narrative about
  one problem solved once, and NOT standard practice documented elsewhere.
- Trigger text says only WHEN to reach for the skill ("Reach for this whenever..."), never
  a summary of its workflow -- a summarized workflow gets skimmed instead of followed.
- Do not create a skill for what a check can enforce mechanically. Automate the check;
  reserve documentation for judgment calls.
- Keep inline: principles, short code patterns. Split out only heavy reference (100+ lines)
  or reusable scripts.

## Policy for this think tank
- House format is a flat .md in library/skills: `# Title`, then `## Purpose`, `## Key
  facts`, `## Policy for this think tank`, `## Sources`. No YAML frontmatter, no scripts
  unless the director approves vendoring them. See research-desk.md for the model.
- Agents have no direct write to the live skill set: candidates land in
  library/pending_review/skills/ and a director or the player promotes or rejects. Promotion
  is never automatic.
- Before proposing, run the baseline: pick a recent card where the failure the skill targets
  actually happened, note what the agent did without the skill, and say so in the candidate.
  A candidate that cannot name its baseline evidence goes back.
- One skill teaches one method. If your candidate needs "and also", it is two candidates.

## Sources
- obra/superpowers skills/writing-skills/SKILL.md @ 2ce8aa1 (MIT license), including its
  references/ directory (discovery, anthropic best practices, flowcharts) for deeper guidance.
