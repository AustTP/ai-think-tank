"""Residual coverage for the sandbox browser driver: defensive helper branches
and the login/captcha run paths. Playwright is fully replaced with fakes; no
browser launches and no network is used."""
import os
import sys
import types
import unittest.mock

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'sandbox'))

import browser_driver  # noqa: E402


class _Locator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.first = self
        self.last = self

    def wait_for(self, **kw):
        if self.page.locator_wait_error:
            raise self.page.locator_wait_error

    def is_enabled(self):
        return self.page.enabled

    def click(self, **kw):
        if self.selector == 'role':
            if self.page.role_click_error:
                raise self.page.role_click_error
        elif self.page.locator_click_error:
            raise self.page.locator_click_error
        self.page.clicks.append(self.selector)

    def fill(self, value, **kw):
        self.page.fills.append((self.selector, value))

    def press_sequentially(self, value, **kw):
        if self.page.sequential_error:
            raise self.page.sequential_error
        self.page.fills.append((self.selector, value))

    def press(self, key, **kw):
        if self.page.locator_press_error:
            raise self.page.locator_press_error

    def input_value(self):
        if self.page.input_value_error:
            raise self.page.input_value_error
        return 'value'

    def bounding_box(self):
        return {'x': 1, 'y': 2, 'width': 3, 'height': 4}


class _Frame:
    def __init__(self, page):
        self.page = page

    def locator(self, selector):
        return _Locator(self.page, selector)

    def wait_for_load_state(self, *a, **kw):
        if self.page.frame_wait_error:
            raise self.page.frame_wait_error

    def frame_element(self):
        if self.page.frame_element_error:
            raise self.page.frame_element_error
        return self


class _FakePage:
    def __init__(self):
        self._url = 'http://sandbox/page'
        self.html = ''
        self.content_error = None
        self.frame_error = None
        self.frame_wait_error = None
        self.frame_element_error = None
        self.frame_obj = None
        self.locator_wait_error = None
        self.locator_click_error = None
        self.role_click_error = None
        self.tab_error = None
        self.sequential_error = None
        self.locator_press_error = None
        self.input_value_error = None
        self.enabled = True
        self.goto_error = None
        self.click_error = None
        self.fill_error = None
        self.get_by_role_error = None
        self.button_click_error = None
        self.form_submit_error = True
        self.keyboard_error = None
        self.mouse_error = None
        self.state_error = None
        self.wait_error = None
        self.gotos = []
        self.fills = []
        self.clicks = []
        self.wait_states = []
        self.mouse = types.SimpleNamespace(
            move=self._mouse_move, click=lambda *a, **kw: None,
            wheel=lambda *a, **kw: None)
        self.keyboard = types.SimpleNamespace(press=self._key_press)

    def _mouse_move(self, *a, **kw):
        if self.mouse_error:
            raise self.mouse_error

    def _key_press(self, key, *a, **kw):
        if self.keyboard_error or (key == 'Tab' and self.tab_error):
            raise self.keyboard_error or self.tab_error

    @property
    def url(self):
        return self._url

    def content(self):
        if self.content_error:
            raise self.content_error
        return self.html

    def frame(self, url=None):
        if self.frame_error:
            raise self.frame_error
        return self.frame_obj

    def wait_for_timeout(self, ms):
        if self.wait_error:
            raise self.wait_error

    def wait_for_load_state(self, *a, **kw):
        self.wait_states.append(a)
        if self.frame_wait_error:
            raise self.frame_wait_error

    def goto(self, url, **kw):
        if self.goto_error:
            raise self.goto_error
        self.gotos.append(url)
        self._url = url
        return types.SimpleNamespace(status=200, headers={})

    def click(self, selector, **kw):
        if self.click_error:
            raise self.click_error
        self.clicks.append(selector)

    def fill(self, selector, value, **kw):
        if self.fill_error:
            raise self.fill_error
        self.fills.append((selector, value))

    def locator(self, selector):
        return _Locator(self, selector)

    def get_by_role(self, role, name=None):
        if self.get_by_role_error:
            raise self.get_by_role_error
        return _Locator(self, 'role')

    def evaluate(self, script):
        if self.state_error and 'document.body' in script:
            raise self.state_error
        if 'requestSubmit' in script:
            if self.form_submit_error:
                raise RuntimeError('no form')
            return True
        if 'navigator.userAgent' in script:
            return 'UA'
        if 'navigator.language' in script:
            return 'en-US'
        if 'resolvedOptions().timeZone' in script:
            return 'UTC'
        if 'a[href]' in script:
            return [{'href': 'http://x'}]
        return 'text'


def _run(action, page=None, ctx=None):
    page = page or _FakePage()
    ctx = ctx or types.SimpleNamespace(close=lambda: None)

    class _Pw:
        def __enter__(self):
            return types.SimpleNamespace()

        def __exit__(self, *a):
            return False

    with unittest.mock.patch.dict(sys.modules, {
            'playwright': types.ModuleType('playwright'),
            'playwright.sync_api': types.SimpleNamespace(
                sync_playwright=lambda: _Pw())}):
        with unittest.mock.patch.object(
                browser_driver, '_launch', lambda p, a: (ctx, page)):
            return browser_driver.run(action)


def test_social_and_login_url_helpers_swallow_parse_errors():
    with unittest.mock.patch('urllib.parse.urlparse', side_effect=Exception):
        assert browser_driver._social_host('http://x') == ''
        assert browser_driver._is_login_path('http://x/login') is False


def test_try_click_disabled_and_exception():
    page = _FakePage()
    page.enabled = False
    assert browser_driver._try_click(page, '#x') is False
    page.locator_wait_error = RuntimeError('boom')
    assert browser_driver._try_click(page, '#x') is False


def test_cf_helper_failures():
    page = _FakePage()
    page.content_error = RuntimeError('boom')
    assert browser_driver._cf_challenge_type(page) is None
    page.frame_error = RuntimeError('boom')
    assert browser_driver._cf_frame(page) is None
    assert browser_driver._cf_cleared(page, 'embedded') is True
    page.frame_wait_error = RuntimeError('boom')
    browser_driver._cf_wait_idle(page)
    page.locator = lambda selector: (_ for _ in ()).throw(RuntimeError('boom'))
    assert browser_driver._element_box(page, '#x') is None
    page.mouse_error = RuntimeError('boom')
    assert browser_driver._click_point(page, 1, 2) is False
    assert browser_driver._click_cf_box(page, None) is False


def test_wait_cf_cleared_loops_then_returns_final_state():
    page = _FakePage()
    with unittest.mock.patch.object(browser_driver, '_cf_cleared',
                                    return_value=False):
        ticks = iter([0, 0, 2])
        with unittest.mock.patch.object(browser_driver.time, 'monotonic',
                                        side_effect=lambda: next(ticks, 2)):
            assert browser_driver._wait_cf_cleared(page, 'managed',
                                                   timeout_s=1) is False


def test_solve_cloudflare_off_no_marker_and_single_attempts():
    page = _FakePage()
    assert browser_driver._solve_cloudflare(page, {'autoSolve': 'off'}) is None
    with unittest.mock.patch.object(browser_driver, 'CF_POLL_DEADLINE_S', 100):
        ticks = iter([0, 0, 1000])
        with unittest.mock.patch.object(browser_driver.time, 'monotonic',
                                        side_effect=lambda: next(ticks, 1000)):
            result = browser_driver._solve_cloudflare(
                page, {'autoSolve': 'cloudflare'})
    assert result['attempted'] is False
    assert result['solved'] is False

    page.html = "cType: 'non-interactive'"
    calls = {'n': 0}

    def content():
        calls['n'] += 1
        return page.html if calls['n'] == 1 else ''

    page.content = content
    with unittest.mock.patch.object(browser_driver, 'CF_MAX_SOLVE_ATTEMPTS', 1):
        result = browser_driver._solve_cloudflare(
            page, {'autoSolve': 'cloudflare'})
    assert result['solved'] is True

    page = _FakePage()
    page.html = 'challenges.cloudflare.com/turnstile/v'
    frame = _Frame(page)
    page.frame_obj = frame
    page.locator_wait_error = RuntimeError('no checkbox')
    page.locator_click_error = RuntimeError('no click')
    original = page.locator

    def locator(selector):
        if 'cf-turnstile' in selector:
            loc = original(selector)
            loc.bounding_box = lambda: None
            return loc
        return original(selector)

    page.locator = locator
    with unittest.mock.patch.object(browser_driver, 'CF_MAX_SOLVE_ATTEMPTS', 1):
        result = browser_driver._solve_cloudflare(
            page, {'autoSolve': 'all'})
    assert result['attempted'] is True


def test_solve_cloudflare_interactive_frame_error_paths():
    page = _FakePage()
    page.html = "cType: 'managed'"
    frame = _Frame(page)
    frame.wait_for_load_state = lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError('load'))
    frame.frame_element = lambda: (_ for _ in ()).throw(RuntimeError('el'))
    page.frame_obj = frame
    with unittest.mock.patch.object(browser_driver, 'CF_MAX_SOLVE_ATTEMPTS', 1):
        with unittest.mock.patch.object(browser_driver, '_cf_challenge_type',
                                        side_effect=["cType", None]):
            browser_driver._solve_cloudflare(page, {'autoSolve': 'all'})


def test_solve_cloudflare_outer_exception_is_reported():
    page = _FakePage()
    page.html = "cType: 'non-interactive'"
    with unittest.mock.patch.object(browser_driver, '_wait_cf_cleared',
                                    side_effect=RuntimeError('boom')):
        result = browser_driver._solve_cloudflare(
            page, {'autoSolve': 'cloudflare'})
    assert result['error'].startswith('RuntimeError')
    with unittest.mock.patch.object(browser_driver, '_solve_cloudflare',
                                    side_effect=RuntimeError('boom')):
        result = browser_driver._autosolve(page, {'autoSolve': 'cloudflare'})
    assert result['solved'] is False


def test_data_routes_non_list_and_response_errors():
    page = _FakePage()
    page.evaluate = lambda script: {'not': 'a list'}
    assert browser_driver._data_routes(page) == []
    page = _FakePage()
    state_error = RuntimeError('status')

    class _Headers:
        def get(self, name):
            raise RuntimeError('headers')

    class _Response:
        @property
        def status(self):
            raise state_error

        headers = _Headers()

    state = browser_driver._page_state(page, 10, response=_Response())
    assert state['status'] is None
    assert state['retryAfter'] is None


def test_host_timezone_reads_etc_timezone_and_falls_back(monkeypatch):
    monkeypatch.setenv('TZ', '')
    real_open = open

    def fake_open(path, *a, **kw):
        if path == '/etc/timezone':
            import io
            return io.StringIO('Europe/Paris\n')
        return real_open(path, *a, **kw)

    with unittest.mock.patch('builtins.open', fake_open):
        assert browser_driver._host_timezone() == 'Europe/Paris'
    with unittest.mock.patch('builtins.open', side_effect=OSError):
        with unittest.mock.patch('os.path.realpath', side_effect=OSError):
            assert browser_driver._host_timezone() == ''


def test_apply_safari_cookies_bad_json(tmp_path):
    (tmp_path / 'safari-cookies.json').write_text('not json')
    browser_driver._apply_safari_cookies(
        types.SimpleNamespace(add_cookies=lambda c: None), str(tmp_path))


def test_launch_sets_locale_from_host(monkeypatch):
    monkeypatch.setenv('LANG', 'en_US.UTF-8')
    monkeypatch.setenv('TZ', 'UTC')
    ctx = types.SimpleNamespace(pages=[], new_page=lambda: _FakePage(),
                                add_init_script=lambda s: None)
    chromium = types.SimpleNamespace(
        launch_persistent_context=lambda d, **kw: ctx)
    pw = types.SimpleNamespace(chromium=chromium)
    ctx, page = browser_driver._launch(pw, {})
    assert isinstance(page, _FakePage)


def test_captcha_act_validation_defaults_non_dict_and_range():
    assert _run({'op': 'captcha_act'})['ok'] is True
    out = _run({'op': 'captcha_act', 'clicks': ['x']})
    assert 'must be an' in out['error']
    out = _run({'op': 'captcha_act', 'clicks': [{'x': 99999, 'y': 0}]})
    assert 'out of range' in out['error']


def test_login_entry_url_rejects_cross_site():
    out = _run({'op': 'login', 'fields': [{'selector': '#u'}],
                'url': 'https://good.test/login',
                'loginEntryUrl': 'https://evil.test/login'})
    assert 'same site' in out['error']


def test_login_entry_url_walks_site_then_falls_back_to_login():
    page = _FakePage()
    page.get_by_role = lambda *a, **kw: _Locator(page, 'link')
    out = _run({'op': 'login', 'fields': [{'selector': '#u', 'value': 'v'}],
                'url': 'https://good.test/login',
                'loginEntryUrl': 'https://good.test/'}, page=page)
    assert out['ok'] is True
    assert page.gotos[0] == 'https://good.test/'
    assert page.gotos[-1] == 'https://good.test/login'


def test_login_entry_url_link_lookup_error_is_ignored():
    page = _FakePage()
    page.get_by_role_error = RuntimeError('no links')
    out = _run({'op': 'login', 'fields': [{'selector': '#u', 'value': 'v'}],
                'url': 'https://good.test/accounts/login',
                'loginEntryUrl': 'https://good.test/'}, page=page)
    assert out['ok'] is True
    assert page.gotos[0] == 'https://good.test/'
    assert page.gotos[-1] == 'https://good.test/accounts/login'


def test_social_write_guard_blocks_fill_on_facebook():
    page = _FakePage()
    page._url = 'https://www.facebook.com/x'
    out = _run({'op': 'fill', 'selector': '#q', 'url': 'https://www.facebook.com/x'},
               page=page)
    assert out['socialReadOnly'] is True


def test_login_falls_back_through_every_submit_path():
    page = _FakePage()
    page.sequential_error = RuntimeError('no typing')
    page.input_value_error = RuntimeError('no value')
    page.tab_error = RuntimeError('no tab')
    page.locator_press_error = RuntimeError('no enter')
    page.get_by_role_error = RuntimeError('no button')
    page.locator_click_error = RuntimeError('no candidate')
    page.form_submit_error = True
    out = _run({'op': 'login', 'fields': [{'selector': '#u', 'value': 'v'}]},
               page=page)
    assert out['ok'] is True
    assert out['submitUsed'].startswith('enter-global')
    assert out['filledFieldLengths'] == [-1]


def test_login_uses_role_button_when_enter_did_not_navigate():
    page = _FakePage()
    page.locator_click_error = RuntimeError('candidates fail')
    out = _run({'op': 'login', 'fields': [{'selector': '#u', 'value': 'v'}]},
               page=page)
    assert out['submitUsed'] == 'role:button:log-in'


def test_captcha_act_text_without_input_presses_enter():
    page = _FakePage()
    page.keyboard_error = RuntimeError('no keyboard')
    out = _run({'op': 'captcha_act', 'clicks': [], 'text': 'answer'}, page=page)
    assert out['ok'] is True


def test_error_path_tolerates_state_failure():
    page = _FakePage()
    page.click_error = RuntimeError('click failed')
    with unittest.mock.patch.object(browser_driver, '_state',
                                    side_effect=RuntimeError('state failed')):
        out = _run({'op': 'click', 'selector': '#x'}, page=page)
    assert out['ok'] is False
    assert 'click failed' in out['error']


def test_outer_harness_failure_returns_error():
    with unittest.mock.patch.dict(sys.modules, {
            'playwright': types.ModuleType('playwright'),
            'playwright.sync_api': types.SimpleNamespace(
                sync_playwright=lambda: (_ for _ in ()).throw(
                    RuntimeError('harness')))}):
        out = browser_driver.run({'op': 'read'})
    assert out['ok'] is False
    assert 'harness' in out['error']
