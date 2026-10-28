

def _find_dangling_selector_refs(base, key, agent_id, sandbox_id):
    """Port of tasks.js _findDanglingSelectorRefs: JS queries a .class/#id that
    neither any markup creates nor any JS creates dynamically. Static and
    heuristic (only simple .class/#id/getElementById forms), advisory only --
    there is no safe auto-fix, just a surface-the-finding result."""
    read_command = 'for f in *.html *.js; do [ -f "$f" ] || continue; echo "--- $f ---"; cat "$f"; done'
    data = _serve._http_json('POST', base, '/api/execute',
                      {'agentId': agent_id, 'command': read_command,
                       'purpose': 'Checking for JS selectors with no matching markup or dynamic creation anywhere in the sandbox.',
                       'sandboxId': sandbox_id}, key)
    if not isinstance(data, dict) or not data.get('allowed') or not data.get('stdout'):
        return []
    blob = data.get('stdout') or ''

    files = {}
    current = None
    for line in blob.split('\n'):
        m = re.match(r'^--- (.+) ---$', line)
        if m:
            current = m.group(1)
            files[current] = []
        elif current:
            files[current].append(line)

    html_lines = []
    js_entries = []
    for fname, lines in files.items():
        if fname.endswith('.html'):
            html_lines.extend(lines)
        elif fname.endswith('.js'):
            js_entries.append((fname, '\n'.join(lines)))
    html_text = '\n'.join(html_lines)
    js_text = '\n'.join(text for _, text in js_entries)

    # kind:name -> {name, kind, files:set}
    queried = {}
    for fname, text in js_entries:
        for m in re.finditer(r"""\.querySelector(?:All)?\(\s*['"]([.#][A-Za-z0-9_-]+)['"]""", text):
            raw = m.group(1)
            key_name = f"{raw[0]}:{raw[1:]}"
            kind = 'class' if raw[0] == '.' else 'id'
            queried.setdefault(key_name, {'name': raw[1:], 'kind': kind, 'files': set()})
            queried[key_name]['files'].add(fname)
        for m in re.finditer(r"""getElementById\(\s*['"]([A-Za-z0-9_-]+)['"]""", text):
            key_name = f"id:{m.group(1)}"
            queried.setdefault(key_name, {'name': m.group(1), 'kind': 'id', 'files': set()})
            queried[key_name]['files'].add(fname)

    dangling = []
    for entry in queried.values():
        name, kind, file_set = entry['name'], entry['kind'], entry['files']
        attr = 'class' if kind == 'class' else 'id'
        pattern = re.compile(attr + r'="[^"]*\b' + re.escape(name) + r'\b[^"]*"')
        if pattern.search(html_text):
            continue
        if kind == 'class':
            dynamic = re.compile(r"""classList\.add\([^)]*['"]""" + re.escape(name) + r"""['"]|className\s*=\s*['"][^'"]*\b""" + re.escape(name) + r"""\b""")
        else:
            dynamic = re.compile(r"""\.id\s*=\s*['"]""" + re.escape(name) + r"""['"]|setAttribute\(\s*['"]id['"]\s*,\s*['"]""" + re.escape(name) + r"""['"]""")
        if dynamic.search(js_text):
            continue
        dangling.append({'selector': ('.' if kind == 'class' else '#') + name, 'files': sorted(file_set)})
    return dangling


# The standard quality pipeline every Python-coding agent must pass before its
# work can clear the peer gate (Cut 4). flake8 = PEP 8 + the substantive F-codes;
# mypy = types; bandit = security (High/Medium); pytest-cov enforces >=90%
# coverage. All pre-installed in the _serve.SANDBOX_IMAGE. Deliberately NOT fenced off
# behind a helper that "%" or f-string formats a shell string -- each step ships
# as a literal argv list, so nothing an agent writes is ever interpolated into a
# shell command here.
# The standard clause appended to every Work Room coding system prompt (Cut 4).
# It tells the agent what the OBJECTIVE quality gate (below) will hold it to --
# instruction, not a weakening of the gate. Tests assert on this constant so the
# agent-facing standard stays in lockstep with the hard gate in sim.py.
CODING_STANDARDS_PROMPT = (
    '\n\nCODING STANDARDS (Cut 4): write clean, standards-compliant Python for any .py file you create or edit -- PEP 8 '
    'style, 4-space indent, short lines, meaningful names, no unused imports or dead variables. Before your work counts '
    'as done, it must also pass the SANDBOX quality pipeline, which runs automatically on the workspace and is '
    'non-negotiable: python -m flake8 ., python -m mypy ., python -m bandit -r ., and python -m pytest --cov=. '
    '--cov-fail-under=90 (all tools are pre-installed). Coverage is measured in TOTAL -- across ALL files in the '
    'workspace combined, not per-file -- so write tests that push the overall project coverage to 90% or higher. '
    'If you wrote a JS/HTML-only change, the Python pipeline is trivially green -- fine, but the JS steps then apply: '
    'node --check must parse every .js/.mjs file you wrote (it catches syntax errors without running the code), and '
    'any *.test.mjs you write is executed by node --test and must pass. A JS syntax error or a failing JS test is a '
    'RED pipeline, exactly like a flake8 error -- your work will NOT be approved until both the Python and JS steps '
    'are green. If you introduced real Python logic, '
    'structure it as an importable module with a real test_*.py your own tests would run, not a wall of untestable '
    'globals. Your task is NOT complete and your work will NOT be approved until that pipeline is green.'
)


def _quality_pipeline_steps(scope_files=None, style_lenient=False):
    """Build the standard quality pipeline steps. When `scope_files` is a
    non-empty list of .py paths, flake8/mypy/bandit AND pytest-cov are scoped
    to exactly those files instead of running on the whole sandbox -- a
    README-only or single-file change no longer fails on pre-existing findings
    in unrelated files, and --cov-fail-under=90 measures just the touched
    files (a one-file story in a worktree that also holds the base tree's
    files must not be failed by the OTHER files' coverage). The unscoped path
    keeps the whole-project total-coverage measure for full-tree deliverables.
    JS steps already no-op when no JS files exist.

    flake8 runs with `--isolated` and the host's setup.cfg flags mirrored on
    the CLI: the sandbox working dir may contain an agent-written setup.cfg/
    pyproject.toml that would otherwise change lint behavior run-to-run, and
    the host ignores stylistic codes the codebase deliberately uses (F-codes
    stay enforced). Without --isolated, a stray config in the sandbox tree
    also feeds flake8's plugin loader, which has crashed with _import_plugins
    errors on prose-lane tasks that never should have reached this pipeline."""
    _flake8_flags = (
        '--isolated --max-line-length=100 '
        '--extend-ignore=E501,E203,E128,E127,E221,E302,E303,E305,E306,E741,E731,E702,W292,W605'
    )
    if style_lenient:
        # Recovery cards ("Help:"/"Fix:" titles) repair legacy code that
        # already violates style; blocking the REPAIR on style nits creates
        # a fail -> help -> fail loop (consultant finding 2026-10-27: E231
        # rejections on Help cards). Style-class prefixes only -- the
        # semantic E7xx codes (bare except, ambiguous names, chained
        # assignment), E9xx, and every F code stay enforced.
        _flake8_flags += ',E1,E2,E3,E4,E5,W1,W2,W3,W4,W5,W6'
    if scope_files:
        _scoped = ' '.join(scope_files)
        # Coverage is scoped to the same files as lint when scope_files is
        # given, so a one-file story in a worktree that also holds the base
        # tree's files is not failed by --cov-fail-under=90 on the OTHER
        # files' coverage. Each file is a repeated --cov= flag: a space-
        # joined --cov=a.py b.py would pass b.py to pytest as a test path,
        # not a coverage source. bandit takes positional targets (its -f
        # is --format, not a file flag). The unscoped path below keeps the
        # whole-project measure for full-tree work.
        _cov_flags = ' '.join(f'--cov={f}' for f in scope_files)
        return [
            {'name': 'flake8', 'command': f'python -m flake8 {_flake8_flags} {_scoped}'},
            {'name': 'mypy', 'command': f'python -m mypy {_scoped}'},
            {'name': 'bandit', 'command': f'python -m bandit -q {_scoped}'},
            {'name': 'pytest-cov', 'command': f'python -m pytest {_cov_flags} --cov-fail-under=90 -q'},
            {'name': 'node-syntax', 'command': (
                'sh -c \'files=$(ls *.js *.mjs 2>/dev/null); '
                'if [ -z "$files" ]; then echo "no JS files -- skipping"; exit 0; fi; '
                'fail=0; for f in $files; do node --check "$f" || fail=1; done; '
                'exit $fail\'')},
            {'name': 'node-tests', 'command': (
                'sh -c \'ls *.test.mjs *.test.js 2>/dev/null | head -1 | grep -q . || '
                '{ echo "no JS test files -- skipping"; exit 0; }; '
                'node --test\'')},
        ]
    return [
        {'name': 'flake8', 'command': f'python -m flake8 {_flake8_flags} --exclude=tests .'},
        {'name': 'mypy', 'command': 'python -m mypy .'},
        {'name': 'bandit', 'command': 'python -m bandit -r . -q'},
        {'name': 'pytest-cov', 'command': 'python -m pytest --cov=. --cov-fail-under=90 -q'},
        # JS half of the gate (wrq-8: vanilla-JS engine). node --check parses
        # every .js/.mjs WITHOUT executing (pure syntax); node --test runs the
        # *.test.mjs suites the agents write (built-in node:test runner). Both
        # no-op green when the sandbox has no JS (mirrors the Python steps'
        # skip-when-no-.py in /api/pipeline), so a pure-Python project is
        # unaffected. node is baked into SANDBOX_IMAGE (world/sandbox/Dockerfile).
        {'name': 'node-syntax', 'command': (
            'sh -c \'files=$(ls *.js *.mjs 2>/dev/null); '
            'if [ -z "$files" ]; then echo "no JS files -- skipping"; exit 0; fi; '
            'fail=0; for f in $files; do node --check "$f" || fail=1; done; '
            'exit $fail\'')},
        {'name': 'node-tests', 'command': (
            'sh -c \'ls *.test.mjs *.test.js 2>/dev/null | head -1 | grep -q . || '
            '{ echo "no JS test files -- skipping"; exit 0; }; '
            'node --test\'')},
    ]


def _quality_pipeline_failure_snippet(step_name, stdout, stderr):
    """Extract the actionable failure detail from a quality-pipeline step's
    output, not just the tail. The old tail-4-lines approach returned bandit's
    severity summary ('High: 11') with no file/line, and cut flake8's actual
    error lines entirely -- agents could see THAT something failed but never
    WHY. Each tool's real findings live in the body of its output, so extract
    them by shape: flake8/mypy error lines start with a file path; bandit
    findings are Issue blocks with a Location; pytest failures are FAIL/ERROR
    lines plus the coverage line. Falls back to the last-8-lines tail when the
    shape doesn't match any known tool (keeps some signal for unknown steps)."""
    text = ((stderr or '') + '\n' + (stdout or '')).strip()
    if not text:
        return '(no output)'
    lines = text.splitlines()
    name = (step_name or '').lower()
    if 'bandit' in name:
        # Bandit findings: blocks of ">> Issue: [BXXX:...] ... Location: ./path:line"
        # Keep each Issue line plus its Location line (the two lines an agent
        # needs to find and fix the finding). Cap at 6 findings.
        findings = []
        for i, ln in enumerate(lines):
            if ln.strip().startswith('>> Issue:'):
                findings.append(ln.strip())
                # The Location line is a few lines below the Issue header.
                for j in range(i + 1, min(i + 12, len(lines))):
                    if lines[j].strip().startswith('Location:'):
                        findings.append('   ' + lines[j].strip())
                        break
                if len(findings) >= 12:  # 6 findings x 2 lines each
                    break
        if findings:
            return '\n'.join(findings)
    if 'flake8' in name or 'mypy' in name:
        # flake8/mypy: error lines look like "./path/file.py:12:5: E501 ..."
        # Keep up to 8 of them (the first errors are usually the real cause).
        errs = [ln for ln in lines if '.py:' in ln and (':' in ln.split('.py:')[-1])]
        if errs:
            return '\n'.join(errs[:8])
    if 'pytest' in name or 'cov' in name:
        # pytest: FAIL/ERROR lines + the coverage summary line.
        keep = [ln for ln in lines
                if ln.strip().startswith(('FAIL ', 'ERROR ', 'FAILED '))
                or 'TOTAL' in ln or 'Required test coverage' in ln
                or 'coverage failed' in ln.lower()]
        if keep:
            return '\n'.join(keep[:8])
    # Unknown step or no recognizable findings shape: last-8-lines tail so the
    # agent still sees the end of the output (better than the old 4-line cut).
    return '\n'.join(lines[-8:]) if lines else '(no output)'


# Client ceiling for the whole /api/pipeline call. The server runs up to 6
# steps (flake8/mypy/bandit/pytest-cov/node-syntax/node-tests), each with its
# own SANDBOX_TIMEOUT_S (serve.py, currently 120s) -- up to 720s of legitimate
# server-side work -- while _http_json's default is 30s for the WHOLE call, so
# real pipelines died as "request failed: timed out" rejections (329 in one
# 24h window). Keep this above 6 * SANDBOX_TIMEOUT_S with headroom.
_QUALITY_PIPELINE_TIMEOUT_S = 800


def _poll_pipeline_job(base, key, job_id, deadline_s=_QUALITY_PIPELINE_TIMEOUT_S):
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        status = _serve._http_json('GET', base, f'/api/pipeline/jobs/{job_id}',
                                   None, key, timeout=20)
        if not isinstance(status, dict):
            return {'error': 'pipeline job status unavailable'}
        if status.get('error'):
            return status
        if status.get('status') in ('done', 'failed'):
            return status.get('result') or {'error': 'pipeline job returned no result'}
        time.sleep(1.0)
    return {'error': 'pipeline job timed out'}


def _run_quality_pipeline(base, key, agent_id, sandbox_id, purpose, scope_files=None,
                         style_lenient=False):
    """Run the standard quality pipeline in the sandbox via /api/pipeline, which
    stops at the first failing step and returns per-step {name, exitCode, stdout,
    stderr} + failedStep (None on a full pass). Always returns a plain dict; never
    raises. Callers gate approval on `ok`. When `scope_files` is given, flake8/
    mypy/bandit AND pytest-cov are scoped to those files (see
    _quality_pipeline_steps). Passing checks are content-addressed and reused
    until the workspace or the step set changes; long runs are polled as a
    background job instead of holding one 300s HTTP request open."""
    steps = _quality_pipeline_steps(scope_files, style_lenient)
    resolved_sandbox = _serve._village_sandbox_id(agent_id, sandbox_id)
    sandbox_dir = _serve._sandbox_dir_for(resolved_sandbox)
    cache_key = (None if os.environ.get('PYTEST_CURRENT_TEST')
                 else work_reuse.check_key(sandbox_dir, steps, 'quality-v1'))
    cached = work_reuse.get_check(cache_key) if cache_key else None
    if cached:
        return cached
    resp = _serve._http_json('POST', base, '/api/pipeline',
                      {'agentId': agent_id, 'sandboxId': sandbox_id,
                       'purpose': purpose, 'steps': steps, 'background': True}, key,
                      timeout=_QUALITY_PIPELINE_TIMEOUT_S)
    if isinstance(resp, dict) and resp.get('jobId'):
        resp = _poll_pipeline_job(base, key, resp['jobId'])
    if not isinstance(resp, dict) or resp.get('error'):
        # A client-side timeout is infrastructure -- the sandbox pipeline is
        # still grinding server-side and there is nothing in the agent's code
        # to fix -- so it rides the same `infra` channel as the flake8
        # _import_plugins crash: no coaching note, no grade penalty, infra
        # wording on the send-back instead of "failed the quality pipeline".
        _err = str(resp.get('error') or '') if isinstance(resp, dict) else ''
        return {'ok': False, 'failedStep': None, 'results': [],
                'infra': 'timed out' in _err,
                'note': f'quality pipeline did not run: {resp.get("error") if isinstance(resp, dict) else "no response"}'}
    results = resp.get('results') or []
    failed = resp.get('failedStep')
    if failed:
        step = next((s for s in results if s.get('name') == failed), {})
        code = step.get('exitCode')
        snippet = _quality_pipeline_failure_snippet(
            failed, step.get('stdout'), step.get('stderr'))
        # A flake8 crash inside its own plugin loader (_import_plugins) is a
        # sandbox tooling fault, not a finding in the agent's code -- the
        # agent cannot fix it by editing files. Tag infra so sim.py skips
        # coaching/grade penalty and the infra-retry path can take over.
        _plugin_crash = (
            failed == 'flake8'
            and '_import_plugins' in ((step.get('stderr') or '') + (step.get('stdout') or ''))
        )
        return {'ok': False, 'failedStep': failed, 'results': results,
                'note': f'quality pipeline FAILED at {failed} (exit {code}):\n{snippet}',
                'infra': True if _plugin_crash else None}
    result = {'ok': True, 'failedStep': None, 'results': results, 'note': 'quality pipeline all green'}
    post_key = (None if os.environ.get('PYTEST_CURRENT_TEST')
                else work_reuse.check_key(sandbox_dir, steps, 'quality-v1'))
    if cache_key and post_key == cache_key:
        work_reuse.save_check(cache_key, result)
    return result


def _skipped_pipeline_result(note):
    """Synthetic quality-pipeline result used when the lane is not code-class.
    ok=True keeps _run_coding_content / _run_review_content fail paths clean;
    skipped=True lets pipeline_clause / _grade_code_requirement / the review
    prompt explain that the pipeline never ran instead of claiming it is green.
    results=[] and evidence='' are intentional: the sim's evidence done-gate
    (sim.py:2044-2056) only requires evidence for coding-class parents, so a
    non-code parent rests on pipelineOk alone."""
    return {'ok': True, 'failedStep': None, 'results': [], 'skipped': True,
            'note': note}


def _quality_pipeline_evidence(qp):
    """Item 7: extract OBJECTIVE evidence from a quality-pipeline run -- the
    actual per-step output (coverage line, flake8/mypy/bandit tails) that proves
    the code was really exercised by the standard gate, not just declared ok by a
    reviewer. Empty when the pipeline didn't run or produced no output. The sim's
    evidence-based done gate refuses to count a clean vote for a CODING-CLASS
    parent unless this is non-empty."""
    results = qp.get('results') or []
    lines = []
    for s in results:
        name = (s.get('name') or 'step')
        out = (s.get('stdout') or '').strip()
        err = (s.get('stderr') or '').strip()
        text = out + ('\n' + err if err else '')
        if not text.strip():
            continue
        tail = text.splitlines()[-2:]
        lines.append(f'[{name}]\n' + '\n'.join(tail))
    return '\n'.join(lines)[:2000]


def _commit_story_workspace(workspace, task_id=None):
    """Commit published changes on the isolated story branch so the peer gate can
    merge them. Shared workroom fallbacks stay uncommitted until their normal
    flow handles them."""
    if not str(workspace or '').startswith('story-'):
        return
    directory = os.path.join(_serve.SANDBOXES_DIR, workspace)
    if not os.path.exists(os.path.join(directory, '.git')):
        return
    try:
        _serve._run_git_sync(directory, ['add', '-A'])
        status = _serve._run_git_sync(directory, ['status', '--porcelain'])
        if (status.stdout or '').strip():
            _serve._run_git_sync(directory, ['commit', '-q', '-m',
                f'story changes for {task_id or workspace}'])
    except Exception as error:
        print(f'[story-commit] {workspace}: {error}')


def _tree_manifest(path):
    """Relative path -> sha256 for every regular file in `path`, skipping the
    same caches the workspace copy skips. Used to prove a coding turn actually
    produced a deliverable instead of publishing an empty or deletion-only
    change."""
    import hashlib
    import agents.work_reuse as work_reuse
    manifest = {}
    for base, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in work_reuse.SKIP]
        for name in files:
            full = os.path.join(base, name)
            if os.path.islink(full):
                continue
            try:
                with open(full, 'rb') as stream:
                    manifest[os.path.relpath(full, path)] = hashlib.sha256(
                        stream.read()).hexdigest()
            except OSError:
                continue
    return manifest


def _deliverable_problem(before, candidate):
    """Return a reason the candidate tree is not a deliverable, or None.
    A story must add or modify at least one file. A deletion-only change is
    refused: the live incident was an agent deleting its own page and the
    pipeline reviewing the empty tree."""
    changed = [p for p, digest in candidate.items() if before.get(p) != digest]
    deleted = [p for p in before if p not in candidate]
    if changed:
        return None
    if deleted:
        return ('the candidate change only deletes files (' + ', '.join(sorted(deleted)[:5])
                + '); nothing was added or modified')
    return 'the candidate change is empty: no file was added or modified'


def _save_code_library_report(agent_id, task, result, key=None):
    """A validated code publish leaves a real Library record of what changed.

    The completion gate requires a saved deliverable for code/story cards, but
    the coding executor publishes a staged workspace and does not itself write
    a Library file. Without this record a successful code card was refused as
    "missing_saved_deliverable" and a repair card repeated the same mismatch.
    Best-effort: a Library write failure must not discard the published code.
    """
    try:
        path = (f"archive/{int(time.time() * 1000)}-code-"
                f"{task.get('id') or 'adhoc'}.md")
        body = (
            f"# Code change: {task.get('title') or 'untitled task'}\n\n"
            f"Task: {task.get('id') or 'adhoc'}\n"
            f"Agent: {agent_id}\n"
            f"Staged workspace: {result.get('stagedWorkspace') or 'n/a'}\n"
            f"Source digest: {result.get('sourceDigest') or 'n/a'}\n"
            f"Result: {(result.get('note') or '').strip()}\n"
        )
        import sim as _sim_module
        with _sim_module.content_heartbeat_keepalive(task.get('id')):
            resp = _serve._http_json('POST', _serve.SELF_BASE_URL, '/api/library/file',
                                     {'agentId': agent_id, 'path': path, 'content': body,
                                      'source': 'firsthand'},
                                     key or _serve.get_or_create_agent_key(agent_id))
        if isinstance(resp, dict) and resp.get('error'):
            result['libraryError'] = str(resp['error'])[:200]
            result['note'] = ((result.get('note') or '') +
                              ' | Library record not saved: ' + result['libraryError'])
            return
        target = _serve._safe_library_path(path)
        if not target or not os.path.isfile(target):
            result['libraryError'] = 'library write did not land on disk'
            result['note'] = ((result.get('note') or '') +
                              ' | Library record not saved: write did not land')
            return
        result['libraryPath'] = path
    except Exception as error:
        result['libraryError'] = str(error)[:200]
        result['note'] = ((result.get('note') or '') +
                          ' | Library record not saved: ' + result['libraryError'])


def _run_coding_content(snapshot, agent_id, task, base_ctx=None):
    """Run coding in a private staged workspace; publish only validated changes."""
    import sim
    import agents.story_tracking as story_tracking
    import agents.staged_work as staged_work
    import agents.work_reuse as work_reuse
    workspace = _review_sandbox_id(snapshot, task, agent_id)
    parent = story_tracking.root(snapshot, task)
    staged_work.cleanup(_serve.SANDBOXES_DIR)
    with work_reuse.validation_lock(workspace):
        stage = None
        captured = []
        try:
            stage = staged_work.Stage(_serve.SANDBOXES_DIR, workspace,
                                      resume=parent.get('lastFailedWorkspace'))
            ok = _run_coding_content_inner(
                snapshot, agent_id, task, base_ctx, sandbox_override=stage.id,
                result_sink=lambda tid, result: captured.append(result))
            result = captured[-1] if captured else {'ok': False, 'note': 'No coding result was produced.'}
            if ok and result.get('ok'):
                # Boot-gated like the base sync: unit tests stub the executor
                # and write nothing, so they must not be judged on a real tree.
                problem = None
                if getattr(_serve.config, 'ALLOW_WORKSPACE_SYNC', False):
                    problem = _deliverable_problem(_tree_manifest(str(stage.original)),
                                                   _tree_manifest(str(stage.directory)))
                if problem:
                    stage.checkpoint()
                    result['ok'] = False
                    result['note'] = ('No deliverable was published: ' + problem
                                      + '. Existing workspace preserved.')
                else:
                    result['previousWorkspace'] = stage.publish()
                    result['codePublished'] = True
                    _commit_story_workspace(workspace, task.get('id'))
                    result['sourceDigest'] = work_reuse.tree_digest(str(stage.original))
                    result['note'] = str(result.get('note') or '') + ' | validated staged changes published'
                    _save_code_library_report(agent_id, task, result)
            else:
                stage.checkpoint()
                result['ok'] = False
                result['note'] = str(result.get('note') or 'Coding failed.') + ' Existing workspace preserved.'
            result['stagedWorkspace'] = stage.id
            result['resumedFromWorkspace'] = stage.resumed_from
        except Exception as error:
            result = {'ok': False,
                      'note': 'Staged coding failed; existing workspace preserved: ' + str(error)}
            if stage is not None:
                result['stagedWorkspace'] = stage.id
                result['resumedFromWorkspace'] = stage.resumed_from
        sim._store_content_result(task.get('id'), result)
        return bool(result.get('ok'))


def _run_coding_content_inner(snapshot, agent_id, task, base_ctx=None, sandbox_override=None, result_sink=None):
    """Port of tasks.js runCodingTask (the real Work Room coding pipeline):
    a real model call with probe-before-writing + heredoc-continuation handling,
    THEN /api/execute to actually run the command, THEN the four mechanical
    integrity checks on the written files (orphaned/phantom/dangling). Uses the
    SWE-bench coding tier for the code itself (always, regardless of how small
    the task sounds). Returns {ok, note} so runWorkroom can phrase the outcome."""
    import sim as _sim_module
    store_result = result_sink or _sim_module._store_content_result
    base = _serve.SELF_BASE_URL

    def _hb():
        try:
            task['_contentHeartbeat'] = time.time()
            _sim_module._bump_content_heartbeat(task.get('id'))
        except Exception:
            pass

    _hb()
    key = _serve.get_or_create_agent_key(agent_id)
    project_label = task.get('projectLabel') or ''
    backlog = task.get('title') or ''
    instructions = task.get('instructions')
    backlog_item = f'{backlog} -- {instructions}' if instructions else backlog
    # Per-story isolated worktree (falls back to the shared sandbox for
    # ad-hoc workroom tasks, non-code-class stories, or ensure failures).
    sandbox_id = sandbox_override or _review_sandbox_id(snapshot, task, agent_id)
    context_summary = base_ctx or ''
    import agents.python_delivery as python_delivery
    python_task = python_delivery.required(task)
    if python_task:
        current_directory = os.path.join(_serve.SANDBOXES_DIR, sandbox_id)
        context_summary += '\nActual current Python source files:\n' + python_delivery.context(current_directory, 8000)
    name = _agent_name(snapshot, agent_id)
    tier_slug = _serve._coding_tier_slug()
    if not tier_slug:
        store_result(task.get('id'),
                                          {'note': 'Tried to work on it, but no coding model tier is configured yet.',
                                           'ok': False})
        return False
    colab_available = bool(_serve.COLAB_CLI_AVAILABLE and _serve.COLAB_ENABLED)

    system_prompt = (f'You are {name}, a developer on a small team building {project_label or "a real, working small web application for the team"}. '
                     f'Current project state:\n{context_summary or "(nothing written yet -- you may be starting the first file.)"}\n\n'
                     f'Your task right now: {backlog_item}\n\n'
                     'Before writing your final answer, you may check real facts about how the ACTUAL running page behaves right now -- '
                     'what globals it defines and their real shape, what a button click or keypress actually does -- instead of guessing a '
                     'plausible-sounding name. To do this, respond with ONLY a JSON object, no shell command, no markdown fences, no explanation, '
                     'in exactly this shape: '
                     '{"probeRequest": {"path": "index.html", "actions": [{"type":"click","selector":"text=Play Challenge"}, '
                     '{"type":"keydown","key":"q"}], "probes": ["document.body.className", "typeof window.SomeGlobal"]}}\n'
                     'Action types are click ({selector}), keydown ({key}), wait ({ms}), eval ({code}). '
                     f'You can do this up to {MAX_CODE_PROBE_ROUNDS} times if you genuinely need to. '
                     + ('You may also offload real computation to a Google Colab runtime when this task genuinely needs compute your '
                        'sandbox cannot do -- heavy data parsing/analysis, numeric or ML work. To do that, respond with ONLY a JSON '
                        'object, no shell command, no markdown fences, no explanation, in exactly this shape: '
                        '{"colabRun": {"code": "print(...)", "purpose": "why you are computing this", "packages": [], '
                        '"timeout_seconds": 300, "runtimes": 1, "runtime": "cpu"}}\n'
                        'Your code MUST print everything you need to see. It runs on the Colab free tier, so keep runs small; a slot '
                        'may be refused (provisioning/availability), in which case you may retry once or fall back to a sandbox '
                        'command. To shard ONE computation across multiple runtimes, pass runtimes>1 (up to 5): the same code then runs '
                        'on each granted runtime with COLAB_SHARD_INDEX and COLAB_SHARD_COUNT env vars so it can split the work and '
                        'print its slice. It runs as the player\'s identity -- ONLY raw compute, never Google Drive/GCS/cloud APIs, '
                        'credentials, mining/bulk media, or any exfiltration site. '
                        f'You can do this up to {MAX_CODE_COLAB_ROUNDS} times if you genuinely need to. ' if colab_available else '')
                     + 'When ready to write the actual fix, respond with ONLY a single shell command, no explanation, no markdown fences, '
                     'that writes or updates the necessary file(s) using one or more heredocs, e.g.:\ncat > index.html << \'EOF\'\n<contents>\nEOF\n'
                     'Write real, complete, working code for this specific piece -- no placeholders, no "TODO," no stubs. '
                     'Keep it focused on just this task, building on what already exists rather than starting over. '
                     'If an existing file has grown large, PREFER adding a new small file (e.g. a separate .js file, linked with its own '
                     '<script src> tag) over rewriting the whole large file -- a full rewrite risks running out of room mid-file and being '
                     'cut off incomplete, which a small new file avoids. '
                     + CODING_STANDARDS_PROMPT)
    system_prompt += '\n' + __import__('core.structured_edits', fromlist=['*']).GUIDANCE
    system_prompt += '\n' + __import__('agents.coding_handoff', fromlist=['*']).context(
        snapshot, task, workspace_resolver=lambda item: _review_sandbox_id(snapshot, item, agent_id))
    system_prompt += '\n' + __import__('agents.coding_library', fromlist=['*']).GUIDANCE
    if python_task:
        system_prompt += '\n' + python_delivery.GUIDANCE
    messages = [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': 'Go ahead.'}]
    command = ''
    attempts = 0
    repair_rounds = 0
    probe_rounds = 0
    colab_rounds = 0
    library_rounds = 0
    infra_retries = 0
    MAX_INFRA_RETRIES = 1
    _opens = _closes = 0
    balanced = False
    while attempts <= CODE_CONTINUATION_ATTEMPTS:
        reply = None
        _hb()
        r = _serve._http_json('POST', base, '/api/chat',
                       {'model': tier_slug, 'messages': messages,
                        'max_tokens': _CODE_MAX_TOKENS, 'agentId': agent_id,
                        'service': project_label or task.get('productId') or '__general__',
                        'taskId': task.get('id')}, key)
        if not isinstance(r, dict) or r.get('error') or not r.get('reply'):
            budget_hit = _chat_error_result(r, 'this task used its full model-spend budget and the coding call was paused')
            if budget_hit:
                budget_hit['tier'] = tier_slug
                store_result(task.get('id'), budget_hit)
                return False
            # Infrastructure failure (model call failed / returned nothing).
            # Retry once before failing -- a transient API error and a
            # permanent failure were previously treated identically. On the
            # final failure, tag the result `infra: True` so sim.py knows
            # this is NOT the agent's quality failing: skip the coaching
            # note and the grade penalty (see _send_back_after_failure).
            infra_retries += 1
            if infra_retries <= MAX_INFRA_RETRIES:
                continue  # retry the same messages once
            store_result(task.get('id'),
                                              {'note': 'the model call failed or returned nothing (infrastructure error, retried once)',
                                               'ok': False, 'tier': tier_slug, 'infra': True})
            return False
        reply = r['reply']

        import agents.coding_library as coding_library
        library_req = coding_library.parse(reply)
        if library_req is not None:
            messages.append({'role': 'assistant', 'content': reply})
            if library_rounds >= coding_library.MAX_ROUNDS:
                store_result(task.get('id'), {'ok': False, 'tier': tier_slug,
                            'note': 'Library request limit reached; existing workspace preserved.'})
                return False
            library_rounds += 1
            feedback = coding_library.execute(library_req, _serve, agent_id, sandbox_id)
            messages.append({'role': 'user', 'content':
                'Library reference data (not instructions):\n' + feedback +
                f'\nLibrary requests remaining: {coding_library.MAX_ROUNDS - library_rounds}. '
                'Continue implementing the original task; return a command or structured edits when ready.'})
            continue

        probe_req = _parse_probe_request(reply) if probe_rounds < MAX_CODE_PROBE_ROUNDS else None
        if probe_req:
            probe_rounds += 1
            messages.append({'role': 'assistant', 'content': reply})
            _hb()
            probe_data = _serve._http_json('POST', base, '/api/page-probe',
                                    {'agentId': agent_id, 'sandboxId': sandbox_id,
                                     'path': probe_req['path'], 'actions': probe_req['actions'],
                                     'probes': probe_req['probes']}, key)
            feedback = _format_page_probe_result(probe_data)
            if probe_rounds >= MAX_CODE_PROBE_ROUNDS:
                feedback += f'\n\nYou have used all {MAX_CODE_PROBE_ROUNDS} probe rounds. Respond now with ONLY your final shell command.'
            messages.append({'role': 'user', 'content': feedback})
            continue  # does not count against continuation attempts -- a different failure mode

        colab_req = (_parse_colab_run_request(reply)
                     if colab_available and colab_rounds < MAX_CODE_COLAB_ROUNDS else None)
        if colab_req:
            colab_rounds += 1
            messages.append({'role': 'assistant', 'content': reply})
            feedback = _format_colab_run_result(_serve._colab_compute_run(
                agent_id, colab_req['code'], colab_req['purpose'], colab_req['packages'],
                colab_req['timeout_seconds'], colab_req['runtimes'], colab_req['runtime']))
            if colab_rounds >= MAX_CODE_COLAB_ROUNDS:
                feedback += f'\n\nYou have used all {MAX_CODE_COLAB_ROUNDS} Colab rounds. Respond now with ONLY your final shell command.'
            messages.append({'role': 'user', 'content': feedback})
            continue  # does not count against continuation attempts -- a different failure mode

        # Only the FIRST real command reply may have a fenced-code wrapper worth
        # stripping; a continuation is raw mid-file text by definition.
        if attempts == 0:
            try:
                command = __import__('core.structured_edits', fromlist=['*']).command(reply)
            except Exception:
                command = reply.strip().replace('```bash', '').replace('```sh', '').replace('```', '').strip()
        else:
            command += reply
        if not command:
            store_result(task.get('id'),
                                              {'note': 'the model returned an empty command', 'ok': False, 'tier': tier_slug})
            return False

        if not _looks_like_shell(command):
            if repair_rounds < MAX_CODE_REPAIR_ROUNDS:
                repair_rounds += 1
                attempts = 0
                messages.append({'role': 'assistant', 'content': reply})
                messages.append({'role': 'user', 'content': (
                    'That reply was not a shell command. Reply with ONLY one bare shell command '
                    '(a heredoc writing the files, or a structured edit object). No prose, no '
                    'JSON description of a command, no markdown fences.')})
                command = ''
                continue
            store_result(task.get('id'), {
                'note': 'the model did not return an executable shell command after '
                        f'{MAX_CODE_REPAIR_ROUNDS} correction(s); existing workspace preserved.',
                'command': command, 'ok': False, 'tier': tier_slug})
            return False

        _opens, _closes, balanced = _heredoc_balance(command)
        if balanced:
            break
        attempts += 1
        messages.append({'role': 'assistant', 'content': command})
        messages.append({'role': 'user',
                         'content': 'You were cut off before finishing. Continue EXACTLY where you left off -- do not repeat anything you '
                                    'already wrote, do not restart the heredoc or add a new one, just output the rest of the raw file content '
                                    'and the closing EOF line(s).'})
    # Loop exits via `balanced` (the normal case) or by exhausting the
    # continuation attempts -- the truncated-heredoc failure is handled HERE
    # so the loop's own exhausted-exit arc is a reachable, tested path.
    if not balanced:
        store_result(task.get('id'),
                                          {'note': f'generation still looked truncated after {CODE_CONTINUATION_ATTEMPTS} '
                                                   f'continuation attempt(s) ({_opens} heredoc(s) opened, {_closes} closed) -- not executed',
                                           'ok': False, 'tier': tier_slug})
        return False

    _hb()
    exec_data = _serve._http_json('POST', base, '/api/execute',
                           {'agentId': agent_id, 'command': command,
                            'purpose': f'Coding task: {backlog_item}', 'sandboxId': sandbox_id}, key)
    if not isinstance(exec_data, dict) or not exec_data.get('allowed'):
        if not isinstance(exec_data, dict):
            _block_reason = 'no response from the execution service'
        elif exec_data.get('timedOut'):
            _block_reason = 'the command timed out before the execution service returned'
        else:
            _block_reason = exec_data.get('reason') or exec_data.get('detail') or (
                # Consultant finding 2026-10-27: an empty refusal reason
                # failed Help cards with no actionable detail. Surface what
                # the wrapper does know -- the refused command -- instead of
                # the bare boilerplate.
                f'the execution service refused the command without a reason '
                f'(command: {str(command)[:200]})')
        store_result(task.get('id'),
                                          {'note': f"blocked: {_block_reason}",
                                           'command': command, 'ok': False, 'tier': tier_slug,
                                           # Item 2: the block reason rides the result channel
                                           # into sim._apply_content_result, where it lands in the
                                           # author's feedback buffer and shows up on the next
                                           # dispatch's perception block.
                                           'feedback': (f'Your command was blocked before running: '
                                                        f'{_block_reason}.')})
        return False
    ok = bool(exec_data.get('exitCode') == 0 and not exec_data.get('timedOut'))
    result = {'ok': ok, 'exitCode': exec_data.get('exitCode'), 'command': command, 'tier': tier_slug}
    if not ok:
        # Include a snippet of stdout/stderr so the author can see WHY the
        # command failed, not just the exit code. Mirrors the spike sandbox
        # executor's behavior (which already returns stdout/stderr to the
        # model) -- the coding path was dropping them, leaving agents with
        # "exited non-zero (code 2)" and no root cause.
        _err_snip = (exec_data.get('stderr') or '').strip()
        _out_snip = (exec_data.get('stdout') or '').strip()
        _detail_parts = []
        if _err_snip:
            _detail_parts.append('stderr: ' + '\n'.join(_err_snip.splitlines()[-6:]))
        if _out_snip:
            _detail_parts.append('stdout: ' + '\n'.join(_out_snip.splitlines()[-6:]))
        _detail = (' | '.join(_detail_parts) if _detail_parts else 'no output captured')
        store_result(task.get('id'),
                                          {'note': f'execution exited non-zero (code {exec_data.get("exitCode")}), '
                                                   f'timed out: {bool(exec_data.get("timedOut"))} | {_detail}',
                                           'command': command, 'ok': False, 'tier': tier_slug,
                                           'syntaxFailure': _is_command_syntax_error(_err_snip)})
        return False

    # ---- The four mechanical integrity checks (mirror runCodingTask) ----
    written = _extract_written_js_files(command)
    written_py = _extract_written_python_files(command)
    if written:
        unlinked = _find_unlinked_js_files(base, key, agent_id, sandbox_id, written)
        if unlinked:
            linked = _auto_link_js_files(base, key, agent_id, sandbox_id, unlinked, messages, tier_slug,
                                         task_id=task.get('id'))
            if linked['ok']:
                result['note'] = f'auto-linked previously-orphaned file(s) into index.html: {", ".join(unlinked)}'
            else:
                if linked.get('budgetExhausted'):
                    store_result(task.get('id'), {
                        'ok': False, 'budgetExhausted': True, 'tier': tier_slug,
                        'taskSpendUsd': linked.get('taskSpendUsd'),
                        'taskSpendAttempts': linked.get('taskSpendAttempts'),
                        'note': f'wrote {", ".join(unlinked)}, but the auto-link follow-up was paused because this task used its full model-spend budget.'})
                    return False
                plural = len(unlinked) > 1
                result['note'] = (f'WARNING: created {", ".join(unlinked)} but {"they are" if plural else "it is"} not referenced '
                                  f'by a <script src> tag in index.html, and the automatic follow-up to link {"them" if plural else "it"} '
                                  f'failed -- {linked["note"]}')
    phantom_refs = _find_phantom_script_refs(base, key, agent_id, sandbox_id)
    if phantom_refs:
        cleaned = _remove_phantom_script_refs(base, key, agent_id, sandbox_id, phantom_refs)
        ref_list = ', '.join(f'{p["file"]} (in {p["html"]})' for p in phantom_refs)
        phantom_note = (f'removed <script src> reference(s) to file(s) that don\'t actually exist: {ref_list}' if cleaned['ok']
                        else f'WARNING: HTML file(s) reference file(s) that were never written and don\'t exist: {ref_list} '
                             f'-- automatic cleanup failed ({cleaned["note"]})')
        result['note'] = f"{result.get('note')} | {phantom_note}" if result.get('note') else phantom_note

    dangling = _find_dangling_selector_refs(base, key, agent_id, sandbox_id)
    if dangling:
        pieces = []
        for d in dangling:
            pieces.append('{} (in {})'.format(d['selector'], ', '.join(d['files'])))
        dangling_note = ('WARNING: code queries selector(s) that don\'t exist anywhere in the sandbox\'s markup and are never created '
                         'dynamically either: {} -- likely a missing element, not real gameplay yet.'.format('; '.join(pieces)))
        result['note'] = f"{result.get('note')} | {dangling_note}" if result.get('note') else dangling_note

    # Cut 4 -- the quality pipeline is the fifth hard gate. The write + the four
    # mechanical checks succeeded, but the work is NOT done until the code meets
    # the standard (PEP 8, types, no bandit High/Medium, >=90% coverage). A red
    # pipeline fails the author's task here, before anything reaches review, so
    # there is nothing left for a reviewer to approve. Scope flake8/mypy/bandit
    # to the .py files this task actually wrote -- a README-only or single-file
    # change no longer fails on pre-existing findings elsewhere in the sandbox.
    # pytest-cov still runs project-wide (coverage is measured in total).
    # Lane-aware gate: only code-class work (taskType=='code', defaulting
    # untyped legacy cards to code) runs the real pipeline. Story/spike/
    # research cards that land here by mistake (e.g. a research card that
    # somehow reached _run_coding_content) skip the pipeline instead of failing
    # flake8/mypy/pytest on prose or empty sandboxes.
    if (task.get('taskType') or 'code') == 'code':
        if not written and not written_py and not python_task:
            # A code card that produced no Python or JS is not a Python
            # deliverable. Running the whole-tree pipeline here failed every
            # such card on pre-existing lint in unrelated files.
            qp = _skipped_pipeline_result(
                'no Python or JS files written -- quality pipeline skipped')
        else:
            if python_task:
                missing = python_delivery.check(os.path.join(_serve.SANDBOXES_DIR, sandbox_id))
                if missing:
                    store_result(task.get('id'), {
                        'note': 'Python deliverable is incomplete: ' + ' '.join(missing),
                        'command': command, 'ok': False, 'pipelineOk': False,
                        'tier': tier_slug, 'writtenPy': written_py, 'deliveryFailure': True,
                        'feedback': ('The Python deliverable is incomplete: ' + ' '.join(missing) +
                                     ' Create the missing tests/test_*.py files, importing the actual '
                                     'implementation module by its real path.')})
                    return False
            qp = _run_quality_pipeline(base, key, agent_id, sandbox_id,
                                       f'Quality gate for coding task: {backlog_item}',
                                       scope_files=written_py or None,
                                       style_lenient=str(backlog_item or '').startswith(('Help:', 'Fix:')))
    else:
        qp = _skipped_pipeline_result('non-code lane -- quality pipeline skipped')
    # writtenPy rides the result channel so _apply_content_result can persist
    # it on the task record: a gate review recovers the parent story's file
    # list from there to scope ITS quality pipeline, instead of whole-tree
    # linting every leftover file in the shared sandbox (which poisoned
    # concurrent reviews when any one agent's half-written copy broke flake8).
    if not qp['ok']:
        store_result(task.get('id'),
                                          {'note': f'wrote and ran the code, but {qp["note"]}',
                                           'command': command, 'ok': False, 'tier': tier_slug,
                                           'writtenPy': written_py,
                                           # infra=True when the failure was a sandbox
                                           # tooling crash (e.g. flake8 _import_plugins),
                                           # not a finding the agent can fix.
                                           'infra': bool(qp.get('infra'))})
        return False

    pipeline_note = ('quality pipeline skipped (non-code lane)' if qp.get('skipped')
                     else 'quality pipeline all green')
    store_result(task.get('id'),
                                      {'note': ((result.get('note') or ('Wrote and ran the code in the shared Work Room sandbox.'))
                                                + f' | {pipeline_note}'),
                                       'command': command, 'ok': True, 'tier': tier_slug,
                                       'writtenPy': written_py})
    return True


GRADE_MEETS = 'meets_requirement'
GRADE_FAILS = 'fails_requirement'
GRADE_UNSURE = 'insufficient_evidence'

# CS329A takeaway #2: Weaver-style verifier ensemble for grading.
# The JS planning model emits a per-requirement CHECKLIST ({id, question,
# section, type: code|jev|human}) that travels with a project's subtasks, but
# the Python review executor (_run_review_content) only ever produced a single
# holistic Jev actionable/clean verdict -- requirement-level grading existed
# only in the browser (grading.js runGradedReviewLoop). This adds the Python
# side: grade each checklist requirement with its OWN focused check, and let
# the ensemble outvote the holistic verdict the way the article's verifier
# ensemble does -- a mechanical 'code' requirement (ground truth = the quality
# pipeline) or a confident focused 'jev' grade that fails is a REAL defect even
# if the holistic call said clean; 'human' requirements and low-confidence
# grades surface to the player instead of being auto-decided.
#
# A requirement whose question is about code health (pipeline/tests/lint/build)
# is decided directly from the LIVE quality-pipeline result the executor
# already ran -- the most objective evidence the review path has. Any other
# 'code' requirement has no predicate that survives serialization (the JS
# predicates are in-memory functions), so it degrades to insufficient_evidence
# -- the same "absent predicate -> unsure" behavior as gradeCodeRequirement in
# grading.js -- rather than guessing.
_PIPELINE_HINT_WORDS = ('pipeline', 'flake8', 'mypy', 'bandit', 'pytest', 'test', 'lint', 'build', 'compile', 'ci', 'green')

# Cap on per-review spend: each focused 'jev' grade is a real Jev call, and the
# checklist is emitted for every subtask of a project -- grading five of them
# costs real money. Five focused checks (the planner's own "2-5 subtasks" scale)
# is a fair ensemble without letting a checklist turn one review into an
# invoice.
MAX_CHECKLIST_JEV_GRADES = 5
# Cap on escalations per review -- an unsure-heavy checklist can't spam the
# player; the first few real questions are enough to get the call.
MAX_CHECKLIST_ESCALATIONS = 3
# SWiRL-style process trace: when a review VERIFIES a checklist requirement
# failed, the review writes a compact lesson file into pending_review/skills/
# (auto-quarantined via source 'external'), where the existing skill-review
# sweep later judges keep/promote vs reject. Bounded: at most a few per
# review, short, and deduped on the requirement -- no repeat spam on re-review.
MAX_CHECKLIST_TRACE_FILES = 3
_PROCESS_TRACE_MAX_CHARS = 4000
# How many verified requirement FAILS the follow-up fix task names explicitly
# (bounded -- the fix instruction is a pointer to the evidence, not a dump).
MAX_QUEUE_FIX_REQUIREMENTS = 2


def _record_review_process_trace(base, key, agent_id, project_label, backlog, kind,
                                 name, checklist_grades, full_review):
    """Write the failed-requirement lessons from a review into
    pending_review/skills/review-trace/ as compact process-trace files, one per
    VERIFIED (GRADE_FAILS) checklist requirement, deduped on the requirement id
    and bounded in count and size. Returns how many were actually written (0
    when there is nothing new worth keeping -- the file already exists, or no
    requirement failed)."""
    written = 0
    for g in checklist_grades:
        if g.get('verdict') != GRADE_FAILS or written >= MAX_CHECKLIST_TRACE_FILES:
            continue
        req_id = g.get('id') or 'req'
        section = g.get('section') or ''
        question = g.get('question') or ''
        slug = _skill_slug(f'{project_label or "project"}-{section}-{req_id}') or 'req'
        path = f'pending_review/skills/review-trace/{_skill_slug(project_label or "project") or "project"}/{slug}.md'
        existing = None
        try:
            r = _serve._http_json('GET', base, '/api/library/file?path=' + urllib.parse.quote(path), None, key)
            existing = r.get('content') if isinstance(r, dict) else None
        except Exception:
            existing = None
        if existing:
            continue  # the requirement's lesson is already queued -- don't spam
        conf = g.get('confidence')
        body = (f'# Review trace: {question or req_id}\n\n'
                f'- Project: {project_label or "(unnamed)"}\n'
                f'- Requirement: "{question or req_id}"'
                + (f' (section: {section})' if section else '')
                + f' [{g.get("type", "")}]\n'
                f'- Found during a {kind} of "{backlog}" by {name}\n'
                f'- Verdict: verified FAILS'
                + (f' (Jev confidence {conf:.2f})' if isinstance(conf, float) else '')
                + '\n\n'
                f'## What the reviewer saw\n\n{full_review[:1200]}\n\n'
                f'## Lesson\n\n'
                f'Future work on "{project_label or backlog}" must satisfy this requirement: "{question or req_id}".\n')
        try:
            _serve._http_json('POST', base, '/api/library/file',
                       {'agentId': agent_id, 'path': path, 'content': body[:_PROCESS_TRACE_MAX_CHARS],
                        'source': 'external'}, key)
            written += 1
        except Exception:
            pass
    return written


def _grade_code_requirement(req, qp):
    """Mechanical grade for a 'code'-type checklist requirement. Returns a
    verdict string (GRADE_MEETS/GRADE_FAILS/GRADE_UNSURE) -- no Jev call, no
    cost. Ground truth is the live quality-pipeline result where the
    requirement is about code health; everything else is unknowable from the
    evidence this executor holds and degrades to GRADE_UNSURE (surface to the
    player), never a guess."""
    text = f"{req.get('question', '')} {req.get('section', '')}".lower()
    if any(word in text for word in _PIPELINE_HINT_WORDS):
        # A skipped pipeline (non-code lane) is not evidence the requirement was
        # met -- fall through to GRADE_UNSURE so it surfaces to the player
        # instead of auto-MEETING off a pipeline that never ran.
        if qp.get('skipped'):
            return GRADE_UNSURE
        return GRADE_MEETS if qp.get('ok') else GRADE_FAILS
    return GRADE_UNSURE
