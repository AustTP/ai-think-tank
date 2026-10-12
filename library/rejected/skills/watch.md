# Skill: watch

## Purpose

Use this skill to check a monitored source for changes, announcements, and real-world examples of a feature, within a stated search window. It only works on sources that change over time and are maintained for monitoring, such as vendor changelogs, release feeds, status pages, and versioned docs sections.

## Premise check

The request asks me to watch `https://example.com`. That assumes the page is a source worth monitoring. It is not. The page says it is "for use in documentation examples without needing permission," that it "is not a service," and to "avoid relying on it for testing and monitoring purposes." Watching it will return nothing useful, and that is the correct result.

The request also gives no search window. Without one, "recent" has no fixed meaning, so I can't say whether anything fell inside it.

The better question is which real source the user wants watched, and over what dates. If the user is testing this skill, a stable placeholder like example.com works as a null case. It does not work as a monitored source.

## Key facts

- The only source supplied is the text of `https://example.com`: a short placeholder page reading "Example Domain."
- The page disclaims service status and advises against using it for testing or monitoring. That is a documented restriction on the source, stated by

## Rejected

Reviewed and rejected on 2026-10-10T11:42:06.888077+00:00 -- judged not accurate/relevant enough to keep as trusted reference material.
