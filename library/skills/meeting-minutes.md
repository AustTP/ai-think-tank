# Meeting Minutes

## Purpose
Use this when a meeting has already happened in-world and you are writing the
minutes. Stay inside the agenda.

## Key facts
- Write plain Markdown with exactly these sections:
  - **Topic**: the meeting title or goal, one line.
  - **Attendees**: the named participants, facilitator first.
  - **Discussion**: 2-4 bullets in the participants' voices, grounded in the
    agenda only.
  - **Decisions**: what the group committed to, or "None yet".
  - **Action items**: "owner: concrete next step", one per line, or "None".
- Invent no external facts and no decisions the agenda cannot support.
- If the agenda names hard prohibitions (a NEVER section), the minutes must
  respect them.
- Under 200 words.

## Policy for this think tank
1. The agenda is the only source of truth.
2. Do not add attendees, decisions, or action items that are not supported by
   the agenda.
3. Put the facilitator's name first in Attendees.

## Sources
- `world/content.py` meeting executor.
- `library/skills/house-format.md` for style.

## Lessons learned
- Minutes drift when the model adds plausible discussion. Keep every bullet
  tied to the agenda.
