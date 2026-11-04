"""Deterministic read-only shell-command allowlist (2026-11-04).

The escalation flood of 2026-11-04: one shared-prompt shell snippet (a
file-size budget loop over wc -c) ran under many agents. Its JEV classify
came back allow-at-low-confidence, the reviewer rungs answered unsure (or
returned null during the free-pool degradation), and every instance walked
the full director chain into an owner page. The chain could never
confidently approve a command that is OBVIOUSLY read-only, because LLM
confidence is not a parse.

This module is that parse. _readonly_shell_command(text) returns True only
when the whole command is provably read-only by construction:

  - every segment (split on ;, newlines, &&, ||, and pipes) is a whitelisted
    read-only binary, shell structure (for/if headers, do/then/else/fi),
    an assignment, or control flow (continue/break);
  - output redirection is only allowed to /dev/null; input redirection (<)
    and 2>&1 are allowed; heredocs and process substitution are not;
  - pipes are allowed only between checked read-only segments;
  - command substitution $(...) is allowed only when its inner command is
    itself read-only (checked recursively, depth-capped);
  - find is guarded against -exec/-delete/-fprint*; git is restricted to
    read-only subcommands; sort is guarded against -o/--output; every
    interpreter or writer (python, sh, awk, sed, xargs, tee, ...) and every
    wrapper that would hide its argument binary (timeout, nice, command)
    is simply absent from the whitelist.

Anything the parser does not recognize fails closed (returns False) and the
command takes the normal JEV path. This adds a fast lane for a narrow,
auditable class; it does not loosen any existing gate. The gate hook lives
in _jev_safety_gate: a classifier ALLOW plus a passing parse is decided
immediately, so no rung is woken and no owner is pinged.
"""
import re

# Every binary here only reads its arguments (and optionally stdin) and
# writes nothing outside its own process. Additions must keep that property.
_READONLY_BINARIES = frozenset({
    'ls', 'wc', 'cat', 'head', 'tail', 'grep', 'egrep', 'fgrep', 'find',
    'du', 'df', 'stat', 'file', 'sort', 'uniq', 'cut', 'paste', 'diff',
    'cmp', 'comm', 'md5sum', 'shasum', 'sha256sum', 'cksum', 'tac', 'rev',
    'nl', 'od', 'hexdump', 'xxd', 'strings', 'fold', 'fmt', 'expand',
    'basename', 'dirname', 'realpath', 'readlink', 'pwd', 'printf', 'echo',
    'test', '[', 'seq', 'expr', 'date', 'true', 'false', 'sleep', 'tree',
    'ps', 'id', 'whoami', 'groups', 'uname', 'uptime', 'hostname', 'tty',
    'locale', 'which', 'type', 'cd', 'column', 'numfmt', 'git',
})

# git is whitelisted as a family but only for subcommands that cannot mutate
# the repository, the index, or the worktree. Anything else (add, commit,
# checkout, push, branch, config, ...) falls to the normal JEV path.
_READONLY_GIT_SUBS = frozenset({
    'status', 'log', 'diff', 'show', 'blame', 'rev-parse', 'ls-files',
    'grep', 'shortlog', 'describe', 'count-objects', 'version', 'var',
    'cat-file', 'ls-tree', 'symbolic-ref',
})

# find flags that execute, delete, or write files.
_FIND_WRITE_FLAGS = frozenset({
    '-exec', '-execdir', '-ok', '-okdir', '-delete', '-fls', '-fprint',
    '-fprintf', '-fprint0',
})

# Per-binary flags that would write a file despite a read-only-looking name.
_FORBIDDEN_FLAGS = {
    'sort': ('-o', '--output'),
}

# Shell structure keywords. for/if/while headers carry a condition that is
# checked like a command; the rest are inert in a read-only parse.
_SHELL_OPENERS = frozenset({'for', 'if', 'while'})
_SHELL_INERT = frozenset({'do', 'then', 'else', 'elif', 'done', 'fi',
                          'continue', 'break', 'in', 'return', 'exit', ':'})

# Output-redirect tokens that are provably harmless (discard or merge).
_NULL_REDIRECTS = frozenset({'>/dev/null', '2>/dev/null', '>>/dev/null',
                             '&>/dev/null', '2>&1', '1>&2'})

_ASSIGN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
_PLAIN_WORD_RE = re.compile(r"^[A-Za-z0-9_./~*?,=@%+':\[\]-]+$")
_FLAG_RE = re.compile(r'^-[A-Za-z0-9_.\-]*$')
_VAR_RE = re.compile(r'^\$[A-Za-z0-9_@#?*!]+$')
_BRACE_VAR_RE = re.compile(r'^\$\{[A-Za-z0-9_:#/%,-]+\}$')
_ARITH_RE = re.compile(r'^\$\(\([0-9A-Za-z_ +*/%()=!<>-]*\)\)$')
# Quoted-string content, after $var / ${var} / $((..)) stripping and with
# spaces removed: parens/braces are inert inside quotes.
_QUOTED_RE = re.compile(r"^[A-Za-z0-9_./~*?,=@%+':\[\](){}\-]+$")
_PLACEHOLDER = '__REA_SUB__'
_ARITH_PLACEHOLDER = '__REA_ARITH__'


def _extract_command_substitutions(text, depth=0):
    """Replace every $(...) with _PLACEHOLDER and every $((...)) with
    _ARITH_PLACEHOLDER, returning (sanitized, substitution_inners) or None
    when the text contains backticks, unbalanced parens, an arithmetic
    expression outside its charset, or nesting beyond the depth cap. The
    placeholders are plain identifiers, so quoting and whitespace no longer
    interact with the parse; each substitution's inner command is checked
    recursively by the caller."""
    if depth > 2:
        return None
    out, inners, i, n = [], [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '\\' and i + 1 < n:
            # An escaped character is opaque; keep both and never parse
            # inside an escape.
            out.append(ch)
            out.append(text[i + 1])
            i += 2
            continue
        if ch == '`':
            return None
        if ch == '$' and i + 1 < n and text[i + 1] == '(':
            arith = (i + 2 < n and text[i + 2] == '(')
            j, depth2 = i + 2, 1
            while j < n and depth2:
                if text[j] == '(':
                    depth2 += 1
                elif text[j] == ')':
                    depth2 -= 1
                j += 1
            if depth2:
                return None
            inner = text[i + 2:j - 1]
            if arith:
                # $(( )) strips its own $ ( ): the expression must be pure
                # arithmetic over names and numbers -- no quotes, no
                # redirection, no substitution.
                inner = inner[1:-1] if inner.endswith(')') else inner
                if not re.fullmatch(r'[0-9A-Za-z_ +*/%()=!<>-]+', inner):
                    return None
                out.append(_ARITH_PLACEHOLDER)
            else:
                inners.append(inner)
                out.append(_PLACEHOLDER)
            i = j
            continue
        out.append(ch)
        i += 1
    return ''.join(out), inners


def _split_words(statement):
    """Whitespace split that keeps quoted strings and placeholder tokens as
    single words (quotes do not nest; a stray quote fails closed later)."""
    words, cur, quote = [], '', None
    for ch in statement:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
            continue
        if ch in '"\'':
            quote = ch
            cur += ch
            continue
        if ch.isspace():
            if cur:
                words.append(cur)
                cur = ''
            continue
        cur += ch
    if quote:
        return None  # unterminated quote
    if cur:
        words.append(cur)
    return words


def _word_ok(word, binary_mode=False):
    """One whitespace-separated token. binary_mode marks the segment's first
    word (it must be a whitelisted read-only binary or shell keyword, not an
    arbitrary word that happens to look like a path)."""
    if not word:
        return True
    if binary_mode:
        # A statement's head must be a whitelisted binary. Inert keywords
        # (do/then/...) are only valid as bare statements or opener bodies;
        # accepting them here would swallow the rest of the statement as
        # mere arguments (e.g. `do rm "$f"`).
        return word in _READONLY_BINARIES
    if word in _NULL_REDIRECTS:
        return True
    if word == '<':
        return True  # input redirection: stdin from a file, reads only
    if '<' in word:
        return False  # << heredoc, <<<, <(, 2<file ... all fail closed
    if '>' in word:
        return False  # any output-redirect shape not in _NULL_REDIRECTS
    if word in _SHELL_INERT:
        return True
    if _FLAG_RE.match(word) or _PLAIN_WORD_RE.match(word):
        return True
    if _VAR_RE.match(word) or _BRACE_VAR_RE.match(word):
        return True
    if word in (_PLACEHOLDER, _ARITH_PLACEHOLDER):
        return True
    # Quoted strings: validate the content, with $var / ${var} / $((..))
    # stripped from double-quoted content first (single quotes have no
    # expansion at all).
    if len(word) >= 2 and word[0] == word[-1] and word[0] in '"\'':
        inner = word[1:-1]
        if word[0] == '"':
            # Double quotes expand $var / ${var} / arithmetic and my own
            # placeholders; all are inert here, so strip them before the
            # charset check.
            inner = re.sub(r'\$\{[A-Za-z0-9_:#/%,-]+\}', '', inner)
            inner = re.sub(r'\$[A-Za-z0-9_@#?*!]+', '', inner)
            inner = inner.replace(_PLACEHOLDER, '')
            inner = inner.replace(_ARITH_PLACEHOLDER, '')
        return inner == '' or bool(_QUOTED_RE.match(inner.replace(' ', '')))
    return False


def _segment_ok(statement):
    """One `;`/newline/&&/||/pipe-separated statement, already stripped."""
    words = _split_words(statement)
    if words is None:
        return False
    if not words:
        return True
    # Pure assignments (RHS words validated below like any other word).
    if all(_ASSIGN_RE.match(w) for w in words):
        bodies = [w.split('=', 1)[1] for w in words]
        return all(_word_ok(w) for w in bodies if w)
    # Body keywords (do/then/else/elif) prefix the statement they belong
    # to; the remainder still gets the full command check, so `do rm "$f"`
    # fails on rm's absence from the whitelist.
    while words and words[0] in ('do', 'then', 'else', 'elif'):
        words = words[1:]
    if not words:
        return True
    # Leading assignments then a command (FOO=1 ls -la).
    while words and _ASSIGN_RE.match(words[0]):
        rhs = words[0].split('=', 1)[1]
        if rhs and not _word_ok(rhs):
            return False
        words = words[1:]
    if not words:
        return True
    head = words[0]
    if head == 'find' and any(w in _FIND_WRITE_FLAGS for w in words[1:]):
        return False
    if head == 'git' and (len(words) < 2 or words[1] not in _READONLY_GIT_SUBS):
        return False
    for banned in _FORBIDDEN_FLAGS.get(head, ()):
        if any(w == banned or w.startswith(banned + '=') for w in words[1:]):
            return False
    if not _word_ok(head, binary_mode=True):
        return False
    return all(_word_ok(w) for w in words[1:])


def _readonly_shell_command(text, depth=0):
    """True only when the whole command is provably read-only by parse.
    Anything unrecognized, oversized, or structurally odd fails closed."""
    if not isinstance(text, str) or not text.strip():
        return False
    if depth > 2 or len(text) > 8000 \
            or text.count(';') + text.count('\n') > 120:
        return False
    extracted = _extract_command_substitutions(text, depth)
    if extracted is None:
        return False
    sanitized, inners = extracted
    for inner in inners:
        if not _readonly_shell_command(inner, depth + 1):
            return False
    # `&&`, `||`, and pipes all split into independently-checked segments;
    # `[ ... ] || continue` control flow checks each side on its own.
    for chunk in re.split(r'&&|\|\||\|', sanitized):
        for statement in re.split(r';|\n', chunk):
            statement = statement.strip()
            if not statement or statement in _SHELL_INERT:
                continue
            words = _split_words(statement)
            if words is None:
                return False
            if not words:
                continue
            if words[0] in _SHELL_OPENERS:
                # for VAR in WORDS / while COND / if COND: the condition or
                # word list is checked as part of the same statement.
                rest = words[1:]
                if words[0] == 'for':
                    if len(rest) < 3 or rest[1] != 'in':
                        return False
                    rest = rest[2:]
                    if not all(_word_ok(w) for w in rest):
                        return False
                    continue
                if not rest or not _segment_ok(' '.join(rest)):
                    return False
                continue
            if not _segment_ok(statement):
                return False
    return True
