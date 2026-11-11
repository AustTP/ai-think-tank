# Habit Link (browser extension)

Metadata-only habit feed for the think tank. Tells the tank which sites you
personally return to (host, title, time on page, scroll depth, clicks) so
agents can learn your real usage instead of guessing from their own sandbox
browsing.

**Never collected:** page text, form contents, cookies, localStorage,
query strings (stripped server-side), incognito tabs, pages under 1.5s.

## Why this has value when agents already browse

The tank's agents browse in their own sandboxed Chromium under a Jev gate.
That browsing is agent-driven and sees a different internet than you do.
Your actual habits are the strongest standing signal a host deserves agent
reach: a site you personally use every day is exactly the kind of host the
allowlist-candidate flow (the same one agent browse-trails earn) should
surface to you. Habit data also ranks digests and research by what you
actually read, not just what is newest.

## Install — Chrome / Edge / Brave

1. `chrome://extensions` → enable Developer mode
2. "Load unpacked" → select this directory
3. Open the popup → Options → paste the tank URL and the pairing token
   (`HABIT_LINK_TOKEN` from the tank's `.env`)

## Install — Safari (macOS)

Safari runs Chrome-style WebExtensions through a converter:

```
xcrun safari-web-extension-converter /Users/poole86/ai-village-template/world/extensions/habit-link --project-location /tmp/habit-link-safari --app-name "Habit Link" --bundle-identifier com.poole86.habit-link
```

Open the generated Xcode project, run it once, then enable the extension in
Safari → Settings → Extensions. Grant "access to website content" as you
see fit (per-site is fine).

## Turning it OFF (any one of these is enough)

1. **Toolbar popup → "Pause for 1 hour"** (or Resume later)
2. **Options page → block specific hosts** (e.g. your bank)
3. **Server toggle:** `POST /api/habit-link/toggle {"enabled": false}`
   (persists across restarts; `{"enabled": true}` to re-enable)
4. **Hard off:** `HABIT_LINK_ENABLED=0` in the tank's `.env`, or simply
   remove `HABIT_LINK_TOKEN` from `.env` (unset token = feature is 501-off
   at the server)

When the server is off, the extension notices within 5 minutes, drops its
buffer, and sends nothing.

## What the tank does with it

- `GET /api/habit-link/summary?days=14` — top hosts by your usage
  (player/director only). Feeds digest ranking.
- After 10+ hits across 3+ distinct days on a host the agents cannot yet
  reach, the tank files the standard "allowlist candidate" escalation —
  you approve or deny it like any other allowlist decision. It fires once
  per host.
- Everything is aggregated per host per day in `habit_link_days`; raw
  events are never stored.
