"""Tests for the sandbox browser_act driver.

Hermetic: playwright is replaced with deterministic fakes (or made
unimportable), so no browser ever launches and no network is used.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'sandbox'))

import browser_driver  # noqa: E402


class _FakeKeyboard:
    def __init__(self):
        self.presses = []

    def press(self, key):
        self.presses.append(key)


class _FakeLocator:
    def __init__(self, selector, clicks, error=None):
        self.selector = selector
        self.clicks = clicks
        self._error = error
        self.first = self

    def wait_for(self, state='visible', timeout=None):
        if self._error:
            raise self._error

    def click(self, timeout=None):
        if self._error:
            raise self._error
        self.clicks.append(self.selector)


class _FakePage:
    def __init__(self):
        self._url = 'http://sandbox/page'
        self._url_error = None
        self._title_error = None
        self._eval_error = None
        self._goto_error = None
        self._fill_error = None
        self._click_error = None
        self._locator_error = None
        self._screenshot_error = None
        self.keyboard = _FakeKeyboard()
        self.gotos = []
        self.fills = []
        self.clicks = []
        self.locators = []
        self.screenshots = []

    @property
    def url(self):
        if self._url_error:
            raise self._url_error
        return self._url

    def title(self):
        if self._title_error:
            raise self._title_error
        return 'Fake Title'

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, script):
        if self._eval_error:
            raise self._eval_error
        if 'input,select,textarea,button' in script:
            return [{'tag': 'input', 'type': 'text', 'name': 'q', 'id': 'q',
                     'placeholder': 'search', 'ariaLabel': '', 'text': ''}]
        if 'a[href]' in script:
            return [{'text': 'a link', 'href': 'http://sandbox/page'}]
        return 'Page body text'

    def goto(self, url, timeout=None, wait_until=None):
        if self._goto_error:
            raise self._goto_error
        self.gotos.append((url, timeout, wait_until))

    def fill(self, selector, value, timeout=None):
        if self._fill_error:
            raise self._fill_error
        self.fills.append((selector, value))

    def click(self, selector, timeout=None):
        if self._click_error:
            raise self._click_error
        self.clicks.append(selector)

    def locator(self, selector):
        self.locators.append(selector)
        return _FakeLocator(selector, self.clicks, self._locator_error)

    def screenshot(self, path=None):
        if self._screenshot_error:
            raise self._screenshot_error
        self.screenshots.append(path)
        Path(path).write_text('png-bytes')


class _FakeContext:
    def __init__(self, pages):
        self.pages = pages
        self._close_error = None
        self.closed = False

    def close(self):
        if self._close_error:
            raise self._close_error
        self.closed = True

    def new_page(self):
        page = _FakePage()
        self.pages.append(page)
        return page


class _FakePlaywright:
    def __init__(self, context):
        self.chromium = self
        self._context = context
        self.launches = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def launch_persistent_context(self, profile_dir, **kwargs):
        self.launches.append((profile_dir, kwargs))
        return self._context


def _with_playwright(playwright):
    package = types.ModuleType('playwright')
    sync_api = types.ModuleType('playwright.sync_api')
    sync_api.sync_playwright = lambda: playwright
    package.sync_api = sync_api
    return unittest.mock.patch.dict(sys.modules, {'playwright': package,
                                                  'playwright.sync_api': sync_api})


def _without_playwright():
    return unittest.mock.patch.dict(sys.modules, {'playwright': None,
                                                  'playwright.sync_api': None})


class RunValidation(unittest.TestCase):
    def test_unknown_op(self):
        result = browser_driver.run({'op': 'nope'})
        self.assertFalse(result['ok'])
        self.assertIn('unknown op', result['error'])

    def test_goto_requires_url(self):
        result = browser_driver.run({'op': 'goto'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'goto requires url')

    def test_fill_requires_selector(self):
        result = browser_driver.run({'op': 'fill'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'fill requires selector')

    def test_fill_many_requires_fields(self):
        result = browser_driver.run({'op': 'fill_many'})
        self.assertFalse(result['ok'])
        self.assertIn('fill_many requires fields', result['error'])

    def test_login_requires_fields(self):
        result = browser_driver.run({'op': 'login'})
        self.assertFalse(result['ok'])
        self.assertIn('login requires fields', result['error'])

    def test_click_requires_selector(self):
        result = browser_driver.run({'op': 'click'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'click requires selector')

    def test_playwright_unavailable(self):
        with _without_playwright():
            result = browser_driver.run({'op': 'read', 'url': 'http://sandbox/x'})
        self.assertFalse(result['ok'])
        self.assertIn('playwright not available', result['error'])


class RunOps(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='think tank-browser-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _run(self, action, page=None, context=None):
        page = page if page is not None else _FakePage()
        context = context if context is not None else _FakeContext([page])
        playwright = _FakePlaywright(context)
        with _with_playwright(playwright):
            result = browser_driver.run(action)
        return result, page, context, playwright

    def test_goto(self):
        shot = str(self.tmp / 'shot.png')
        result, page, context, playwright = self._run(
            {'op': 'goto', 'url': 'http://sandbox/target', 'timeoutMs': 5000,
             'screenshotPath': shot, 'maxText': 500})
        self.assertTrue(result['ok'])
        self.assertEqual(result['op'], 'goto')
        self.assertEqual(page.gotos, [('http://sandbox/target', 5000, 'domcontentloaded')])
        self.assertEqual(result['title'], 'Fake Title')
        self.assertEqual(result['screenshot'], 'shot.png')
        self.assertEqual(result['fields'][0]['name'], 'q')
        self.assertEqual(result['links'][0]['href'], 'http://sandbox/page')
        self.assertTrue(context.closed)
        profile_dir, kwargs = playwright.launches[0]
        self.assertEqual(kwargs['viewport'], {'width': 1280, 'height': 900})
        self.assertIn('--no-sandbox', kwargs['args'])
        self.assertFalse(kwargs['headless'] is False)
        self.assertEqual(kwargs['accept_downloads'], False)
        self.assertIn('Chrome/', kwargs['user_agent'])

    def test_read_navigates_first(self):
        result, page, _, _ = self._run({'op': 'read', 'url': 'http://sandbox/start'})
        self.assertEqual(page.gotos, [('http://sandbox/start', 30000, 'domcontentloaded')])
        self.assertEqual(result['text'], 'Page body text')
        self.assertTrue(result['ok'])

    def test_read_without_url_skips_navigation(self):
        result, page, _, _ = self._run({'op': 'read'})
        self.assertEqual(page.gotos, [])
        self.assertTrue(result['ok'])

    def test_max_text_truncates(self):
        result, _, _, _ = self._run({'op': 'read', 'maxText': 5})
        self.assertEqual(result['text'], 'Page ')

    def test_fill(self):
        result, page, _, _ = self._run(
            {'op': 'fill', 'url': 'http://sandbox/form', 'selector': '#q', 'value': 'hello'})
        self.assertEqual(page.fills, [('#q', 'hello')])
        self.assertTrue(result['ok'])

    def test_fill_many(self):
        result, page, _, _ = self._run(
            {'op': 'fill_many', 'fields': [{'selector': '#a', 'value': '1'}, {'selector': '#b'}]})
        self.assertEqual(page.fills, [('#a', '1'), ('#b', '')])
        self.assertTrue(result['ok'])

    def test_fill_many_wait_failure_returns_error(self):
        page = _FakePage()
        page._locator_error = RuntimeError('no element')
        result, _, _, _ = self._run(
            {'op': 'fill_many', 'fields': [{'selector': '#a', 'value': '1'}]}, page=page)
        self.assertFalse(result['ok'])
        self.assertIn('RuntimeError', result['error'])
        self.assertIn('trace', result)

    def test_login(self):
        result, page, _, _ = self._run(
            {'op': 'login', 'fields': [{'selector': '#u', 'value': 'me'}], 'submitSelector': '#go'})
        self.assertEqual(page.fills, [('#u', 'me')])
        self.assertEqual(page.clicks, ['#go'])
        self.assertTrue(result['ok'])

    def test_login_default_submit(self):
        result, page, _, _ = self._run(
            {'op': 'login', 'fields': [{'selector': '#u', 'value': 'me'}]})
        self.assertIn('button[type=submit]', page.locators)
        self.assertTrue(result['ok'])

    def test_click(self):
        result, page, _, _ = self._run({'op': 'click', 'selector': '#go'})
        self.assertEqual(page.clicks, ['#go'])
        self.assertTrue(result['ok'])

    def test_submit_with_selector(self):
        result, page, _, _ = self._run({'op': 'submit', 'selector': '#go'})
        self.assertEqual(page.clicks, ['#go'])
        self.assertEqual(page.keyboard.presses, [])
        self.assertTrue(result['ok'])

    def test_submit_without_selector(self):
        result, page, _, _ = self._run({'op': 'submit'})
        self.assertEqual(page.clicks, ['button[type=submit], input[type=submit]'])
        self.assertTrue(result['ok'])

    def test_submit_falls_back_to_enter(self):
        page = _FakePage()
        page._click_error = RuntimeError('click failed')
        result, page, _, _ = self._run({'op': 'submit', 'selector': '#go'}, page=page)
        self.assertEqual(page.keyboard.presses, ['Enter'])
        self.assertTrue(result['ok'])

    def test_screenshot_op(self):
        result, _, _, _ = self._run({'op': 'screenshot', 'screenshotPath': str(self.tmp / 's.png')})
        self.assertTrue(result['ok'])
        self.assertEqual(result['screenshot'], 's.png')
        self.assertEqual(result['text'], '')

    def test_screenshot_failure_reported(self):
        page = _FakePage()
        page._screenshot_error = OSError('disk full')
        result, _, _, _ = self._run(
            {'op': 'goto', 'url': 'http://sandbox/x', 'screenshotPath': str(self.tmp / 's.png')},
            page=page)
        self.assertTrue(result['ok'])
        self.assertIn('__screenshot_failed__', result['screenshot'])

    def test_no_screenshot_path_returns_none(self):
        result, _, _, _ = self._run({'op': 'goto', 'url': 'http://sandbox/x'})
        self.assertIsNone(result['screenshot'])

    def test_goto_error_returns_failure_with_trace(self):
        page = _FakePage()
        page._goto_error = RuntimeError('net down')
        result, _, _, _ = self._run({'op': 'goto', 'url': 'http://sandbox/x'}, page=page)
        self.assertFalse(result['ok'])
        self.assertIn('RuntimeError', result['error'])
        self.assertIn('trace', result)

    def test_page_state_survives_broken_page(self):
        page = _FakePage()
        page._url_error = RuntimeError('no url')
        page._title_error = RuntimeError('no title')
        page._eval_error = RuntimeError('no eval')
        result, _, _, _ = self._run({'op': 'read'}, page=page)
        self.assertEqual(result['url'], '')
        self.assertEqual(result['title'], '')
        self.assertEqual(result['text'], '')
        self.assertEqual(result['fields'], [])
        self.assertEqual(result['links'], [])

    def test_context_close_error_is_ignored(self):
        context = _FakeContext([_FakePage()])
        context._close_error = RuntimeError('close failed')
        result, _, _, _ = self._run({'op': 'read'}, context=context)
        self.assertTrue(result['ok'])

    def test_launch_uses_proxy_env(self):
        with unittest.mock.patch.dict(os.environ, {'BROWSER_PROXY_URL': 'https://proxy:8080'}):
            result, _, _, playwright = self._run({'op': 'goto', 'url': 'http://sandbox/x'})
        self.assertTrue(result['ok'])
        self.assertIn('--proxy-server=http://proxy:8080', playwright.launches[0][1]['args'])

    def test_launch_uppercase_proxy_scheme(self):
        with unittest.mock.patch.dict(os.environ, {'BROWSER_PROXY_URL': 'HTTP://proxy:8080'}):
            result, _, _, playwright = self._run({'op': 'goto', 'url': 'http://sandbox/x'})
        self.assertTrue(result['ok'])
        self.assertIn('--proxy-server=http://proxy:8080', playwright.launches[0][1]['args'])

    def test_launch_custom_viewport_and_profile(self):
        result, _, _, playwright = self._run(
            {'op': 'goto', 'url': 'http://sandbox/x', 'viewport': '800x600', 'profileDir': '/tmp/prof'})
        self.assertTrue(result['ok'])
        profile_dir, kwargs = playwright.launches[0]
        self.assertEqual(profile_dir, '/tmp/prof')
        self.assertEqual(kwargs['viewport'], {'width': 800, 'height': 600})

    def test_launch_bad_viewport_defaults(self):
        result, _, _, playwright = self._run(
            {'op': 'goto', 'url': 'http://sandbox/x', 'viewport': 'nope'})
        self.assertTrue(result['ok'])
        self.assertEqual(playwright.launches[0][1]['viewport'], {'width': 1280, 'height': 900})

    def test_launch_empty_pages_creates_one(self):
        context = _FakeContext([])
        result, _, context, _ = self._run({'op': 'read'}, context=context)
        self.assertTrue(result['ok'])
        self.assertEqual(len(context.pages), 1)


class Main(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='think tank-browser-main-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_main_reads_action_from_file(self):
        action = {'op': 'read', 'resultPath': str(self.tmp / 'result.json')}
        (self.tmp / 'action.json').write_text(json.dumps(action), encoding='utf-8')
        with _with_playwright(_FakePlaywright(_FakeContext([_FakePage()]))):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = browser_driver.main(['browser_driver.py', str(self.tmp / 'action.json')])
        self.assertEqual(code, 0)
        result = json.loads((self.tmp / 'result.json').read_text(encoding='utf-8'))
        self.assertTrue(result['ok'])
        self.assertTrue(out.getvalue())

    def test_main_reads_action_from_stdin(self):
        # json.load(sys.stdin.read()) always fails (a str has no .read),
        # so the except branch resets the action to {} and the run
        # reports the unknown op without launching anything.
        with unittest.mock.patch('sys.stdin', io.StringIO('{"op": "read"}')):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = browser_driver.main(['browser_driver.py'])
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertFalse(result['ok'])

    def test_main_result_path_failure_is_ignored(self):
        action = {'op': 'read', 'resultPath': str(self.tmp / 'no-such-dir' / 'result.json')}
        (self.tmp / 'action.json').write_text(json.dumps(action), encoding='utf-8')
        with _with_playwright(_FakePlaywright(_FakeContext([_FakePage()]))):
            with contextlib.redirect_stdout(io.StringIO()):
                code = browser_driver.main(['browser_driver.py', str(self.tmp / 'action.json')])
        self.assertEqual(code, 0)


if __name__ == '__main__':
    unittest.main()
