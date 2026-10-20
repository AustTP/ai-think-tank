import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATE = ROOT / '.env.template'
WORLD = ROOT / 'world'
IGNORE = {'PYTEST_CURRENT_TEST'}

READ_PATTERNS = (
    re.compile(r"(?:_load_env\(\)\.get|os\.environ\.get|os\.getenv)\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"),
    re.compile(r"\benv\.get\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"),
    re.compile(r"os\.environ\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]\s*\]"),
)


def _production_files():
    for path in WORLD.rglob('*.py'):
        if 'tests' in path.parts or '__pycache__' in path.parts:
            continue
        yield path


class EnvTemplateTest(unittest.TestCase):
    def test_every_read_key_is_documented(self):
        read = set()
        for path in _production_files():
            text = path.read_text(encoding='utf-8', errors='ignore')
            for pattern in READ_PATTERNS:
                read.update(pattern.findall(text))
        documented = set(re.findall(r'^(?:#\s?)?([A-Z][A-Z0-9_]*)=',
                                    TEMPLATE.read_text(encoding='utf-8'),
                                    re.M))
        missing = sorted(read - documented - IGNORE)
        self.assertEqual(missing, [], f'undocumented env keys: {missing}')

    def test_secret_shaped_template_values_are_blank(self):
        secret_name = re.compile(r'(KEY|SECRET|TOKEN|PASSWORD|PASS|HASH|SALT)$')
        non_empty = []
        for line in TEMPLATE.read_text(encoding='utf-8').splitlines():
            match = re.match(r'^(#\s?)?([A-Z][A-Z0-9_]*)=(.*)$', line)
            if not match:
                continue
            name, value = match.group(2), match.group(3).strip()
            if value and secret_name.search(name):
                non_empty.append(f'{name}={value[:8]}...')
        self.assertEqual(non_empty, [], f'secret values in template: {non_empty}')


if __name__ == '__main__':
    unittest.main()
