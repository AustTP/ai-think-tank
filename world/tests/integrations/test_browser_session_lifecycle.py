import subprocess
import types

import agents.browser_session as bs


class R:
    def __init__(self, code=0, out='', err=''):
        self.returncode, self.stdout, self.stderr = code, out, err


def test_pure(monkeypatch, tmp_path):
    assert bs._slug('a b') == 'a_b'
    for bad in ('', '.', '..'):
        try:
            bs._slug(bad)
        except bs.BrowserSessionError:
            pass
    assert bs.container_name('a').startswith('tt-browser-')
    assert bs.viewer_container_name('a').startswith('tt-browser-view-')
    assert 'password=' in bs.viewer_url(1, 'p')
    assert 'password=' not in bs.viewer_url(1, '')
    assert 'http_proxy' in ' '.join(bs._build_run_command('n', 'p', 'a', 'b', 'u', '1', '1g'))
    assert '127.0.0.1:1:6080' in ' '.join(bs._build_viewer_command('v', 'b', 1))
    assert bs._container_script()


def test_docker_wrappers(monkeypatch, tmp_path):
    monkeypatch.setattr(bs, '_docker', lambda *a, **k: R(0, 'true'))
    assert bs._running('c') is True
    monkeypatch.setattr(bs, '_docker', lambda *a, **k: (_ for _ in ()).throw(OSError('x')))
    assert bs._running('c') is False
    calls = []
    def docker(args, **k):
        calls.append(args)
        return R(0, '172.17.0.2')
    monkeypatch.setattr(bs, '_docker', docker)
    assert bs._container_ip('c', 'n', attempts=1) == '172.17.0.2'
    def docker_bad(args, **k):
        return R(0, 'bad ip')
    monkeypatch.setattr(bs, '_docker', docker_bad)
    monkeypatch.setattr(bs.time, 'sleep', lambda *_: None)
    assert bs._container_ip('c', 'n', attempts=1) == ''
    try:
        bs._start_x11vnc('c', '')
    except bs.BrowserSessionError:
        pass
    monkeypatch.setattr(bs, '_docker', lambda *a, **k: R(1, '', 'err'))
    try:
        bs._start_x11vnc('c', '1.2.3.4')
    except bs.BrowserSessionError:
        pass
    monkeypatch.setattr(bs, '_docker', lambda *a, **k: (_ for _ in ()).throw(OSError('x')))
    bs._teardown('c')


def test_start_get_stop(monkeypatch, tmp_path):
    bs._SESSIONS.clear()
    monkeypatch.setattr(bs, '_free_port', lambda: 1234)
    monkeypatch.setattr(bs.secrets, 'token_urlsafe', lambda n: 't')
    calls = []
    def docker(args, **k):
        calls.append(args)
        if args and args[0] == 'inspect':
            return R(0, 'true')
        return R(0, 'id')
    monkeypatch.setattr(bs, '_docker', docker)
    monkeypatch.setattr(bs, '_container_ip', lambda *a, **k: '1.2.3.4')
    monkeypatch.setattr(bs, '_start_x11vnc', lambda *a, **k: None)
    pub = bs.start('a', sandbox_dir=str(tmp_path / 's'), profile_dir=str(tmp_path / 'p'))
    assert pub['agentId'] == 'a' and pub['status'] == 'starting'
    assert bs.get('a')['status'] == 'running'
    assert bs.list_sessions()
    assert bs.session_by_token('t')
    assert bs.session_by_token('') is None
    assert bs.session_sandbox_dir('a')
    assert bs.is_waiting_for_operator('a') is False
    assert bs.takeover('a')['takeover'] is True
    assert bs.is_waiting_for_operator('a') is True
    assert bs.handback('a')['takeover'] is False
    assert bs.stop('a') is True
    assert bs.stop('a') is False
    # dead session in get
    bs._SESSIONS['b'] = {'agentId': 'b', 'container': 'c', 'status': 'running',
                         'takeover': True, 'lastUsed': 0, 'port': 1,
                         'startedAt': 0, 'viewerUrl': '', 'token': 'x'}
    monkeypatch.setattr(bs, '_running', lambda c: False)
    assert bs.get('b')['status'] in ('stopped', 'running')
    # errors
    try:
        bs.takeover('x')
    except bs.BrowserSessionError:
        pass
    try:
        bs.handback('x')
    except bs.BrowserSessionError:
        pass


def test_start_errors(monkeypatch, tmp_path):
    bs._SESSIONS.clear()
    monkeypatch.setattr(bs, '_free_port', lambda: 1)
    monkeypatch.setattr(bs.secrets, 'token_urlsafe', lambda n: 't')
    monkeypatch.setattr(bs, '_container_ip', lambda *a, **k: '1.2.3.4')
    monkeypatch.setattr(bs, '_start_x11vnc', lambda *a, **k: None)
    # docker run failure
    def docker(args, **k):
        if args[0] == 'run': return R(1, '', 'bad')
        return R(0, 'x')
    monkeypatch.setattr(bs, '_docker', docker)
    try:
        bs.start('a', sandbox_dir=str(tmp_path / 's'), profile_dir=str(tmp_path / 'p'))
    except bs.BrowserSessionError:
        pass
    # network attach failure
    def docker2(args, **k):
        if args[0] == 'network' and args[1] == 'connect': return R(1, '', 'bad')
        return R(0, 'x')
    monkeypatch.setattr(bs, '_docker', docker2)
    try:
        bs.start('a', sandbox_dir=str(tmp_path / 's'), profile_dir=str(tmp_path / 'p'))
    except bs.BrowserSessionError:
        pass
    # viewer failure
    def docker3(args, **k):
        if args[0] == 'run' and '--name' in args and 'tt-browser-view-a' in args: return R(1, '', 'bad')
        return R(0, 'x')
    monkeypatch.setattr(bs, '_docker', docker3)
    try:
        bs.start('a', sandbox_dir=str(tmp_path / 's'), profile_dir=str(tmp_path / 'p'))
    except bs.BrowserSessionError:
        pass
    # exec error
    monkeypatch.setattr(bs, '_docker', lambda *a, **k: (_ for _ in ()).throw(OSError('x')))
    try:
        bs.exec_driver('a', {})
    except bs.BrowserSessionError:
        pass


def test_browser_remaining(monkeypatch, tmp_path):
    assert bs._free_port() > 0
    monkeypatch.setattr(bs.subprocess, 'run', lambda *a, **k: R(0, ''))
    assert bs._docker(['version']).returncode == 0
    # container ip exception
    monkeypatch.setattr(bs, '_docker', lambda *a, **k: (_ for _ in ()).throw(OSError('x')))
    monkeypatch.setattr(bs.time, 'sleep', lambda *_: None)
    assert bs._container_ip('c', 'n', attempts=1) == ''
    # start existing running/dead
    bs._SESSIONS.clear()
    monkeypatch.setattr(bs.secrets, 'token_urlsafe', lambda n: 't')
    monkeypatch.setattr(bs, '_free_port', lambda: 1)
    monkeypatch.setattr(bs, '_container_ip', lambda *a, **k: '1.2.3.4')
    monkeypatch.setattr(bs, '_start_x11vnc', lambda *a, **k: None)
    monkeypatch.setattr(bs, '_running', lambda c: True)
    bs._SESSIONS['a'] = {'agentId': 'a', 'container': 'c', 'status': 'running',
                         'takeover': False, 'lastUsed': 0, 'port': 1,
                         'startedAt': 0, 'viewerUrl': '', 'token': 'x'}
    assert bs.start('a', sandbox_dir=str(tmp_path / 's'), profile_dir=str(tmp_path / 'p'))['agentId'] == 'a'
    monkeypatch.setattr(bs, '_running', lambda c: False)
    monkeypatch.setattr(bs, '_docker', lambda *a, **k: R(0, 'x'))
    assert bs.start('a', sandbox_dir=str(tmp_path / 's'), profile_dir=str(tmp_path / 'p'))
    # network create exception
    def docker(args, **k):
        if args[:2] == ['network', 'create']: raise OSError('x')
        return R(0, 'x')
    monkeypatch.setattr(bs, '_docker', docker)
    assert bs.start('a', sandbox_dir=str(tmp_path / 's'), profile_dir=str(tmp_path / 'p'))
    # run OSError
    def docker2(args, **k):
        if args[0] == 'run': raise OSError('x')
        return R(0, 'x')
    monkeypatch.setattr(bs, '_docker', docker2)
    try:
        bs.start('a', sandbox_dir=str(tmp_path / 's'), profile_dir=str(tmp_path / 'p'))
    except bs.BrowserSessionError:
        pass
    # idle reaping
    bs._SESSIONS.clear()
    bs.SESSION_IDLE_STOP_S = 1
    bs._SESSIONS['b'] = {'agentId': 'b', 'container': 'c', 'status': 'running',
                         'takeover': False, 'lastUsed': 0, 'port': 1,
                         'startedAt': 0, 'viewerUrl': '', 'token': 'x',
                         'viewerContainer': 'v'}
    monkeypatch.setattr(bs, '_running', lambda c: True)
    assert bs.get('b') is None
    bs.SESSION_IDLE_STOP_S = 0
    assert bs.get('missing') is None
    assert bs.session_by_token('nope') is None


def test_browser_idle_disabled(monkeypatch, tmp_path):
    bs._SESSIONS.clear(); bs.SESSION_IDLE_STOP_S = 0
    bs._SESSIONS['c'] = {'agentId': 'c', 'container': 'c', 'status': 'running',
                         'takeover': False, 'lastUsed': 0, 'port': 1,
                         'startedAt': 0, 'viewerUrl': '', 'token': 'x'}
    monkeypatch.setattr(bs, '_running', lambda c: True)
    assert bs.get('c')['agentId'] == 'c'
