# Project board

`project_board` is the read-only cowork view over the tank's tasks.

## Filters
- `team`: team or director id.
- `project`: project label or title substring.
- `status`: `open`, `done`, or `all`.
- `limit`: max cards, default 30.

## Each card
Task id, title, status, owner, team, project label, priority, budget, spend,
acceptance criteria, report path, whether the artifact exists on disk, and
the parent task id for delegated forks.

## Use it
- Start a shift by reading the open board for your team.
- Check `artifactExists` before calling a report delivered.
- Follow `parentTaskId` to see which supervisor card forked a delegated card.
- Use it with `supervisor_report` for team-scoped process health.

The board is read-only. It never assigns, edits, or closes a card.
