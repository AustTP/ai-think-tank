# --- Local model tiers (operator, 2026-11-26) -------------------------------
# Repos and local ML runtimes as additional tiers routed to INSTEAD of an LLM
# when the job fits. Registry: world/local_models.json (operator-edited facts,
# like model_bands.json). Every lane is health-checked before use (enabled
# flag + binary on PATH), any runtime failure trips a cooldown so a broken
# engine cannot thrash on every call, and the LLM band underneath stays as
# the fallback for anything the local engine cannot answer. Local calls log
# $0 spend to the ledger -- no entry, so the tier-value math stays honest.
# First entry: tesseract OCR (apt package, no model downloads, no network).
_LOCAL_MODELS_PATH = os.path.join(ROOT, 'local_models.json')
_LOCAL_MODEL_HEALTH_TTL_S = 60.0
_LOCAL_MODEL_FAILURE_COOLDOWN_S = 120.0
_LOCAL_MODEL_STATE: dict = {'cache': None, 'cacheAt': 0.0, 'ready': {},
                            'cooldownUntil': {}}


def _load_local_models():
    """Parse world/local_models.json, stat-guarded (reread after edits at
    most once per second). Corrupt or absent file -> {} (every local lane
    off; the LLM bands are untouched)."""
    now = time.time()
    st = _LOCAL_MODEL_STATE
    try:
        mtime = os.path.getmtime(_LOCAL_MODELS_PATH)
    except OSError:
        st['cache'], st['cacheAt'] = {}, now
        return {}
    if st['cache'] is not None and now - st['cacheAt'] < 1.0 and st.get('cacheMtime') == mtime:
        return st['cache']
    try:
        with open(_LOCAL_MODELS_PATH, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f'[local-models] registry unreadable: {exc}', flush=True)
        data = {}
    cfg = data if isinstance(data, dict) else {}
    cfg.pop('_notes', None)
    st['cache'], st['cacheAt'], st['cacheMtime'] = cfg, now, mtime
    return cfg


def _local_model_cfg(kind):
    cfg = _load_local_models().get(kind)
    return cfg if isinstance(cfg, dict) else None


def _local_model_ready(kind):
    """True when the local lane may run: enabled in the registry, binary on
    PATH, and not inside a failure cooldown. Cached briefly so hot call
    paths do not stat/shutil.which every time."""
    cfg = _local_model_cfg(kind)
    if not cfg or cfg.get('enabled') is False:
        return False
    st = _LOCAL_MODEL_STATE
    now = time.time()
    if st['cooldownUntil'].get(kind, 0) > now:
        return False
    cached = st['ready'].get(kind)
    if cached is not None and now - cached['at'] < _LOCAL_MODEL_HEALTH_TTL_S:
        return cached['ok']
    binary = cfg.get('binary') or cfg.get('engine')
    ok = bool(binary) and shutil.which(binary) is not None
    st['ready'][kind] = {'at': now, 'ok': ok}
    if not ok:
        print(f'[local-models] {kind}: binary {binary!r} not on PATH; lane off', flush=True)
    return ok


def _local_model_trip_cooldown(kind):
    _LOCAL_MODEL_STATE['cooldownUntil'][kind] = (
        time.time() + _LOCAL_MODEL_FAILURE_COOLDOWN_S)
    _LOCAL_MODEL_STATE['ready'].pop(kind, None)


def _local_ocr_text(image_bytes):
    """Run the registry's OCR engine over one PNG (bytes). Returns the text
    or None -- None ALWAYS means "cannot answer" (lane off, engine failed,
    empty output) and the caller falls back to the LLM band. Never raises."""
    cfg = _local_model_cfg('ocr')
    if not cfg or not image_bytes:
        return None
    binary = cfg.get('binary') or cfg.get('engine')
    timeout_s = float(cfg.get('timeoutS') or 15)
    max_chars = int(cfg.get('maxTextChars') or 20000)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix='.png')
        with os.fdopen(fd, 'wb') as fh:
            fh.write(image_bytes)
        run = subprocess.run([binary, tmp, 'stdout'], capture_output=True,
                             text=True, timeout=timeout_s)
        if run.returncode != 0:
            raise RuntimeError(f'exit {run.returncode}: {(run.stderr or "")[:200]}')
        text = (run.stdout or '').strip()[:max_chars]
        if not text:
            return None
        log_action(None, 'local_model_used',
                   {'kind': 'ocr', 'engine': binary, 'chars': len(text)},
                   authorized=True)
        return text
    except Exception as exc:
        _local_model_trip_cooldown('ocr')
        print(f'[local-models] ocr failed ({exc}); lane cooling down', flush=True)
        return None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _norm_ocr_text(text):
    return ' '.join(str(text or '').lower().split())


def _local_ocr_visual_answer(image_b64, expect_visual):
    """The local OCR lane for a YES/NO visual check. Answers ONLY with
    positive evidence: the expected text, normalized, found verbatim in the
    OCR text of the screenshot. Anything else -- lane off, OCR empty, phrase
    absent (stylized font, rendering difference, OCR miss) -- returns None
    and the caller keeps the LLM vision band as the arbiter. A YES that
    local OCR cannot support is never invented; absence of evidence is
    deferred, not converted into a NO."""
    if not expect_visual or not image_b64:
        return None
    if not _local_model_ready('ocr'):
        return None
    try:
        image_bytes = base64.b64decode(image_b64, validate=True)
    except (ValueError, TypeError):
        return None
    if not image_bytes:
        return None
    text = _local_ocr_text(image_bytes)
    if not text:
        return None
    expect = ' '.join(str(expect_visual).lower().split())
    if expect not in _norm_ocr_text(text):
        return None
    snippet = ' '.join(text.split())[:200]
    return {'ok': True,
            'reply': f'YES\nLocal OCR ({_local_model_cfg("ocr").get("engine", "tesseract")}) '
                     f'found "{expect_visual}" in the page text: "{snippet}"'}
