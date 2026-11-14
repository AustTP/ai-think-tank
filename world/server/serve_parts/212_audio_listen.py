# ------------------------------------------------------- live audio listen --
# Operator request (2026-11-14): let the tank listen to a live audio feed.
# Capture stays LOCAL on purpose: a 24/7 ffmpeg ingest needs process
# supervision (launchd restarts this tank; a Colab runtime dies on idle and
# at its 12h cap), so Colab is not the home for the listener. Transcription
# rides Groq's free whisper tier, whose quota is its own per-endpoint
# bucket -- chat models draw from separate limits, so Groq stays in the
# chat free pool untouched. The listener budgets itself under the whisper
# ceiling (_AUDIO_LISTEN_TRANSCRIBE_PER_MIN) and honors 429 Retry-After,
# the same discipline as the chat transport. Feeds are operator-declared
# via AUDIO_LISTEN_FEEDS ("label=url,..." -- anything ffmpeg reads:
# icecast, HLS, RTSP); the feature is fully dormant when the env is unset,
# and villages never listen. Transcript text lands in a per-feed ring
# buffer, keyword regex hits push to the telegram bridge, and a 30-minute
# digest markdown lands next to the telegram digests under
# library/analysis. Transcript lines are third-party speech: they flow
# through the same bounded-file sink as every other digest, never into
# kv_state read-modify-write races.

import asyncio
import os
import re
import shutil
import subprocess
import time
import urllib.error
import uuid

AUDIO_LISTEN_FEEDS_ENV = 'AUDIO_LISTEN_FEEDS'
AUDIO_LISTEN_KEYWORDS_ENV = 'AUDIO_LISTEN_KEYWORDS'
_AUDIO_TRANSCRIBE_URL = 'https://api.groq.com/openai/v1/audio/transcriptions'
_AUDIO_TRANSCRIBE_MODEL = 'whisper-large-v3-turbo'
_AUDIO_LISTEN_SEGMENT_S = 30
_AUDIO_LISTEN_POLL_S = 15.0
_AUDIO_LISTEN_DIGEST_S = 1800.0
_AUDIO_LISTEN_RING_MAX = 500
_AUDIO_LISTEN_TRANSCRIBE_PER_MIN = 10
_AUDIO_LISTEN_INFLIGHT_MAX = 3
_AUDIO_LISTEN_OPEN_MIN_AGE_S = 12.0
_AUDIO_LISTEN_SEG_MAX_AGE_S = 600.0
_AUDIO_LISTEN_ALERT_DEDUPE_S = 300.0
_AUDIO_LISTEN_FFMPEG_BACKOFF_MAX_S = 300.0

# Colab overflow leg (operator, 2026-11-14): multiple feeds can saturate the
# Groq whisper free-tier gate (10/min shared across ALL feeds), and segments
# the gate refused age out through the sweep after 10 minutes -- lost
# transcripts under sustained load. When AUDIO_LISTEN_COLAB=1, segments the
# Groq leg cannot take (rate-refused or failed) queue for a BATCHED job on
# the standby's CPU runtime (faster-whisper on the runtime, one tar upload
# + one exec per batch of 8). The listener only BORROWS an already-alive
# CPU session -- it never provisions compute of its own, so the leg costs
# nothing when the standby is stood down and silently falls back to the
# Groq leg when it is not.
AUDIO_LISTEN_COLAB_ENV = 'AUDIO_LISTEN_COLAB'
_AUDIO_COLAB_BATCH_MAX = 8
_AUDIO_COLAB_QUEUE_MAX = 24
_AUDIO_COLAB_BACKOFF_S = 120.0
_AUDIO_COLAB_EXEC_TIMEOUT_S = 600.0
_AUDIO_COLAB_MODEL = 'small'
_AUDIO_COLAB_RESULT_TAG = 'AUDIO_BATCH_RESULT '

_audio_state = {'feeds': [], 'alerts_last': {}, 'digest_at': 0.0, 'rate': [],
                'patterns': [], 'colab': [], 'colab_pending': set(),
                'colab_next': 0.0, 'colab_enabled': False, 'colab_ok': None}
# Indirection so tests can fast-forward retry backoffs without real sleeps.
_audio_transcribe_sleep = time.sleep


def _audio_feeds_parse(raw):
    """Parse AUDIO_LISTEN_FEEDS ("label=url,label=url") into [(label, url)].
    Bad entries are skipped, never fatal: a typo in one feed must not
    silence the others."""
    feeds = []
    for part in str(raw or '').split(','):
        part = part.strip()
        if not part or '=' not in part:
            continue
        label, _, url = part.partition('=')
        label = label.strip().lower()
        url = url.strip()
        if not label or not re.match(r'^[a-z0-9_-]+$', label) \
                or '://' not in url:
            continue
        feeds.append((label, url))
    return feeds


def _audio_feed_dir(label):
    return os.path.join(THINK_TANK_DIR, 'audio-listen', label)


def _audio_ffmpeg_cmd(url, pattern):
    return ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-i', url, '-vn', '-ac', '1', '-ar', '16000',
            '-f', 'segment', '-segment_time', str(_AUDIO_LISTEN_SEGMENT_S),
            '-reset_timestamps', '1', pattern]


def _audio_ready_segments(feed, now):
    """Closed segments only: the segment muxer keeps writing the newest
    file, so anything younger than _AUDIO_LISTEN_OPEN_MIN_AGE_S is still
    live and must not be transcribed mid-write."""
    try:
        names = os.listdir(feed['dir'])
    except OSError:
        return []
    segs = []
    for name in names:
        if not name.endswith('.wav'):
            continue
        path = os.path.join(feed['dir'], name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if now - mtime < _AUDIO_LISTEN_OPEN_MIN_AGE_S:
            continue
        segs.append((mtime, path))
    segs.sort()
    return [path for _, path in segs]


def _audio_sweep_segments(feed, now):
    """Delete segments older than _AUDIO_LISTEN_SEG_MAX_AGE_S: crash
    leftovers and 429-stuck chunks must not accumulate on disk."""
    try:
        names = os.listdir(feed['dir'])
    except OSError:
        return
    for name in names:
        if not name.endswith('.wav'):
            continue
        path = os.path.join(feed['dir'], name)
        try:
            if now - os.path.getmtime(path) > _AUDIO_LISTEN_SEG_MAX_AGE_S:
                os.remove(path)
        except OSError:
            continue


def _audio_multipart(boundary, fields, filename, data):
    parts = []
    for name, value in fields.items():
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"'
                      '\r\n\r\n%s\r\n' % (boundary, name, value))
                     .encode('utf-8'))
    parts.append(('--%s\r\nContent-Disposition: form-data; name="file"; '
                  'filename="%s"\r\nContent-Type: audio/wav\r\n\r\n'
                  % (boundary, filename)).encode('utf-8'))
    parts.append(data)
    parts.append(('\r\n--%s--\r\n' % boundary).encode('utf-8'))
    return b''.join(parts)


def _audio_rate_ok(now):
    """Sliding 60s window under the whisper free-tier ceiling (~20 RPM
    published; the listener holds itself to half of it so digests, retries
    and a second feed cannot crowd the bucket)."""
    window = _audio_state['rate']
    while window and now - window[0] > 60.0:
        window.pop(0)
    if len(window) >= _AUDIO_LISTEN_TRANSCRIBE_PER_MIN:
        return False
    window.append(now)
    return True


def _audio_transcribe_wav(path):
    """One Groq whisper transcription of a closed segment. The multipart
    POST rides _urlopen_with_resilience like every other outbound call.
    One 429 retry honoring Retry-After; every other failure raises so the
    caller keeps the file on disk for the next tick."""
    key = (_load_env().get('GROQ_API_KEY') or '').strip()
    if not key:
        raise RuntimeError('audio-listen: no GROQ_API_KEY -- transcription '
                           'unavailable')
    with open(path, 'rb') as f:
        data = f.read()
    boundary = uuid.uuid4().hex
    body = _audio_multipart(
        boundary,
        {'model': _AUDIO_TRANSCRIBE_MODEL, 'response_format': 'text',
         'temperature': '0'},
        os.path.basename(path), data)
    headers = {'Authorization': 'Bearer %s' % key,
               'Content-Type': 'multipart/form-data; boundary=%s' % boundary,
               'User-Agent': 'AIThinkTankAgent/1.0'}
    for attempt in (0, 1):
        req = urllib.request.Request(_AUDIO_TRANSCRIBE_URL, data=body,
                                     headers=headers, method='POST')
        try:
            raw = _urlopen_with_resilience(req, timeout=120)
            if isinstance(raw, bytes):
                raw = raw.decode('utf-8', errors='replace')
            return str(raw).strip()
        except urllib.error.HTTPError as e:
            if attempt == 0 and e.code == 429:
                try:
                    retry_after = float(e.headers.get('Retry-After') or 30.0)
                except (TypeError, ValueError):
                    retry_after = 30.0
                _audio_transcribe_sleep(min(retry_after, 120.0))
                continue
            raise


def _audio_colab_enabled():
    """The overflow leg is opt-in: AUDIO_LISTEN_COLAB=1. Default off keeps
    the feature fully dormant for existing installs."""
    return (_load_env().get(AUDIO_LISTEN_COLAB_ENV) or '').strip() == '1'


def _audio_colab_enqueue(feed, path):
    """Queue a segment for the Colab batch leg. Returns True when the path
    is (or already was) claimed by the leg, so the caller stops treating it
    as Groq work; False when the leg is disabled or the queue is full (the
    file stays on disk and ages out through the sweep exactly as before)."""
    if not _audio_state.get('colab_enabled'):
        return False
    if path in _audio_state['colab_pending']:
        return True
    if len(_audio_state['colab']) >= _AUDIO_COLAB_QUEUE_MAX:
        return False
    _audio_state['colab'].append((feed['label'], path))
    _audio_state['colab_pending'].add(path)
    return True


# Runs ON the Colab runtime per batch: pip once (marker file), untar,
# transcribe every wav, print one tagged JSON line the host parses out of
# stdout. A segment that fails on the runtime yields '' -- an honest drop,
# identical in shape to the Groq leg's empty transcript.
_AUDIO_COLAB_SCRIPT = '''
import json, os, subprocess, tarfile
if not os.path.exists('/tmp/.fw-ok'):
    subprocess.run(['pip', 'install', '-q', 'faster-whisper'], check=False)
    open('/tmp/.fw-ok', 'w').write('1')
from faster_whisper import WhisperModel
os.makedirs('/tmp/audio-batch', exist_ok=True)
with tarfile.open('/tmp/audio-batch.tar') as t:
    t.extractall('/tmp/audio-batch')
model = WhisperModel('__MODEL__', device='cpu', compute_type='int8')
out = {}
for name in sorted(os.listdir('/tmp/audio-batch')):
    if not name.endswith('.wav'):
        continue
    try:
        segs, _info = model.transcribe('/tmp/audio-batch/' + name, beam_size=1)
        out[name] = ' '.join(s.text.strip() for s in segs).strip()
    except Exception:
        out[name] = ''
print('__TAG__' + json.dumps(out))
'''.replace('__MODEL__', _AUDIO_COLAB_MODEL).replace(
    '__TAG__', _AUDIO_COLAB_RESULT_TAG)


def _audio_colab_arcname(label, path):
    return '%s-%s' % (label, os.path.basename(path))


def _audio_colab_transcribe_batch(batch):
    """One batched transcription job on the standby CPU session: tar the
    segments, upload, exec faster-whisper on the runtime, parse the tagged
    result line from stdout. Returns {abs_path: text} for segments that
    produced text. Raises on any HARNESS failure (upload/exec/parse) so the
    caller keeps the whole batch queued and backs off -- a batch is only
    consumed after the runtime answered."""
    import tarfile
    import tempfile
    session = COLAB_CPU_SESSION
    with tempfile.TemporaryDirectory(prefix='audio-colab-') as work:
        tar_path = os.path.join(work, 'audio-batch.tar')
        with tarfile.open(tar_path, 'w') as t:
            for label, path in batch:
                t.add(path, arcname=_audio_colab_arcname(label, path))
        rc, out = _colab_cli('upload', '-s', session, tar_path,
                             '/tmp/audio-batch.tar', timeout=300)
        if rc != 0:
            raise RuntimeError('colab upload failed: %s' % out[:200])
        script = os.path.join(work, 'transcribe.py')
        with open(script, 'w', encoding='utf-8') as fh:
            fh.write(_AUDIO_COLAB_SCRIPT)
        rc, out = _colab_cli('exec', '-s', session, '--file', script,
                             '--timeout', str(_AUDIO_COLAB_EXEC_TIMEOUT_S),
                             timeout=_AUDIO_COLAB_EXEC_TIMEOUT_S + 120.0)
        if rc != 0:
            raise RuntimeError('colab exec failed: %s' % out[:200])
        payload = None
        for line in out.splitlines():
            if line.startswith(_AUDIO_COLAB_RESULT_TAG):
                payload = line[len(_AUDIO_COLAB_RESULT_TAG):]
        if payload is None:
            raise RuntimeError('colab exec produced no result line')
        by_arc = json.loads(payload)
        return {path: str(by_arc[_audio_colab_arcname(label, path)])
                for label, path in batch
                if by_arc.get(_audio_colab_arcname(label, path))}


def _audio_colab_tick(now):
    """Dispatch queued overflow segments to the Colab batch leg. Borrows the
    standby CPU session only when it is already alive; one log line per
    state transition, then a backoff so the listener never hammers the
    runtime. A harness failure keeps the whole batch queued (segments age
    out through the sweep, same bound as before this leg existed)."""
    queue = _audio_state['colab']
    if not queue or now < _audio_state['colab_next']:
        return
    if not COLAB_ENABLED or not _colab_session_exists(COLAB_CPU_SESSION):
        if _audio_state['colab_ok'] is not False:
            _audio_state['colab_ok'] = False
            print('[audio-listen] colab leg idle -- no standby CPU session; '
                  'overflow stays on disk for the Groq leg', flush=True)
        return
    batch = list(queue[:_AUDIO_COLAB_BATCH_MAX])
    _audio_state['colab_next'] = now + _AUDIO_COLAB_BACKOFF_S
    try:
        results = _audio_colab_transcribe_batch(batch)
    except Exception as e:  # noqa: BLE001
        if _audio_state['colab_ok'] is not False:
            _audio_state['colab_ok'] = False
            print('[audio-listen] colab leg failed: %s' % e, flush=True)
        return
    _audio_state['colab_next'] = 0.0
    if _audio_state['colab_ok'] is not True:
        _audio_state['colab_ok'] = True
        print('[audio-listen] colab leg active (%d-segment batch)'
              % len(batch), flush=True)
    for label, path in batch:
        queue.remove((label, path))
        _audio_state['colab_pending'].discard(path)
        try:
            os.remove(path)
        except OSError:
            pass  # gone already (swept); nothing to record
        feed = next((f for f in _audio_state['feeds']
                     if f['label'] == label), None)
        text = results.get(path)
        if feed is not None and text:
            _audio_record_transcript(feed, text, now)


def _audio_alert_patterns(raw):
    """Compile AUDIO_LISTEN_KEYWORDS (CSV regex). A bad regex logs and is
    skipped: one typo must not take down the listener."""
    pats = []
    for kw in str(raw or '').split(','):
        kw = kw.strip()
        if not kw:
            continue
        try:
            pats.append(re.compile(kw, re.IGNORECASE))
        except re.error as e:
            print('[audio-listen] bad keyword regex %r: %s' % (kw, e),
                  flush=True)
    return pats


def _audio_alert_scan(feed_label, text, now):
    """Push keyword matches to the telegram bridge, deduped per
    (feed, pattern) within _AUDIO_LISTEN_ALERT_DEDUPE_S so a repeating
    announcement cannot flood the owner's chat."""
    sent = 0
    for pat in _audio_state.get('patterns') or []:
        if not pat.search(text):
            continue
        dedupe_key = (feed_label, pat.pattern)
        last = _audio_state['alerts_last'].get(dedupe_key, 0.0)
        if now - last < _AUDIO_LISTEN_ALERT_DEDUPE_S:
            continue
        _audio_state['alerts_last'][dedupe_key] = now
        snippet = text if len(text) <= 300 else text[:300] + '...'
        try:
            _telegram_send_text_sync('[audio:%s] match %r: %s'
                                     % (feed_label, pat.pattern, snippet))
        except Exception as e:  # noqa: BLE001
            print('[audio-listen] telegram push failed: %s' % e, flush=True)
        sent += 1
    return sent


def _audio_digest_dir():
    return os.path.join(THINK_TANK_DIR, 'library', 'analysis')


def _audio_digest_write(now):
    """Write the pending transcript window per feed as one digest markdown
    next to the telegram digests, then clear the pending windows. No
    pending text means no file -- silence never produces artifacts."""
    rows = []
    for feed in _audio_state['feeds']:
        if not feed['pending']:
            continue
        rows.append('## %s' % feed['label'])
        for ts, text in feed['pending']:
            rows.append('- [%s] %s' % (time.strftime('%H:%M',
                                                     time.localtime(ts)),
                                       text))
        feed['pending'] = []
    if not rows:
        return None
    os.makedirs(_audio_digest_dir(), exist_ok=True)
    name = 'audio-listen-digest-%s.md' % time.strftime(
        '%Y-%m-%d-%H%M', time.localtime(now))
    path = os.path.join(_audio_digest_dir(), name)
    text = '# audio listen digest %s\n\n%s\n' % (
        time.strftime('%Y-%m-%d %H:%M', time.localtime(now)), '\n'.join(rows))
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)
    print('[audio-listen] digest written: %s' % name, flush=True)
    return path


def _audio_record_transcript(feed, text, now):
    """Shared downstream for both transcription legs: strip, ring, pending,
    alerts. Empty text (silence, or a segment the leg could not transcribe)
    is an honest drop."""
    text = ' '.join((text or '').split())
    if not text:
        return
    entry = (now, text)
    feed['ring'].append(entry)
    del feed['ring'][:-_AUDIO_LISTEN_RING_MAX]
    feed['pending'].append(entry)
    _audio_alert_scan(feed['label'], text, now)


def _audio_handle_segment(feed, path, now):
    """Transcribe one closed segment, record the text, scan alerts, and
    remove the file. On rate-window exhaustion the file stays for the next
    tick; on transcription failure it stays too (bounded by the sweep).
    When the Colab overflow leg is enabled, a segment this leg cannot take
    is queued for the runtime batch instead of aging out under sustained
    multi-feed load."""
    if not _audio_rate_ok(now):
        _audio_colab_enqueue(feed, path)
        return False
    try:
        text = _audio_transcribe_wav(path)
    except Exception as e:  # noqa: BLE001
        print('[audio-listen] %s transcribe failed (%s): %s'
              % (feed['label'], os.path.basename(path), e), flush=True)
        _audio_colab_enqueue(feed, path)
        return False
    try:
        os.remove(path)
    except OSError:
        pass
    _audio_record_transcript(feed, text, now)
    return True


def _audio_feed_ensure(feed, now):
    """Supervise one ffmpeg capture: spawn on first tick, respawn with
    doubling backoff after an exit (streams die; launchd restarts the tank
    but not a dead ffmpeg child)."""
    proc = feed.get('proc')
    if proc is not None and proc.poll() is None:
        return
    if proc is not None:
        if now < feed.get('next_start', 0.0):
            return
        backoff = min(max(feed.get('backoff', 5.0) * 2, 10.0),
                      _AUDIO_LISTEN_FFMPEG_BACKOFF_MAX_S)
        feed['backoff'] = backoff
        feed['next_start'] = now + backoff
        print('[audio-listen] %s ffmpeg exited (%s); retry in %ds'
              % (feed['label'], proc.returncode, int(backoff)), flush=True)
    if now < feed.get('next_start', 0.0):
        return
    os.makedirs(feed['dir'], exist_ok=True)
    pattern = os.path.join(feed['dir'], '%s-%%05d.wav' % feed['label'])
    try:
        feed['proc'] = subprocess.Popen(_audio_ffmpeg_cmd(feed['url'],
                                                          pattern))
        feed['backoff'] = 5.0
        feed['next_start'] = 0.0
        print('[audio-listen] %s capture started' % feed['label'], flush=True)
    except OSError as e:
        feed['next_start'] = now + _AUDIO_LISTEN_FFMPEG_BACKOFF_MAX_S
        print('[audio-listen] %s ffmpeg spawn failed: %s'
              % (feed['label'], e), flush=True)


async def _audio_listen_loop():
    """Owner-level lifespan loop: supervise one ffmpeg capture per feed,
    dispatch closed segments to transcription threads, and write the
    periodic digest. Dormant (returns) without feeds or without ffmpeg."""
    parsed = _audio_feeds_parse(_load_env().get(AUDIO_LISTEN_FEEDS_ENV) or '')
    if not parsed:
        return
    if not shutil.which('ffmpeg'):
        print('[audio-listen] ffmpeg not found -- listener dormant',
              flush=True)
        return
    _audio_state['patterns'] = _audio_alert_patterns(
        _load_env().get(AUDIO_LISTEN_KEYWORDS_ENV))
    _audio_state['feeds'] = [
        {'label': label, 'url': url, 'dir': _audio_feed_dir(label),
         'proc': None, 'backoff': 5.0, 'next_start': 0.0,
         'ring': [], 'pending': []}
        for label, url in parsed]
    _audio_state['colab_enabled'] = _audio_colab_enabled()
    print('[audio-listen] %d feed(s) active: %s%s' % (
        len(parsed), ', '.join(label for label, _ in parsed),
        ' + colab overflow leg' if _audio_state['colab_enabled'] else ''),
        flush=True)
    inflight = {}
    try:
        while True:
            now = time.time()
            for feed in _audio_state['feeds']:
                _audio_feed_ensure(feed, now)
                _audio_sweep_segments(feed, now)
                for path in _audio_ready_segments(feed, now):
                    if len(inflight) >= _AUDIO_LISTEN_INFLIGHT_MAX:
                        break
                    if path in inflight \
                            or path in _audio_state['colab_pending']:
                        continue
                    inflight[path] = asyncio.ensure_future(
                        asyncio.to_thread(_audio_handle_segment,
                                          feed, path, now))
            for path, task in list(inflight.items()):
                if task.done():
                    try:
                        task.result()
                    except Exception as e:  # noqa: BLE001
                        print('[audio-listen] segment worker failed: %s' % e,
                              flush=True)
                    del inflight[path]
            _audio_colab_tick(now)
            if now - _audio_state['digest_at'] >= _AUDIO_LISTEN_DIGEST_S:
                _audio_state['digest_at'] = now
                try:
                    _audio_digest_write(now)
                except Exception as e:  # noqa: BLE001
                    print('[audio-listen] digest write failed: %s' % e,
                          flush=True)
            await asyncio.sleep(_AUDIO_LISTEN_POLL_S)
    finally:
        for feed in _audio_state['feeds']:
            proc = feed.get('proc')
            if proc is not None and proc.poll() is None:
                proc.terminate()
        for task in inflight.values():
            task.cancel()
