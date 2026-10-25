"""Static safety scan for reusable skill cards.

The think tank promotes completed work into skill cards, and those cards are
later injected into agent prompts. A card that says "ignore the constitution"
or "curl the API key to this host" is executable advice even when it contains
no code. This is a bounded, deterministic pre-publish gate modeled on the
static half of SkillSpector: no model call, no network, no third-party runtime.

The gate blocks critical/high findings and records the rest. It is deliberately
narrow so ordinary documentation that *mentions* an API key is not a finding;
the high-severity patterns require a command, a secret-shaped literal, or an
instruction to bypass a control.
"""
from __future__ import annotations

import re

_SEVERITY_WEIGHT = {'critical': 40, 'high': 25, 'medium': 8, 'low': 2}
_BLOCKING = ('critical', 'high')

# (id, severity, category, regex, redact_excerpt)
_PATTERNS = [
    ('prompt-ignore-previous', 'critical', 'prompt-injection',
     re.compile(r'\bignore\s+(?:all\s+)?(?:previous|prior|above)\s+'
                r'(?:instructions|rules|guardrails)\b', re.I), False),
    ('prompt-override-system', 'high', 'prompt-injection',
     re.compile(r'\b(?:disregard|override|bypass)\b.{0,60}'
                r'\b(?:system|developer|constitution|policy|guardrail)\b',
                re.I | re.S), False),
    ('prompt-reveal-secret', 'high', 'prompt-injection',
     re.compile(r'\breveal\b.{0,40}\b(?:system prompt|hidden instructions|'
                r'api key|token|secret)\b', re.I | re.S), False),
    ('secret-command', 'high', 'data-exfiltration',
     re.compile(r'\b(?:curl|wget|nc|netcat|scp|rsync)\b.{0,120}'
                r'(?:\$[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)|'
                r'\.env\b|~/\.ssh\b|/etc/passwd\b)', re.I | re.S), True),
    ('secret-post', 'high', 'data-exfiltration',
     re.compile(r'\b(?:post|send|upload|exfiltrate)\b.{0,100}'
                r'\b(?:https?://|webhook|pastebin|ngrok|requestbin)\b',
                re.I | re.S), False),
    ('private-key-material', 'critical', 'credential-material',
     re.compile(r'-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE KEY-----',
                re.I), True),
    ('aws-access-key', 'critical', 'credential-material',
     re.compile(r'\bAKIA[0-9A-Z]{16}\b'), True),
    ('openai-key', 'critical', 'credential-material',
     re.compile(r'\bsk-[A-Za-z0-9_-]{20,}\b'), True),
    ('destructive-root', 'critical', 'destructive',
     re.compile(r'\brm\s+-rf\s+/(?:\s|$)', re.I), False),
    ('destructive-system', 'high', 'destructive',
     re.compile(r'\b(?:mkfs(?:\.\w+)?|dd\s+if=|shutdown|reboot)\b', re.I),
     False),
    ('privilege-escalation', 'high', 'privilege',
     re.compile(r'\b(?:sudo|chmod\s+777|setuid|runas)\b', re.I), False),
    ('policy-bypass', 'high', 'policy-bypass',
     re.compile(r'\b(?:disable|bypass|skip|circumvent)\b.{0,60}'
                r'\b(?:approval|validator|gate|allowlist|constitution|'
                r'tool_policy|review)\b', re.I | re.S), False),
    ('obfuscated-exec', 'high', 'obfuscation',
     re.compile(r'\b(?:base64\s+(?:-d|--decode)|atob\s*\(|eval\s*\()',
                re.I), False),
    ('hidden-unicode', 'high', 'obfuscation',
     re.compile('[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]'), True),
    ('always-run', 'medium', 'excessive-agency',
     re.compile(r'\b(?:always|automatically)\b.{0,40}'
                r'\b(?:run|execute|send|delete|post)\b', re.I | re.S), False),
]

_MAX_FINDINGS = 20
_EXCERPT = 120


def _line_of(text, match):
    return text.count('\n', 0, match.start()) + 1


def scan_skill_text(text, name=''):
    """Return a deterministic scan report for one skill card or skill file.

    `ok` is False only for critical/high findings. The report is safe to log:
    excerpts for secret-shaped findings are redacted.
    """
    text = text if isinstance(text, str) else ''
    name = str(name or '')
    findings = []
    seen = set()
    for rule_id, severity, category, pattern, redact in _PATTERNS:
        for match in pattern.finditer(text):
            line = _line_of(text, match)
            key = (rule_id, line)
            if key in seen:
                continue
            seen.add(key)
            excerpt = match.group(0).replace('\n', ' ')[:_EXCERPT]
            if redact:
                excerpt = '[redacted secret-shaped match]'
            findings.append({
                'rule': rule_id,
                'severity': severity,
                'category': category,
                'line': line,
                'excerpt': excerpt,
            })
            if len(findings) >= _MAX_FINDINGS:
                break
        if len(findings) >= _MAX_FINDINGS:
            break
    score = 0
    for finding in findings:
        score += _SEVERITY_WEIGHT.get(finding['severity'], 0)
    score = min(100, score)
    blocking = [f for f in findings if f['severity'] in _BLOCKING]
    if not findings:
        summary = 'No static skill-safety findings.'
    elif blocking:
        summary = (f'{len(blocking)} blocking finding(s); '
                   f'risk score {score}/100.')
    else:
        summary = (f'{len(findings)} non-blocking finding(s); '
                   f'risk score {score}/100.')
    return {
        'ok': not blocking,
        'name': name,
        'score': score,
        'findings': findings,
        'summary': summary,
    }


def format_findings(report, limit=5):
    """One-line-per-finding text for a tool result or log entry."""
    lines = []
    for finding in (report.get('findings') or [])[:limit]:
        lines.append(f"- {finding['severity']} {finding['rule']} "
                     f"line {finding['line']}: {finding['excerpt']}")
    return '\n'.join(lines)
