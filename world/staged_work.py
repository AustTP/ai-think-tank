"""Keep failed generated changes out of the working code workspace."""
import os
import re
import shutil
import uuid
import json
from pathlib import Path

import work_reuse


class Stage:
    def __init__(self, root, workspace, resume=None):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', workspace):
            raise ValueError('Invalid workspace identifier.')
        self.root = Path(root).resolve()
        self.original = self.root / workspace
        self.id = 'stage-' + uuid.uuid4().hex
        self.directory = self.root / self.id
        if workspace.startswith('story-'):
            (self.root / (self.id + '.runtime.json')).write_text(json.dumps({'workspace':workspace}), encoding='utf-8')
        self.backup = self.root / ('previous-' + uuid.uuid4().hex)
        for path in (self.original, self.directory, self.backup):
            if path.resolve().parent != self.root:
                raise ValueError('Workspace must remain inside the sandbox root.')
        self.original.mkdir(parents=True, exist_ok=True)
        for directory, dirs, files in os.walk(self.original):
            if any(Path(directory, name).is_symlink() for name in dirs + files):
                raise ValueError('Staged work does not follow symbolic links.')
        self.before = work_reuse.tree_digest(str(self.original))
        source = self.original
        self.resumed_from = None
        if resume and re.fullmatch(r'stage-[a-f0-9]{32}', resume):
            candidate = self.root / resume
            checkpoint = self.root / (resume + '.repair.json')
            if checkpoint.is_file() and not checkpoint.is_symlink() and candidate.is_dir() and not candidate.is_symlink():
                metadata = json.loads(checkpoint.read_text(encoding='utf-8'))
                if metadata.get('workspace') == workspace and metadata.get('baseDigest') == self.before:
                    source = candidate
                    self.resumed_from = resume
        for directory, dirs, files in os.walk(source):
            if any(Path(directory, name).is_symlink() for name in dirs + files):
                raise ValueError('Repair checkpoints must not contain symbolic links.')
        shutil.copytree(source, self.directory,
                        ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache', '.mypy_cache'))

    def checkpoint(self):
        path = self.root / (self.id + '.repair.json')
        with path.open('x', encoding='utf-8') as stream:
            json.dump({'workspace': self.original.name, 'baseDigest': self.before}, stream)

    def publish(self):
        for directory, dirs, files in os.walk(self.directory):
            if any(Path(directory, name).is_symlink() for name in dirs + files):
                raise ValueError('Staged changes contain symbolic links; not published.')
        if work_reuse.tree_digest(str(self.original)) != self.before:
            raise ValueError('Workspace changed during validation; staged changes were not published.')
        os.replace(self.original, self.backup)
        try:
            os.replace(self.directory, self.original)
        except BaseException:
            os.replace(self.backup, self.original)
            raise
        # Preserve the previous workspace for recovery rather than deleting it.
        return str(self.backup)
