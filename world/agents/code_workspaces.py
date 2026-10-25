"""Keep project code and its quality checks in the same bounded workspace."""
import hashlib
import re
import os
from pathlib import Path
import shutil
import json
import uuid


def has_web_assets(directory):
    if not os.path.isdir(directory):
        return None
    for base, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in __import__('agents.work_reuse', fromlist=['*']).SKIP and not Path(base, d).is_symlink()]
        if any(name.endswith(('.html', '.js', '.jsx', '.ts', '.tsx')) for name in files):
            return True
    return False


def fork_checkpoint(root, old_workspace, workspace, checkpoint):
    """Copy a valid repair checkpoint into a private story without changing the old one."""
    if not re.fullmatch(r'stage-[a-f0-9]{32}', checkpoint or ''):
        return None
    base = Path(root).resolve()
    if not all(re.fullmatch(r'[A-Za-z0-9_-]{1,100}', name) for name in (old_workspace, workspace)):
        raise ValueError('Invalid workspace name.')
    source = base / checkpoint
    manifest = base / (checkpoint + '.repair.json')
    if not source.is_dir() or source.is_symlink() or not manifest.is_file() or manifest.is_symlink():
        return None
    metadata = json.loads(manifest.read_text(encoding='utf-8'))
    digest = __import__('agents.work_reuse', fromlist=['*']).tree_digest(str(base / workspace))
    if metadata.get('workspace') != old_workspace or not digest or metadata.get('baseDigest') != digest:
        return None
    for directory, dirs, files in os.walk(source):
        if any(Path(directory, name).is_symlink() for name in dirs + files):
            raise ValueError('Checkpoint contains a symbolic link.')
    identifier = 'stage-' + uuid.uuid4().hex
    shutil.copytree(source, base / identifier)
    (base / (identifier + '.repair.json')).write_text(json.dumps({'workspace':workspace,'baseDigest':digest}),encoding='utf-8')
    (base / (identifier + '.runtime.json')).write_text(json.dumps({'workspace':workspace}),encoding='utf-8')
    return identifier


def ensure_test_directory(root, sandbox):
    """Provision the conventional test directory, including staged workspaces."""
    base = Path(root).resolve()
    directory = (base / sandbox / 'tests').resolve()
    if base not in directory.parents:
        raise ValueError('Test directory must stay inside the sandbox root')
    directory.mkdir(parents=True, exist_ok=True)


def sandbox_for(state, task):
    parent = __import__('agents.story_tracking', fromlist=['*']).root(state, task)
    product = (state.get('products') or {}).get(parent.get('productId')) or {}
    explicit = parent.get('_sandboxId') or parent.get('sandboxId') or product.get('sandboxId')
    if parent.get('codeWorkspaceVersion', 0) >= 2:
        identity = str(parent.get('id') or (str(parent.get('workOrderId')) + ':' + str(parent.get('workOrderStoryId'))))
        return 'story-' + hashlib.sha256(identity.encode()).hexdigest()[:20]
    if explicit:
        return explicit
    if parent.get('id') in (state.get('tasks') or {}) and not parent.get('codeWorkspaceVersion'):
        return 'workroom-shared'
    label = parent.get('workOrderId') or parent.get('projectLabel')
    if not label:
        return 'workroom-shared'
    key = str(parent.get('villageId') or 'main') + ':' + str(label)
    slug = re.sub(r'[^a-z0-9-]', '-',str(label).lower()).strip('-')[:32] or 'project'
    return 'project-' + slug + '-' + hashlib.sha256(key.encode()).hexdigest()[:10]


def prepare(state, task, root):
    sandbox = sandbox_for(state, task)
    parent = __import__('agents.story_tracking', fromlist=['*']).root(state, task)
    destination = Path(root).resolve() / sandbox
    if sandbox.startswith('story-') and not destination.exists():
        source = parent.get('_legacySandboxId') or parent.get('_sandboxId') or parent.get('sandboxId')
        if not source:
            request = next((r for r in state.get('backlogRequests', []) if r.get('id') == parent.get('workOrderId')), {})
            plan_story = next((s for s in request.get('plan', {}).get('stories', []) if s.get('id') == parent.get('workOrderStoryId')), {})
            prerequisites = [t for t in state.get('tasks', {}).values() if t.get('workOrderId') == parent.get('workOrderId')
                and t.get('workOrderStoryId') in plan_story.get('prerequisiteStoryIds', []) and t.get('status') == 'done'
                and not t.get('reviewOf')]
            if prerequisites:
                source = sandbox_for(state, prerequisites[-1])
        if source and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', source) and source != sandbox:
            original = Path(root).resolve() / source
            if original.is_dir() and not original.is_symlink():
                for directory, dirs, files in os.walk(original):
                    if any(Path(directory, name).is_symlink() for name in dirs + files):
                        raise ValueError('Story workspace seeds cannot contain symbolic links.')
                shutil.copytree(original, destination, ignore=shutil.ignore_patterns('.git', '__pycache__', '.pytest_cache', '.mypy_cache'))
    ensure_test_directory(root, sandbox)
    if sandbox.startswith(('project-', 'story-')):
        directory = os.path.join(root,sandbox)
        os.makedirs(directory,exist_ok=True)
        # Same third-party stub and test security configuration as legacy work.
        for name, contents in {
            'mypy.ini':'[mypy]\n\n[mypy-pandas.*]\nignore_missing_imports = True\n',
            '.bandit':'[bandit]\nexclude = ./tests\n',
        }.items():
            try:
                with open(os.path.join(directory,name),'x',encoding='utf-8') as stream:
                    stream.write(contents)
            except FileExistsError:
                pass
    return sandbox
