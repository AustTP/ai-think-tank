# YOLO object detection for video frames, run on Colab (owner direction,
# 2026-11-21): the vision tier describes frames with an OpenRouter LLM; this
# module adds CHEAP, DETERMINISTIC detections to the same frames by running
# an Ultralytics YOLO model as an ephemeral Colab job through the same CLI
# gateway run_on_colab uses (170). Why Colab: torch inference on this VM is
# CPU-only and too slow for batch frames; a Colab runtime answers in seconds.
# Per-job account affinity holds here exactly as everywhere else: ONE
# account is picked for the whole detection batch, its session is used for
# every upload/exec, and the extra-runtime teardown never touches another
# account. The exec'd code is tank-authored (never agent-authored), so the
# deny-list sweep agent code goes through is not applied here; the frames
# themselves come from the agent's YouTube URL, which the endpoint already
# validated, and the budget gate still applies (detect runs accrue compute
# units like any other Colab job).

_YOLO_DETECT_ENABLED = str(
    _load_env().get('YOLO_DETECT_ENABLED', '1') or '').lower() not in ('0', 'false', 'no')
_YOLO_DETECT_MODEL = (_load_env().get('YOLO_DETECT_MODEL') or 'yolov8n.pt').strip() or 'yolov8n.pt'
try:
    _YOLO_DETECT_MAX_FRAMES = max(1, min(int(_load_env().get('YOLO_DETECT_MAX_FRAMES') or 12), 60))
except Exception:
    _YOLO_DETECT_MAX_FRAMES = 12
try:
    _YOLO_DETECT_CONF = max(0.01, min(float(_load_env().get('YOLO_DETECT_CONF') or 0.25), 0.95))
except Exception:
    _YOLO_DETECT_CONF = 0.25
_YOLO_DETECT_KIND = ('cpu' if str(_load_env().get('YOLO_DETECT_RUNTIME') or 'gpu').strip().lower()
                     == 'cpu' else 'gpu')
_YOLO_BATCH_REMOTE = '/content/yolo-batch.zip'

# The upload batching: one zip upload per detection call (N single-file
# uploads would be N round-trips), fixed member names so the order mapping
# survives (the caller maps response rows back to timestamps by index, not
# by filename guessing).
_YOLO_EXEC_TEMPLATE = '''
import os, json, re, zipfile, glob
try:
    from ultralytics import YOLO
except ImportError:
    import subprocess
    subprocess.run(['pip', 'install', '-q', 'ultralytics'], timeout=540)
    from ultralytics import YOLO
zf = zipfile.ZipFile('/content/yolo-batch.zip')
zf.extractall('/content/yf/')
zf.close()
model = YOLO(os.environ.get('YOLO_MODEL', 'yolov8n.pt'))
conf = float(os.environ.get('YOLO_CONF', '0.25'))
rows = []
for path in sorted(glob.glob('/content/yf/*')):
    match = re.search(r'(\\d+)', os.path.basename(path))
    if not match:
        continue
    result = model.predict(path, conf=conf, verbose=False)[0]
    names = result.names
    detections = []
    for box in result.boxes:
        cls_id = int(box.cls[0])
        detections.append({
            'cls': cls_id,
            'name': names[cls_id],
            'conf': round(float(box.conf[0]), 3),
            'xyxy': [round(float(x), 1) for x in box.xyxy[0].tolist()],
        })
    rows.append({'i': int(match.group(1)),
                 'f': os.path.basename(path), 'd': detections})
rows.sort(key=lambda row: row['i'])
for row in rows:
    print(json.dumps(row, separators=(',', ':')))
print('__COLAB_DONE__')
'''


def _yolo_zip_bytes(index):
    return f'frame-{index:04d}.png'


def _yolo_build_batch_zip(images):
    """Pack frame bytes into one upload zip. PNG bytes go in as-is; frames
    that decode as an image get re-encoded as JPEG first (quality 85) to cut
    the upload to a fraction -- best-effort, a frame that will not decode
    rides along as raw bytes rather than failing the batch."""
    import io
    import tempfile
    import zipfile as zipfile_mod
    tmp = tempfile.NamedTemporaryFile(prefix='yolo-batch-', suffix='.zip',
                                      delete=False)
    tmp.close()
    try:
        with zipfile_mod.ZipFile(tmp.name, 'w', zipfile_mod.ZIP_DEFLATED) as zf:
            for index, data in enumerate(images):
                data = data or b''
                encoded = None
                if data[:8] == b'\x89PNG\r\n\x1a\n':
                    try:
                        from PIL import Image
                        with Image.open(io.BytesIO(data)) as img:
                            buf = io.BytesIO()
                            img.convert('RGB').save(buf, 'JPEG', quality=85)
                            encoded = buf.getvalue()
                    except Exception:
                        encoded = None
                if encoded is not None:
                    zf.writestr(_yolo_zip_bytes(index).replace('.png', '.jpg'),
                                encoded)
                elif data:
                    zf.writestr(_yolo_zip_bytes(index), data)
        return tmp.name
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def _yolo_detect_images_sync(images, purpose='YOLO video-frame detection',
                             model_slug=None, conf=None):
    """Detect objects in a batch of image bytes on one Colab runtime.
    Returns (ok, rows) where rows align 1:1 with the input order, each row
    {'detections': [...], 'model': slug, 'elapsedS': n}; or (False, error)
    on any failure. Blocks -- call from a worker thread, and note it takes
    _COLAB_COMPUTE_LOCK so it serializes with run_on_colab jobs (two execs
    on one Colab session race)."""
    global _COLAB_COMPUTE_LAST_USED
    if not _YOLO_DETECT_ENABLED:
        return False, {'error': 'YOLO detection is disabled (YOLO_DETECT_ENABLED=0)'}
    if not COLAB_CLI_AVAILABLE:
        return False, {'error': 'the colab CLI is not installed on this machine'}
    if not COLAB_ENABLED:
        return False, {'error': 'Colab compute is disabled by the operator (COLAB_ENABLED=0)'}
    if not images:
        return False, {'error': 'no frames to detect on'}
    images = list(images)[:_YOLO_DETECT_MAX_FRAMES]
    model_slug = (model_slug or _YOLO_DETECT_MODEL).strip() or _YOLO_DETECT_MODEL
    try:
        conf = max(0.01, min(float(conf if conf is not None else _YOLO_DETECT_CONF), 0.95))
    except (TypeError, ValueError):
        conf = _YOLO_DETECT_CONF
    if _colab_budget_exceeded():
        return False, {'error': 'the think tank has used its own monthly Colab unit cap; '
                                'detection is paused until the cap is raised'}
    kind = _YOLO_DETECT_KIND
    units_per_min = COLAB_UNITS_PER_MIN_GPU if kind == 'gpu' else COLAB_UNITS_PER_MIN_CPU
    zip_path = None
    with _COLAB_COMPUTE_LOCK:
        start = time.time()
        account = _colab_pick_account()
        session = _colab_session_name(kind, account)
        ok, msg = _colab_compute_provision(session, kind=kind, account=account)
        if not ok:
            return False, {'error': f'could not provision a Colab {kind} session: {msg}',
                           'account': account}
        try:
            zip_path = _yolo_build_batch_zip(images)
        except Exception as e:
            return False, {'error': f'could not pack the frame batch: {e}'}
        try:
            rc, out = _colab_cli('upload', zip_path, _YOLO_BATCH_REMOTE,
                                 timeout=300, account=account)
            if rc != 0:
                return False, {'error': f'frame upload failed: {out[-400:]}',
                               'account': account}
            rc, out = _colab_cli(
                'exec', '-s', session, '--timeout', '840',
                '--env', f'YOLO_MODEL={model_slug}',
                '--env', f'YOLO_CONF={conf}',
                timeout=900, account=account,
                input=_YOLO_EXEC_TEMPLATE)
        finally:
            try:
                os.unlink(zip_path)
            except OSError:
                pass
        elapsed = max(1, int(time.time() - start))
        _COLAB_COMPUTE_LAST_USED = time.time()
        if rc != 0 or '__COLAB_DONE__' not in (out or ''):
            return False, {'error': f'YOLO exec failed (exit {rc}): {(out or "")[-600:]}',
                           'account': account}
        units = max(COLAB_MIN_UNITS_PER_RUN, round(elapsed / 60.0 * units_per_min, 3))
        _accrue_colab_units(units)
        # Rows are placed by the frame index the runtime reports back (it
        # parses the numeric member name), so JPEG-re-encoded members that
        # sort differently from raw PNG ones can never scramble the order.
        rows = [None] * len(images)
        for line in (out or '').splitlines():
            line = line.strip()
            if not line or '__COLAB_DONE__' in line:
                continue
            try:
                parsed = json.loads(line)
            except Exception:
                continue
            if not isinstance(parsed, dict) or 'i' not in parsed:
                continue
            index = parsed['i']
            if isinstance(index, int) and 0 <= index < len(images):
                rows[index] = {'detections': parsed.get('d') or [],
                               'file': parsed.get('f')}
        for index, row in enumerate(rows):
            # Missing rows stay explicit: an empty detection set aligned by
            # index, never a silent re-map.
            if row is None:
                rows[index] = {'detections': [], 'file': None}
        log_action('system', 'yolo_detect_run',
                   {'frames': len(images), 'model': model_slug, 'conf': conf,
                    'kind': kind, 'account': account, 'session': session,
                    'elapsedS': elapsed, 'units': units,
                    'withDetections': sum(1 for r in rows if r['detections'])},
                   authorized=True)
        return True, {'rows': rows, 'account': account, 'session': session,
                      'model': model_slug, 'conf': conf, 'elapsedS': elapsed,
                      'units': units}


# --- Colab fetch lane (rare-day fallback) -----------------------------------
# Operator question (2026-11-22): when the VM's datacenter IP gets blocked
# (403/429 from e.g. a Cloudflare-fronted host), a fetch from a Colab
# runtime's Google Cloud egress is occasionally granted where both the VM's
# IP and the Mullvad exits are refused. The existing browser fallback
# (_do_fetch_with_browser_fallback) only changes the RENDERER on 404/410 --
# it never changes the IP. This lane changes the IP. Deliberately narrow:
# only wired where the browser fallback was NOT wired (no VPN branches), one
# attempt per fetch, best-effort (failure returns the original error), and
# the URL was already Jev/allowlist-cleared before the first fetch -- a
# Colab lane grants no second unvetted path, it is another way to read an
# already-cleared page. Budget + single-account affinity identical to every
# other Colab job. Env: COLAB_FETCH_ENABLED=0 to shut it off entirely.
_COLAB_FETCH_ENABLED = str(
    _load_env().get('COLAB_FETCH_ENABLED', '1') or '').lower() not in ('0', 'false', 'no')
_COLAB_FETCH_LAST_AT = 0.0
_COLAB_FETCH_MIN_INTERVAL_S = 30.0  # rare-day lane: never hot-looping

def _colab_fetch_sync(url, purpose=''):
    """Fetch an already-Jev-cleared URL through a Colab runtime's network.
    Returns (ok, data): ok=True -> data = {'body', 'contentType', 'finalUrl',
    'account', 'elapsedS', 'units'}; ok=False -> {'error'}. Best-effort by
    design: callers swallow failure and keep their original error. Blocks;
    call from a worker thread. Takes _COLAB_COMPUTE_LOCK so it serializes
    with run_on_colab jobs (one exec at a time per session)."""
    global _COLAB_FETCH_LAST_AT, _COLAB_COMPUTE_LAST_USED
    if not _COLAB_FETCH_ENABLED:
        return False, {'error': 'the Colab fetch lane is disabled '
                                '(COLAB_FETCH_ENABLED=0)'}
    if not COLAB_CLI_AVAILABLE:
        return False, {'error': 'the colab CLI is not installed'}
    if not COLAB_ENABLED:
        return False, {'error': 'Colab compute is disabled (COLAB_ENABLED=0)'}
    now = time.time()
    if now - _COLAB_FETCH_LAST_AT < _COLAB_FETCH_MIN_INTERVAL_S:
        return False, {'error': 'the Colab fetch lane is rate-limited '
                                f'(one attempt per '
                                f'{_COLAB_FETCH_MIN_INTERVAL_S:.0f}s)'}
    if _colab_budget_exceeded():
        return False, {'error': 'the think tank has used its monthly Colab '
                                'unit cap; the fetch lane is paused'}
    with _COLAB_COMPUTE_LOCK:
        start = time.time()
        account = _colab_pick_account()
        session = _colab_session_name('cpu', account)
        ok, msg = _colab_compute_provision(session, kind='cpu', account=account)
        if not ok:
            return False, {'error': f'could not provision a Colab cpu '
                                    f'session: {msg}', 'account': account}
        exec_code = (
            'import os, urllib.request, sys\n'
            'req = urllib.request.Request(os.environ[\'COLAB_FETCH_URL\'], '
            'headers={\'User-Agent\': \'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            ' AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36\'})\n'
            'try:\n'
            '    with urllib.request.urlopen(req, timeout=60) as resp:\n'
            '        raw = resp.read(3_000_000)\n'
            '        print(resp.status)\n'
            '        print(resp.headers.get(\'Content-Type\', \'\'))\n'
            '        print(resp.geturl())\n'
            '        sys.stdout.flush()\n'
            '        sys.stdout.buffer.write(raw)\n'
            'except Exception as e:\n'
            '    print(f\'FETCHERR {e}\')\n'
            'print(\'__COLAB_DONE__\')\n'
        )
        rc, out = _colab_cli(
            'exec', '-s', session, '--timeout', '90',
            '--env', f'COLAB_FETCH_URL={url}',
            timeout=150, account=account,
            input=exec_code)
        elapsed = max(1, int(time.time() - start))
        _COLAB_COMPUTE_LAST_USED = time.time()
        _COLAB_FETCH_LAST_AT = time.time()
        if rc != 0 or '__COLAB_DONE__' not in (out or ''):
            return False, {'error': f'Colab fetch failed (exit {rc}): '
                                    f'{(out or "")[-400:]}',
                           'account': account}
        units = max(COLAB_MIN_UNITS_PER_RUN,
                    round(elapsed / 60.0 * COLAB_UNITS_PER_MIN_CPU, 3))
        _accrue_colab_units(units)
        # The CLI streams the exec's stdout; the first line is the HTTP
        # status, the second the content type, the third the final URL
        # (three lines so a content type carrying "; charset=utf-8" can
        # never bleed into the URL -- a real 2026-11-22 test catch), and
        # everything after the third newline is the raw body.
        head_and_body = (out or '').split('__COLAB_DONE__')[0].rstrip('\n')
        first_line = head_and_body.split('\n', 1)[0]
        if first_line.startswith('FETCHERR'):
            return False, {'error': f'remote fetch refused: {first_line}',
                           'account': account}
        parts = head_and_body.split('\n', 3)
        if len(parts) < 3:
            return False, {'error': 'unparseable Colab fetch header: '
                                    f'{head_and_body[:200]}',
                           'account': account}
        status, content_type, final_url = parts[0], parts[1], parts[2]
        raw_body = parts[3] if len(parts) > 3 else ''
        log_action('system', 'colab_fetch_run',
                   {'url': url[:300], 'purpose': (purpose or '')[:120],
                    'status': status, 'bytes': len(raw_body),
                    'account': account, 'session': session,
                    'elapsedS': elapsed, 'units': units},
                   authorized=True)
        return True, {'body': raw_body, 'contentType': content_type,
                      'finalUrl': final_url, 'account': account,
                      'elapsedS': elapsed, 'units': units}
