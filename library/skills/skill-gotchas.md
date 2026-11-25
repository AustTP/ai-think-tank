# Skill Gotchas: carry what testing the skill taught you

Adopted from Warp Factories software-factory material (@BHolmesDev article,
2026-11-24): the gotchas in a working skill are knowledge the agent DISCOVERED
testing that skill over time, and the skill is where that knowledge lives.
jev_lessons.json generalizes teacher verdicts by case-class; gotchas hold the
mechanical discoveries (flags, formats, timing) that no verdict key captures.

## Purpose
Reach for this when writing or revising a skill in `library/skills/`. If the
skill has been exercised against real work, it should end with a `## Gotchas`
section listing what the run taught you that the clean "Key facts" section
cannot: the flag you must pass, the format that is not what the docs claim,
the ordering constraint, the permission that silently fails.

## Key facts
- A skill that has been used successfully at least once earns a `## Gotchas`
  section. A skill still untested stays without one; invented gotchas are
  worse than none.
- One line per gotcha, specific enough that the next run skips rediscovering
  it: name the exact flag, path, format, or timing, then the failure it
  avoids.
- Gotchas precede the Sources section and are quoted in prompts the same way
  as Key facts (sections kept by load_skill_prompt are quoted literally, so
  "Gotchas" must be added to _PROMPT_SECTIONS when a prompt should see it).
- A gotcha that later proves wrong gets deleted, not annotated: the section
  is working knowledge, not a changelog.

## Sources
- @BHolmesDev article 2026-11-24 (verification-by-computer-use skill with a
  Gotchas section accumulated over time).
- `world/agents/skill_prompts.py` (`load_skill`, `load_skill_prompt`) for
  how skill sections reach prompts.
