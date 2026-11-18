"""Blender render lane: agents run headless Blender (Cycles CPU) to produce
images, the same thin-adapter pattern as the X lane (185): the tank runs
the vetted upstream binary (`blender` from the Ubuntu archive, upstream
blender/blender on the trusted-codebase registry) and contributes only the
gate and the guardrails.

Why the wrapper exists (beyond provenance): an agent's scene script is
arbitrary Python executing at full user rights on the VM. The tank cannot
sandbox that meaningfully; it CAN bound the blast radius -- render once,
after the agent's code, with engine/device/denoiser/samples/resolution and
the output path forced here, output confined to library/renders/<agent>/,
a hard subprocess timeout, and every run logged in action_log. OIDN is
absent from the Ubuntu build, so denoising is off by default (the first
live render died on exactly that: 'Build without OpenImageDenoiser').

If the tool is not trusted or not installed, the lane refuses with the
sanctioned fix in the message, like the X lane does."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from fastapi import Request
from fastapi.responses import JSONResponse

_BLENDER_TRUSTED_REPO = 'blender/blender'
_BLENDER_TIMEOUT_S = 300.0
_BLENDER_TIMEOUT_MAX_S = 900.0
_BLENDER_SCRIPT_MAX_CHARS = 8000
_BLENDER_MAX_SAMPLES = 256
_BLENDER_MAX_RES = 1920
# Output confinement: everything lands under this subdir of the Library,
# which is exactly the scope agents already hold a file grant on, so a
# finished render is readable back through local_file.
_BLENDER_RENDERS_SUBDIR = 'renders'


def _blender_binary():
    """The trusted tool's binary, located through PATH like any other
    installed tool (the X-lane pattern); provenance is pinned by the
    trusted-repos registry, not by where the binary sits."""
    return shutil.which('blender')


def _blender_gate():
    """(binary, error_message) -- shared preconditions: tool trusted and
    installed. Message names the sanctioned fix, like the X lane."""
    if not source_is_trusted(_BLENDER_TRUSTED_REPO):
        return None, ('blender is not on the trusted-codebase registry '
                      f'({_BLENDER_TRUSTED_REPO}); an operator adds it via '
                      'POST /api/trusted-codebase')
    binary = _blender_binary()
    if not binary:
        return None, ('blender CLI not installed on the server '
                      '(sudo apt-get install blender)')
    return binary, None


def _safe_render_name(name):
    """Strip an output filename down to a safe basename: no paths, no
    traversal, alnum + dash/dot/underscore only, capped length."""
    base = os.path.basename(str(name or 'render')).strip()
    base = re.sub(r'[^A-Za-z0-9._-]+', '-', base).strip('-.')
    return (base or 'render')[:64]


def _build_wrapper(user_script, out_path, samples, res_x, res_y):
    """The executed wrapper: run the agent's scene code first, THEN force
    the guardrails and render exactly once. Forcing after user code means a
    scene that sets wild values still lands within caps, and the render
    target is ours no matter what the script assigned."""
    return (
        "import bpy, sys\n"
        "scene = bpy.context.scene\n"
        "# --- agent scene code ---\n"
        + user_script + "\n"
        "# --- guardrails (forced after agent code) ---\n"
        "scene.render.engine = 'CYCLES'\n"
        "scene.cycles.device = 'CPU'\n"
        "scene.cycles.use_denoising = False\n"  # Ubuntu build has no OIDN
        f"scene.cycles.samples = max(1, min(int(scene.cycles.samples or 1), {samples}))\n"
        f"scene.render.resolution_x = min(int(scene.render.resolution_x or {res_x}), {res_x})\n"
        f"scene.render.resolution_y = min(int(scene.render.resolution_y or {res_y}), {res_y})\n"
        "scene.render.image_settings.file_format = 'PNG'\n"
        f"scene.render.filepath = {out_path!r}\n"
        "bpy.ops.render.render(write_still=True)\n"
        "print('BLENDER_TANK_RENDER_DONE')\n"
    )


def _blender_render(agent_id, args):
    """Shared worker for the ask-tool and the operator endpoint. Returns
    (ok, payload_dict). Reads the script from args, renders once under the
    guardrails, files the PNG under library/renders/<agent>/."""
    binary, err = _blender_gate()
    if err:
        return False, {'error': err}
    script = str(args.get('script') or '').strip()
    if not script:
        return False, {'error': 'script is required (bpy scene code; the '
                                'wrapper renders for you -- do not call '
                                'bpy.ops.render.render yourself)'}
    if len(script) > _BLENDER_SCRIPT_MAX_CHARS:
        return False, {'error': (f'script too long ({len(script)} chars; '
                                 f'cap {_BLENDER_SCRIPT_MAX_CHARS})')}
    try:
        timeout = float(args.get('timeoutS') or _BLENDER_TIMEOUT_S)
        timeout = max(10.0, min(timeout, _BLENDER_TIMEOUT_MAX_S))
        samples = max(1, min(int(args.get('samples') or 64),
                             _BLENDER_MAX_SAMPLES))
        res_x = max(64, min(int(args.get('resX') or 640), _BLENDER_MAX_RES))
        res_y = max(64, min(int(args.get('resY') or 640), _BLENDER_MAX_RES))
    except (TypeError, ValueError):
        return False, {'error': 'malformed numeric arg'}

    out_dir = os.path.join(LIBRARY_DIR, _BLENDER_RENDERS_SUBDIR, agent_id)
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    name = _safe_render_name(args.get('output'))
    out_path = os.path.join(out_dir, f'{stamp}-{name}.png')

    wrapper = _build_wrapper(script, out_path, samples, res_x, res_y)
    tmpdir = tempfile.mkdtemp(prefix='blender-tank-')
    wrapper_path = os.path.join(tmpdir, 'scene_wrapper.py')
    try:
        with open(wrapper_path, 'w', encoding='utf-8') as fh:
            fh.write(wrapper)
        started = time.time()
        try:
            proc = subprocess.run(
                [binary, '-b', '--python', wrapper_path],
                capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            log_action(agent_id, 'blender_render', {
                'agent': agent_id, 'output': out_path,
                'outcome': 'timeout', 'timeoutS': timeout})
            return False, {'error': (f'render exceeded {timeout:.0f}s and '
                                     'was killed (raise timeoutS up to '
                                     f'{_BLENDER_TIMEOUT_MAX_S:.0f}, or '
                                     'drop samples/resolution)')}
        done_marker = 'BLENDER_TANK_RENDER_DONE' in (proc.stdout or '')
        elapsed = round(time.time() - started, 1)
        if proc.returncode != 0 or not done_marker \
                or not os.path.exists(out_path):
            tail = '\n'.join((proc.stderr or proc.stdout or '').
                             strip().splitlines()[-8:])
            log_action(agent_id, 'blender_render', {
                'agent': agent_id, 'output': out_path,
                'outcome': 'failed', 'rc': proc.returncode})
            return False, {'error': 'render failed', 'rc': proc.returncode,
                           'tail': tail[:800]}
        size = os.path.getsize(out_path)
        rel = os.path.relpath(out_path, LIBRARY_DIR)
        log_action(agent_id, 'blender_render', {
            'agent': agent_id, 'output': out_path, 'bytes': size,
            'outcome': 'ok', 'elapsedS': elapsed})
        return True, {
            'ok': True, 'file': rel, 'bytes': size,
            'elapsedS': elapsed, 'samples': samples,
            'res': [res_x, res_y],
            'note': ('read it back with local_file scope '
                     '/tanklib, path "' + rel + '"')}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _blender_render_tool(agent_id, args):
    ok, out = _blender_render(agent_id, args)
    if not ok:
        return 'blender_render failed: ' + json.dumps(out)
    return (f"Rendered library/{out['file']} ({out['bytes']} bytes, "
            f"{out['elapsedS']}s, samples {out['samples']}, "
            f"{out['res'][0]}x{out['res'][1]}). {out['note']}")


_BLENDER_TOOL = {
    'type': 'function',
    'function': {
        'name': 'blender_render',
        'description': 'Render an image with headless Blender (Cycles, '
                       'CPU). Pass `script`: your bpy scene code (add '
                       'objects, lights, camera -- do NOT call '
                       'bpy.ops.render.render yourself; the wrapper renders '
                       'once after your code with guardrails: samples and '
                       'resolution are capped, denoising is off, output is '
                       'a PNG under library/renders/<your-id>/ which you '
                       'can read back via local_file). Runs are logged and '
                       'time-capped.',
        'parameters': {
            'type': 'object',
            'properties': {
                'script': {'type': 'string',
                           'description': 'bpy scene code, no render call.'},
                'output': {'type': 'string',
                           'description': 'Output file base name '
                                          '(default "render").'},
                'samples': {'type': 'integer',
                            'description': 'Cycles samples (default 64, '
                                           'cap 256).'},
                'resX': {'type': 'integer',
                         'description': 'Width (default 640, cap 1920).'},
                'resY': {'type': 'integer',
                         'description': 'Height (default 640, cap 1920).'},
                'timeoutS': {'type': 'integer',
                             'description': 'Hard kill after N seconds '
                                            '(default 300, cap 900).'},
            },
            'required': ['script'],
        },
    },
}


@app.post('/api/blender/render')
async def api_blender_render(request: Request):
    if not _require_player_or_admin(request):
        return JSONResponse({'error': 'Unauthorized -- operator only'},
                            status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'malformed body'}, status_code=400)
    ok, out = await asyncio.to_thread(_blender_render, 'operator', body)
    return JSONResponse(out, status_code=200 if ok else 400)
