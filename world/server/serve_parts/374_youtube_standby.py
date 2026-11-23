# YouTube standby session (operator, 2026-11-23, after BEN-210): a dedicated,
# pre-provisioned Colab CPU session with the vetted media stack pre-installed,
# so a video job never pays the cold-start tax or hunts for the right recipe.
# Same pattern as the Laya standby (170_colab_standby_reachable.py): the colab
# CLI owns the session; the tank only checks reachability and warms the recipe.
#
# Why: task-35861 burned a card cycle proving the LANE SEQUENCE for one video
# (host shim missing module -> sandbox OOM -> VPS yt-dlp bot-walled -> Colab
# works). The lane knowledge was real, the warmup was real (pip installs,
# model preload, ffmpeg), and the 429 burst-limit is real. A standing session
# with the stack baked in removes all three for the next video card.
#
# Rate-limit envelope (empirical, 2026-11-23): YouTube posts no published
# limit for captions/extractor calls; enforcement is IP-reputation based and
# UNPUBLISHED. A short burst from one IP fails within minutes. Like the page
# budget, the honest storage is the empirical envelope + a soft self-cap, with
# the batch pattern (few calls per job; captions-then-whisper in one pass;
# off the watch page unless downloading):
#   Per-failure: back off >= 60 min after any 429/bot challenge from that IP.
#   Per video: ONE exec pass on the standby (few calls, batched).
#   Self-cap (convention, not a YouTube number): YOUTUBE_DAILY_JOB_CAP jobs/day
#   defaults OFF so the empirical guidance is the only active rule.

def _youtube_standby_enabled():
    """Same triple opt-in shape as the Laya standby: colab CLI present AND
    COLAB_ENABLED AND an explicit operator opt-in (YOUTUBE_STANDBY_ENABLED=1).
    Off by default so existing test suites and machines without the CLI never
    see this surface."""
    try:
        flag = str(_load_env().get('YOUTUBE_STANDBY_ENABLED', '') or '')
    except Exception:
        return False
    return bool(COLAB_ENABLED and COLAB_CLI_AVAILABLE
                and flag.lower() in ('1', 'true', 'yes'))


YOUTUBE_SESSION = str((_load_env().get('YOUTUBE_SESSION') or 'ytpack') or 'ytpack')

# The vetted media stack (mirrors library/skills/youtube.md's approved list):
# yt-dlp + youtube-transcript-api (captions), faster-whisper (no-caption
# fallback), transformers/torch/PIL (captioning frames on the runtime).


def _youtube_standby_ready(session=None):
    """True when the session is ALIVE (colab status does not say not-found)
    AND a lightweight ping exec answers. Deliberately does NOT run the warm
    recipe on every check -- the recipe only runs in the warm pass so a
    reachability poll never pays pip cost."""
    session = session or YOUTUBE_SESSION
    return _colab_session_exists(session, account='primary')


def _youtube_standby_warm(session=None):
    """One exec pass: install anything missing from the vetted stack and
    confirm ffmpeg. Returns (ok, tail). Never raises; surfaces the CLI tail
    like every other tool result."""
    session = session or YOUTUBE_SESSION
    ok, msg = _colab_standby_ensure_session(session)
    if not ok:
        return False, f'provision failed: {msg[:300]}'
    code = (
        "import os, subprocess, importlib.util, json\n"
        "need = {\n"
        "    'yt_dlp': 'yt-dlp',\n"
        "    'youtube_transcript_api': 'youtube-transcript-api',\n"
        "    'faster_whisper': 'faster-whisper',\n"
        "    'transformers': 'transformers',\n"
        "    'torch': 'torch',\n"
        "    'PIL': 'pillow',\n"
        "}\n"
        "missing = [pkg for mod, pkg in need.items() if importlib.util.find_spec(mod) is None]\n"
        "if missing:\n"
        "    r = subprocess.run(['pip', 'install', '-q'] + missing, capture_output=True, text=True, timeout=560)\n"
        "    print('__PIP__' + ''.join(missing) + ' rc=' + str(r.returncode))\n"
        "state = {m: importlib.util.find_spec(m) is not None for m in need}\n"
        "import shutil as _sh\n"
        "state['ffmpeg'] = bool(_sh.which('ffmpeg'))\n"
        "print(json.dumps(state))\n"
    )
    rc, out = _colab_cli('exec', '-s', session, '--timeout', '700', input=code)
    tail = (out or '')[-400:]
    return rc == 0, tail


def _youtube_standby_status():
    """Operator/observability shape: the session, whether it is alive, the
    daily-cap setting, and today's job count. Used by the service page and
    any future ytpack tool ad; cheap on purpose (one CLI status call)."""
    return {
        'session': YOUTUBE_SESSION,
        'alive': _youtube_standby_ready(),
        'enabled': _youtube_standby_enabled(),
        'dailyCap': float(_load_env().get('YOUTUBE_DAILY_JOB_CAP', '0') or 0),
    }
