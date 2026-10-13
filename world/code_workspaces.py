"""Keep project code and its quality checks in the same bounded workspace."""
import hashlib
import re
import os
from pathlib import Path


def ensure_test_directory(root, sandbox):
    """Provision the conventional test directory, including staged workspaces."""
    base = Path(root).resolve()
    directory = (base / sandbox / 'tests').resolve()
    if base not in directory.parents:
        raise ValueError('Test directory must stay inside the sandbox root')
    directory.mkdir(parents=True, exist_ok=True)


def sandbox_for(state, task):
    parent = (state.get('tasks') or {}).get(task.get('reviewOf')) or task
    product = (state.get('products') or {}).get(parent.get('productId')) or {}
    explicit = parent.get('_sandboxId') or parent.get('sandboxId') or product.get('sandboxId')
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
    ensure_test_directory(root, sandbox)
    if sandbox.startswith('project-'):
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
