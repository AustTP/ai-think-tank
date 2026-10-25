"""Turn bounded, exact-match edits into one safely quoted sandbox command."""
import base64
import json
import shlex

GUIDANCE = ('Prefer one JSON object {"edits":[{"path":"module.py","old":"exact unique existing text",'
            '"new":"replacement text"}],"commands":["python -m pytest tests/test_module.py -q"]}. '
            'Use old:null only to create a new file that does not exist. Prefer one or two small edits '
            'that fit within the response token limit; '
            'inspect the current file first. Do not rewrite unrelated files. Commands may install '
            'required packages inside this story container; record pinned requirements. '
            'JSON is validated before execution; never emit explanatory prose as commands.')


def _load(reply):
    text = (reply or '').strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # Tolerate a fenced or narrated wrapper around the one JSON object the
    # model was asked for. The old strict parse failed on ```json fences and
    # the caller fell back to executing the raw reply as a shell command,
    # which is how prose like "Let me..." became a shell error.
    start, end = text.find('{'), text.rfind('}')
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    raise ValueError('Reply was not a JSON edit object.')


def command(reply):
    data = _load(reply)
    edits, commands = data.get('edits'), data.get('commands', [])
    if (not isinstance(edits, list) or not 1 <= len(edits) <= 4 or not isinstance(commands, list)
            or len(commands) > 3 or any(not isinstance(c, str) or len(c) > 2000 for c in commands)):
        raise ValueError('Supply one to four exact-match edits and at most three commands.')
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {'path','old','new'}:
            raise ValueError('Each edit needs path, old and new.')
        path = edit['path']
        if (not isinstance(path, str) or not path or path.startswith('/') or '\\' in path or ':' in path
                or '..' in path.split('/') or any(ord(c) < 32 for c in path)):
            raise ValueError('Edits require a relative sandbox path.')
        if (edit['old'] is not None and (not isinstance(edit['old'], str) or not edit['old'] or len(edit['old']) > 8000)
                or not isinstance(edit['new'], str) or len(edit['new']) > 8000):
            raise ValueError('Edits require nonempty exact old text or null, and bounded new text.')
    script = '''import ast,json,pathlib,os,tempfile
edits=json.loads(DATA)
root=pathlib.Path.cwd().resolve()
pending={}
for edit in edits:
    path=root/edit['path']
    if root not in path.resolve().parents or any(p.is_symlink() for p in [path,*path.parents] if p!=root):
        raise ValueError('Edit path must remain inside this workspace without symlinks')
    old=edit['old']
    text=pending.get(path,path.read_text(encoding='utf-8') if path.exists() else None)
    if old is None:
        if text is not None: raise ValueError('Create would overwrite existing file: '+edit['path'])
        text=edit['new']
    else:
        if text is None or text.count(old)!=1: raise ValueError('Old text must match exactly once: '+edit['path'])
        text=text.replace(old,edit['new'],1)
    if path.suffix=='.py': ast.parse(text,filename=edit['path'])
    pending[path]=text
for path,text in pending.items():
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.edit-')
    with os.fdopen(fd,'w',encoding='utf-8') as stream: stream.write(text)
    os.replace(tmp,path)
'''.replace('DATA', repr(json.dumps(edits)))
    encoded = base64.b64encode(script.encode()).decode()
    runner = 'import base64;exec(base64.b64decode(' + repr(encoded) + '))'
    return 'python -c ' + shlex.quote(runner) + ''.join(' && sh -c ' + shlex.quote(c) for c in commands)
