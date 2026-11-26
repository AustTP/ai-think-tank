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


def _local_ocr_text(image_bytes, tag='base'):
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
        entry = {'kind': 'ocr', 'engine': binary, 'chars': len(text)}
        if tag != 'base':
            entry['pass'] = tag
        log_action(None, 'local_model_used', entry, authorized=True)
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


_OCR_HEADER_CROP_FRAC = 0.2   # mastheads/toolbars live in the top strip
_OCR_BINARIZE_AT = 128


def _local_ocr_preprocess(image_bytes):
    """Second-pass preprocessing for the OCR lane: crop the top strip
    (the masthead/header region), grayscale, then normalize contrast --
    the DOMINANT value (the background) is stretched to black and the
    brightest value to white, inverted when the result still reads dark,
    hard-thresholded to pure black/white and upscaled 2x (small header
    fonts read badly at 1x). This is the chain real tesseract was probed
    with: light-on-orange mastheads (the Hacker News class of miss) come
    out as clean dark-on-light text, while a plain binarize at 128 erases
    them (orange bg ~= gray 136 straddles the threshold). Returns PNG
    bytes, or None when PIL is unavailable or the bytes are not a
    decodable image -- never raises."""
    if not image_bytes:
        return None
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        img = img.convert('L').crop((0, 0, w, max(1, int(h * _OCR_HEADER_CROP_FRAC))))
        hist = img.histogram()
        bg = max(range(256), key=lambda i: hist[i])
        img = img.point(lambda p, m=bg: max(0, p - m) * 255 // max(1, 255 - m))
        hist = img.histogram()
        mean = sum(i * n for i, n in enumerate(hist)) / (sum(hist) or 1)
        if mean < _OCR_BINARIZE_AT:
            img = img.point(lambda p: 255 - p)
        img = img.point(lambda p: 0 if p < _OCR_BINARIZE_AT else 255)
        img = img.resize((img.width * 2, img.height * 2))
        out = io.BytesIO()
        img.save(out, format='PNG')
        return out.getvalue() or None
    except Exception:
        return None


def _ocr_phrase_found(expect, text):
    """Positive-evidence phrase match. First the plain normalized compare;
    then a whitespace-stripped compare, because tesseract glues adjacent
    words ('TOTAL$12.50' for 'total $12.50') and the glued form never
    appears as a normalized substring."""
    norm = _norm_ocr_text(text)
    if not norm:
        return False
    if expect in norm:
        return True
    glued = ''.join(norm.split())
    return bool(glued) and ''.join(expect.split()) in glued


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
    expect = ' '.join(str(expect_visual).lower().split())
    matched = None
    text = _local_ocr_text(image_bytes)
    if text and _ocr_phrase_found(expect, text):
        matched = text
    if not matched:
        # Second pass, only on a miss and only preprocessing ($0 local
        # compute): the plain run reads the whole page, so a colored or
        # inverted header it garbled is the remaining fixable miss.
        prepped = _local_ocr_preprocess(image_bytes)
        if prepped:
            text2 = _local_ocr_text(prepped, tag='header')
            if text2 and _ocr_phrase_found(expect, text2):
                matched = text2
    if not matched:
        return None
    snippet = ' '.join(matched.split())[:200]
    return {'ok': True,
            'reply': f'YES\nLocal OCR ({_local_model_cfg("ocr").get("engine", "tesseract")}) '
                     f'found "{expect_visual}" in the page text: "{snippet}"'}
