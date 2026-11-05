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

## Guardrails

- Only youtube.com / youtu.be hosts.
- Respect rate limits: one video at a time, back off on 429s.
- Store findings under `library/research/` and cite the video URL.
- Never upload the tank's credentials anywhere; credentials come only
  through the ask flow, and YouTube research needs none.
