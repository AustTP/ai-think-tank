# YouTube research (teams own this)

The tank hardcodes almost nothing for YouTube. It gives you neutral
primitives and an approved tool list; this file is the recipe book. If a
recipe stops working, fix the recipe here rather than asking the tank for a
new hardcoded endpoint.

## Approved tools (trusted-codebase registry)

- `jdepoix/youtube-transcript-api` — fetch a video's own captions. Fast,
  no audio, works locally.
- `yt-dlp/yt-dlp` — media extraction when captions are not enough.
- `SYSTRAN/faster-whisper` — local speech-to-text for captionless media.

All are pip-installable in your sandbox via `/api/execute`
(`pip install <pkg>`). They are vetted; anything else needs a
trusted-codebase approval first.

## IP reputation wall (operator-verified 2026-11-27, task: pXro0)

When an egress IP is reputation-flagged, "Sign in to confirm you're not
a bot" is returned for EVERY video (even 2005 uploads) on every player
client, with or without cookies. Findings from a full day of testing:

- PO tokens do NOT clear it. `Brainicism/bgutil-ytdlp-pot-provider`
  minted valid player tokens for the video and yt-dlp attached them;
  the player response stayed LOGIN_REQUIRED. Deployed and kept:
  container `bgutil-provider` on 127.0.0.1:4416 (started with
  `docker run --name bgutil-provider -d --init -p 127.0.0.1:4416:4416
  brainicism/bgutil-ytdlp-pot-provider`).
- `coletdjnz/yt-dlp-getpot-wpc` (browser minting via nodriver) is
  installed too (`uv tool install yt-dlp --with bgutil-ytdlp-pot-provider
  --with yt-dlp-getpot-wpc --force`); it needs
  `--extractor-args "youtubepot-wpc:browser_path=<chrome>"` and a
  chromium binary (`~/.cache/ms-playwright/chromium-1255/chrome-linux-arm64/chrome`).
  It spawns chrome processes that linger after the run; kill the
  ubuntu-owned ones only. NEVER `pkill -f chrome` broadly: the tank's
  own captcha-solver chrome runs as user `opc`.
- Force the token path with
  `--extractor-args "youtube:player_client=web;fetch_pot=always"`
  (separate `--extractor-args` per namespace; the legacy plugin zip
  layout is NOT scanned by yt-dlp 2026.11 — install via uv/pip, not
  `~/.config/yt-dlp/plugins`).
- Instance hopping is dead too: public Invidious and Piped instances
  error with the same `SignInConfirmNotBotException` from their own
  datacenter IPs.
- The only egress-independent fix is a LOGGED-IN Google session
  (logged-in requests pass where guest requests fail). The vault
  profile's Google session gets server-side signed out automatically;
  re-login needs the password, which we do not store. Route through the
  ask flow, or run the extraction on a residential machine.
- Watch-page DOM metadata (title, channel, views, description from
  ytInitialData) stays readable even when walled. Use it to at least
  identify the video.

## Recipe 1: transcript via captions (default)

```
pip install youtube-transcript-api
python - <<'PY'
from youtube_transcript_api import YouTubeTranscriptApi
parts = YouTubeTranscriptApi().fetch('VIDEO_ID', languages=['en'])
text = ' '.join(s.text for s in parts)
print(text[:2000])
PY
```

- Resolve VIDEO_ID from watch?v=, /shorts/, /live/, or youtu.be forms.
- Try the video's spoken language first, then `en`.
- Captions absent or empty → Recipe 2.

## Recipe 2: no captions — audio then transcribe

```
pip install yt-dlp faster-whisper
yt-dlp -x --audio-format mp3 -o audio.vid 'VIDEO_URL'
python - <<'PY'
from faster_whisper import WhisperModel
m = WhisperModel('small')          # CPU-friendly
segs, _ = m.transcribe('audio.vid.mp3')
print(' '.join(s.text for s in segs)[:2000])
PY
```

Heavy: runs minutes of CPU. Mark the card resource class appropriately and
keep the sandbox alive until the transcript is saved.

## Recipe 3: server convenience endpoint (fallback only)

`POST /api/youtube-transcript {url, lang}` — a thin shim over
youtube-transcript-api, with a Colab audio path behind it for captionless
videos. Prefer Recipe 1 in your own sandbox: it is yours, it is auditable,
and it survives tank restarts unchanged.

## Recipe 4: third-party transcript tools (when every direct lane is IP-walled)

This is one instance of the general pattern: when a lane is walled,
search for a free hosted service that does the job with its own
infrastructure. See `library/wiki/think_tank/wall-break-playbook.md`.

Operator-verified 2026-11-27 against a 1-day-old upload while the host
IP was fully bot-walled. These services fetch the caption track with
THEIR infrastructure; our IP reputation is irrelevant to the upstream
fetch, and the watch page stays readable enough for identification:

- `youtubescribe.com` — WORKED end to end in the browser: paste link,
  "Get transcript", full auto-caption track with timestamps (409 cues
  for a 14-min video). Free for the first transcript per session; parse
  the rendered page (cue lines are `MM:SS` timestamp + text) instead of
  re-submitting. Do NOT trust the page's duration/word-count over
  YouTube's own when a sidebar number was misread.
- `notegpt.io`, `getyoutubetext.com`, `vidscript.co` — page loads but
  the fetch returned no video content.
- `youtubetotranscript.com` (403), `kome.ai` (Vercel checkpoint),
  `tactiq.io` (needs Firebase App Check token) — blocked to curl AND
  browser that day.
- Pattern: drive the tank's browser (session-vault chrome over CDP),
  fill the URL box, click submit, wait ~10 s, then parse
  `document.body.innerText` for cue lines. Keep transcripts under the
  same provenance rules as Recipe 1 (internal inspiration, cite URL,
  never republish).

## Guardrails

- Only youtube.com / youtu.be hosts.
- Respect rate limits: one video at a time, back off on 429s.
- Store findings under `library/research/` and cite the video URL.
- Never upload the tank's credentials anywhere; credentials come only
  through the ask flow, and YouTube research needs none.

## Rate limits (empirical, operator-verified 2026-11-23, BEN-210)

YouTube posts NO published rate limit for the lanes below. The only
official quoted quota is the YouTube Data API (10,000 units/day per
project; search costs 100 units -- we do not use it). Everything else is
reputation-based and unannounced. The empirical envelope (task-35861):

- Watch-page/extractor calls (yt-dlp, youtube-transcript-api, the
  transcript shim): a short burst from one IP fails within minutes
  (HTTP 429 on captions; "Sign in to confirm you're not a bot" on the
  watch page). Cloud/VPS/Colab IPs are challenged far earlier than
  residential ones. Published limit: none. Work like there is one.
- Back off AT LEAST 60 minutes after any 429 or bot challenge from
  that IP; do not burn retries (the stop rule applies).
- Batch extractor work: one session, few calls, captions-then-whisper
  in a single pass. Never probe the watch page per-frame.
- `i.ytimg.com` thumbnails and the oEmbed endpoint are CDN-served and
  effectively unmetered; prefer them for identity/metadata checks.
- Colab is the escape hatch (Google-egress IPs): run the whole job
  there in ONE exec (install, fetch, transcribe, save) rather than
  round-tripping between host and runtime.
- The host transcript shim needs youtube-transcript-api installed in
  the server venv; if it ModuleNotFound-errors, fix the venv first --
  do not file a card about the tool being "broken."
