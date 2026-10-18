# Browser Recording

## Purpose
Reach for this whenever a browser flow must be done by hand so an agent can
replay it later, or when teaching material about a site is needed: logins
with captcha or 2FA, OAuth consent screens, single-page apps that defeat the
act driver, password resets, one-off export flows. A recording restates human
browser work in a wire format instead of prose; it is the think tank's
equivalent of a Photoshop Action.

## Key facts
- It is a host-side operator tool, not an agent primitive. Run it from a host
  terminal: `python3 scripts/browser_record.py --label "huggingface login"
  [--url URL]`, drive the task in the window that opens, then Ctrl-C in the
  terminal to finish and write the artifacts.
- The recorder runs a headful Chromium under the DevTools protocol (CDP) and
  captures passively: page loads (with interactive fields and links), clicks
  (with the element description and a stable selector), typed values (masked
  when the field is sensitive, with a character count), Enter/submit presses,
  SPA route changes, and the network rows each step produced.
- The page is never touched with free-form JS. The only injected code is a
  fixed listener bundle that reports through one CDP binding
  (`__browserRecordSink`); the same fixed-op philosophy as
  `world/sandbox/browser_driver.py`.
- Redaction is not optional and happens BEFORE anything reaches disk. A field
  whose type, id, name, placeholder, autocomplete, aria-label, visible label
  or surrounding text hints at a secret (password, otp, 2fa, token, secret,
  api key, card number, ssn, ...) keeps only a length-preserving placeholder:
  `[redacted] len 12`. Credential-bearing query values lose their value but
  keep the parameter name; headers named authorization, cookie, x-api-key or
  matching token/secret/api-key/cookie/auth are replaced wholesale. Request
  and response bodies are never captured at all, so nothing can leak through
  that path.
- Each session writes one atomically renamed directory under
  `library/recordings/`, named `rec-YYYYmmdd-HHMMSS-xxxx-<slug>`:
  - `session.txt` -- the numbered transcript (locator, action, value, page
    context, screenshot path). This is the document agents read to learn.
  - `session.json` -- the machine log: steps, page snapshots, shot SHA-256s,
    and the network rows behind each step.
  - `browser_act_actions.json` -- the same flow restated in the exact
    browser_driver.py wire format (ops: goto/fill/click/submit), ready for
    replay.
  - `shots/step-NNN.png` -- screenshots taken over CDP itself
    (Page.captureScreenshot), so no OS screen-recording permission applies.
- `meta.artifacts` maps every artifact to its SHA-256. Verify the digests
  before trusting or sending a recording; a mismatch means the file changed
  after the fact.
- Replay path: `browser_act_actions.json` goes through `/api/browser-act`,
  one op at a time, each op Jev-gated. Every `[redacted]` value must be
  supplied at replay time from the credential vault -- the artifact never
  carries the value.
- If the capture crashes, the partial session is still written and
  `meta.aborted` names the error. Treat that session as partial.
- Caps keep artifacts bounded: 40 fields and 40 links per page, 120-char
  values, 2000 steps, 4000 network rows, 1.5s minimum gap between screenshots
  on one page. `--max-minutes` is a hard stop; 0 means run until Ctrl-C.

## Policy for this think tank
1. Record, don't narrate. Any browser flow an agent will repeat gets a
   recording first; stories about clicks do not replay.
2. Run it from the host terminal with a --label; the label slug becomes the
   directory name, so name it after the task.
3. Never edit an artifact by hand. If the flow changed, record a fresh
   session.
4. Before replaying, check the digests and source redacted values from the
   credential vault. Pastes of live credentials into any Library document
   are treated as a red-line incident.
5. Verify digests before sending a recording to a client; a recording is a
   deliverable under the house format like anything else.

## Sources
- `scripts/browser_record.py` -- the module docstring is the specification;
  the redaction regexes and caps live in the module.
- `world/sandbox/browser_driver.py` -- the wire format
  `browser_act_actions.json` restates.

## Lessons learned
- The PumpIntegration hang: an inline screenshot wait drained the shared
  event queue, so a nested reader could eat the end-of-stream marker or
  stall the pump. Fix: replies resolve by id wherever the code that issued
  the call continues; the screenshot call is fire-and-forget and its future
  is awaited out-of-band while the pump keeps consuming. Never block inside
  an event handler.
- A length-preserving placeholder beats a bare `[redacted]`: "typed 12
  chars" teaches the flow and leaks only a length.