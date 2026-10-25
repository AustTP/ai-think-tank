from scripts import check_business_processes as check


def test_repo_business_processes_document_passes():
    assert check.check_document() == []


def test_missing_date_and_missing_appendix_path_are_reported(tmp_path):
    (tmp_path / 'BUSINESS-PROCESSES.md').write_text(
        '### BP-01 Test\n\nNo date here.\n\n'
        '## 19. Appendix C\n\n| BP-01 | `world/missing.py` |\n\n'
        '## 20. Appendix D\n',
        encoding='utf-8',
    )
    errors = check.check_document(tmp_path)
    assert 'BP-01 has no last-checked date' in errors
    assert 'Appendix C path does not resolve: world/missing.py' in errors


def test_present_date_and_path_resolve(tmp_path):
    (tmp_path / 'world').mkdir()
    (tmp_path / 'world' / 'present.py').write_text('', encoding='utf-8')
    (tmp_path / 'BUSINESS-PROCESSES.md').write_text(
        '### BP-01 Test\n\n**Last checked:** 2026-10-25\n\n'
        '## 19. Appendix C\n\n| BP-01 | `world/present.py` |\n\n'
        '## 20. Appendix D\n',
        encoding='utf-8',
    )
    assert check.check_document(tmp_path) == []


def test_no_sections_is_an_error(tmp_path):
    (tmp_path / 'BUSINESS-PROCESSES.md').write_text(
        '## 19. Appendix C\n\n## 20. Appendix D\n', encoding='utf-8')
    assert check.check_document(tmp_path) == ['no BP sections found']


def test_http_and_wildcard_tokens_are_skipped(tmp_path):
    (tmp_path / 'BUSINESS-PROCESSES.md').write_text(
        '### BP-01 Test\n\n**Last checked:** 2026-10-25\n\n'
        '## 19. Appendix C\n\n| BP-01 | `https://example.com/x.py` `world/*.py` |\n\n'
        '## 20. Appendix D\n',
        encoding='utf-8')
    assert check.check_document(tmp_path) == []


def test_resolves_checks_world_subdirectories(tmp_path):
    (tmp_path / 'world' / 'core').mkdir(parents=True)
    (tmp_path / 'world' / 'core' / 'x.py').write_text('', encoding='utf-8')
    assert check._resolves('x.py', tmp_path) is True


def test_main_returns_zero_and_prints_on_success(monkeypatch, capsys):
    monkeypatch.setattr(check, 'check_document', lambda: [])
    assert check.main() == 0
    assert 'business processes check passed' in capsys.readouterr().out


def test_main_returns_one_and_prints_errors(monkeypatch, capsys):
    monkeypatch.setattr(check, 'check_document', lambda: ['boom'])
    assert check.main() == 1
    assert 'boom' in capsys.readouterr().out
