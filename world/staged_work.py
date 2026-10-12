"""Keep failed generated changes out of the working code workspace."""
import os
import re
import shutil
import uuid
from pathlib import Path

import work_reuse


class Stage:
    def __init__(self, root, workspace):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', workspace):
            raise ValueError('Invalid workspace identifier.')
        self.root = Path(root).resolve()
        self.original = self.root / workspace
        self.id = 'stage-' + uuid.uuid4().hex
        self.directory = self.root / self.id
        self.backup = self.root / ('previous-' + uuid.uuid4().hex)
        for path in (self.original, self.directory, self.backup):
            if path.resolve().parent != self.root:
                raise ValueError('Workspace must remain inside the sandbox root.')
        self.original.mkdir(parents=True, exist_ok=True)
        for directory, dirs, files in os.walk(self.original):
            if any(Path(directory, name).is_symlink() for name in dirs + files):
                raise ValueError('Staged work does not follow symbolic links.')
        self.before = work_reuse.tree_digest(str(self.original))
        shutil.copytree(self.original, self.directory,
                        ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache', '.mypy_cache'))

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
