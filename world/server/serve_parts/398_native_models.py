# Native-model tiers (operator, 2026-11-8): JEV-routed jobs on ephemeral
# Colab runtimes for NON-LLM HuggingFace model classes. The YOLO pattern,
# generalized: a route table decides install + pipeline per model class.
#
# Selection criteria (operator directive: weigh model size, boot time,
# value):
#   * small enough to install + pull from HF on the runtime in under ~2min
#     (boot time is the dominant cost)
#   * classes the tank already has demand shapes for
#   * permissive licenses (MIT/Apache); noncommercial stays out
#
# Route table:
#   tts        | mesmerize/matcha-tts        ~60MB  | ~25s  | MIT
#   whispr-s   | openai/whisper-small        244MB  | ~40s  | MIT (CPU-runnable)
#   blip-cap   | Salesforce/blip-image-captioning-large ~470MB | ~35s | MIT (CPU-run)
# Route names are stable keys; the executor resolves repo id + job code.
import asyncio
import json
import os
import re
import tempfile
import time
import urllib.request
import zipfile

_NATIVE_ROUTES = {
    'tts': {
        'label': 'Matcha TTS',
        'description': ('Text-to-speech: short narration clips as wav, from '
                        'the Matcha checkpoint (MIT, ~60MB).'),
        'repos': ['mesmerize/matcha-tts'],
        'pip': 'matcha-tts',
        'kind': 'tts',
    },
    'whisper-s': {
        'label': 'Whisper Small ASR',
        'description': ('Speech-to-text for short clips (openai/whisper-small, '
                        'MIT, ~244MB, CPU-runnable).'),
        'repos': ['openai/whisper-small'],
        'pip': 'openai-whisper',
        'kind': 'asr',
    },
    'blip-cap': {
        'label': 'BLIP image captioning',
        'description': ('Short English caption for images '
                        '(Salesforce/blip-image-captioning-large, MIT, '
                        '~470MB, CPU-runnable).'),
        'repos': ['Salesforce/blip-image-captioning-large'],
        'pip': 'transformers',
        'kind': 'caption',
    },
}


def _native_route(route):
    return _NATIVE_ROUTES.get((route or '').strip())


def _native_routes():
    return _NATIVE_ROUTES


def _native_enabled():
    return str(_load_env().get('NATIVE_MODELS_ENABLED') or '1').lower() \
        not in ('0', 'false', 'no')


def _native_job_code(kind):
    """Python run on the Colab runtime: install, load, infer, print a
    3-line header (ok|err / kind / count), then JSON. Mirrors the YOLO
    fetch-wire shape (first line is the status)."""
    if kind == 'tts':
        return (
            "import json, subprocess, torch\n"
            "subprocess.run(['pip','install','-q','matcha-tts'], timeout=540)\n"
            "from matcha import text_to_speech as _tts\n"
            "import scipy.io.wavfile\n"
            "texts = json.loads(IN_PAYLOAD)\n"
            "outs = []\n"
            "for t in texts[:8]:\n"
            "    wav, sr = _tts(str(t)[:600])\n"
            "    path = f'/tmp/out{len(outs)}.wav'\n"
            "    scipy.io.wavfile.write(path, int(sr), wav)\n"
            "    outs.append(path)\n"
            "print('RESULT:' + json.dumps({'ok': True, 'files': outs}))\n"
            "print('__COLAB_DONE__')\n")
    if kind == 'asr':
        return (
            "import json, subprocess, torch\n"
            "subprocess.run(['pip','install','-q','openai-whisper'], timeout=540)\n"
            "import whisper\n"
            "model = whisper.load_model('small')\n"
            "import json as _j\n"
            "paths = _j.loads(IN_PAYLOAD)\n"
            "outs = []\n"
            "for p in paths[:8]:\n"
            "    res = model.transcribe(p)\n"
            "    outs.append({'file': p, 'text': str(res.get('text') or '')[:4000]})\n"
            "print('RESULT:' + json.dumps({'ok': True, 'texts': outs}))\n"
            "print('__COLAB_DONE__')\n")
    if kind == 'caption':
        return (
            "import json, subprocess\n"
            "subprocess.run(['pip','install','-q','transformers pillow'], timeout=540)\n"
            "from transformers import pipeline\n"
            "cap = pipeline('image-to-text', model='Salesforce/blip-image-captioning-large')\n"
            "paths = json.loads(IN_PAYLOAD)\n"
            "outs = []\n"
            "for p in paths[:8]:\n"
            "    res = cap(p)\n"
            "    outs.append({'file': p, 'text': str(res[0].get('generated_text') or '')[:300]})\n"
            "print('RESULT:' + json.dumps({'ok': True, 'captions': outs}))\n"
            "print('__COLAB_DONE__')\n")
    return None


def _native_model_run_sync(route, payload_list, purpose=''):
    """One native-model job on an ephemeral Colab runtime: provision, upload
    a small input payload, exec the job code, parse RESULT lines. Returns
    (ok, result) -- mirroring the YOLO lane's (ok, rows) contract. Blocks;
    takes _COLAB_COMPUTE_LOCK so it serializes with the YOLO and fetch
    lanes."""
    route_def = _native_route(route)
    if not route_def:
        return False, {'error': f'unknown native route: {route}'}
    if not _native_enabled():
        return False, {'error': 'native models are disabled '
                                '(NATIVE_MODELS_ENABLED=0)'}
    items = [str(x) for x in (payload_list or []) if str(x).strip()]
    if not items:
        return False, {'error': 'no payload for the native job'}
    if not COLAB_CLI_AVAILABLE:
        return False, {'error': 'the colab CLI is not installed on this machine'}
    if not COLAB_ENABLED:
        return False, {'error': 'Colab compute is disabled (COLAB_ENABLED=0)'}
    def inner():
        with _COLAB_COMPUTE_LOCK:
            start = time.time()
            account = _colab_pick_account()
            kind = 'cpu' if route_def['kind'] in ('asr', 'caption') else 'gpu'
            session = _colab_session_name(kind, account)
            ok, msg = _colab_compute_provision(session, kind=kind, account=account)
            if not ok:
                return False, {'error': f'could not provision the Colab '
                                         f'{kind} session: {msg}',
                               'account': account}
            # payload rides as one small file
            tmp = tempfile.NamedTemporaryFile(prefix='native-in-', suffix='.json',
                                              delete=False)
            tmp.write(json.dumps(items).encode())
            tmp.close()
            remote_in = 'native_in.json'
            try:
                rc, out = _colab_cli('upload', tmp.name, remote_in,
                                     timeout=120, account=account)
                if rc != 0:
                    return False, {'error': f'payload upload failed: {out[-300:]}',
                                   'account': account}
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
            code = _native_job_code(route_def['kind'])
            if not code:
                return False, {'error': f'no job code for {route}'}
            rc, out = _colab_cli('exec', '-s', session, '--timeout', '660',
                                 '--env', f'IN_PAYLOAD_FILE={remote_in}',
                                 timeout=720, account=account, input=code)
            elapsed = max(1, int(time.time() - start))
            globals()['_COLAB_COMPUTE_LAST_USED'] = time.time()
            if rc != 0 or '__COLAB_DONE__' not in (out or ''):
                return False, {'error': f'native exec failed (exit {rc}): '
                                        f'{(out or "")[-600:]}',
                               'account': account}
            units = max(COLAB_MIN_UNITS_PER_RUN, round(elapsed / 60.0 * 1.0, 3))
            _accrue_colab_units(units)
            rows = []
            for line in (out or '').splitlines():
                line = line.strip()
                if not line.startswith('RESULT:'):
                    continue
                try:
                    parsed = json.loads(line[7:])
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    rows.append(parsed)
            if not rows:
                return False, {'error': 'native exec produced no RESULT lines',
                               'account': account}
            return True, rows[0]
    try:
        return inner()
    except Exception as e:
        return False, {'error': str(e)[:200]}


async def _native_model_run(route, payload_list, purpose=''):
    return await asyncio.to_thread(
        _native_model_run_sync, route, payload_list, purpose)


@app.get('/api/native-models')
async def native_models_list(request: Request):
    """The native-model route table. Open to authenticated clients."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    return JSONResponse({'routes': _native_routes(),
                         'enabled': _native_enabled()})


@app.post('/api/native-models/{route}/run')
async def native_models_run(route: str, request: Request):
    """Runs one native-model job on an ephemeral Colab runtime. Player-gated
    (spends Colab units). Body: {'payload': [ ... up to 8 items ... ]}."""
    authed, _actor = _operator_gate(request)
    if not authed:
        return JSONResponse({'error': 'Unauthorized'}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    payload = (body.get('payload') or []) if isinstance(body, dict) else []
    log_action('player', 'native_model_run',
               {'route': route, 'items': len(payload)}, authorized=True)
    ok, result = await _native_model_run(route, payload)
    status = 200 if ok else 502
    return JSONResponse({'ok': ok, **(result if isinstance(result, dict) else
                                       {'error': str(result)[:300]})},
                        status_code=status)
