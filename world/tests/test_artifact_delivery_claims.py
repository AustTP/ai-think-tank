from types import SimpleNamespace

import librarian
import review_quality


REPORT = '''# Final Findings
No substantive findings were obtained because the documentation was inaccessible.
7. Use execute_script to write `coinbase_api_trading_capabilities.csv`: Unable to write due to lack of data.
A CSV was intended to be produced, but no data was available to populate it.
'''
CONTRACT = {'instructions': 'Collect and review official API documentation',
            'acceptanceCriteria': 'Compiled documentation on trading capabilities'}


def test_honest_non_delivery_is_not_a_claim_of_a_missing_artifact(tmp_path):
    assert review_quality.artifact_references(REPORT, CONTRACT) == []
    checks = review_quality.artifact_checks(REPORT, CONTRACT, lambda p: None)
    # This only clears artifact custody; independent review must still reject
    # the lack of substantive findings against the documentation contract.
    assert checks['errors'] == []
    report = tmp_path / 'report.md'
    report.write_text(REPORT)
    task = {**CONTRACT, 'id': 'research', 'libraryPath': 'report.md'}
    custody = librarian.inspect_report(task, SimpleNamespace(
        _safe_library_path=lambda p: str(tmp_path / p), log_action=lambda *a, **k: None))
    assert custody['missing'] == []
    assert not (tmp_path / 'coinbase_api_trading_capabilities.csv').exists()


def test_explicit_contract_file_remains_required_even_when_admitted_missing():
    contract = {**CONTRACT, 'acceptanceCriteria': 'Save coinbase_api_trading_capabilities.csv in Library'}
    checks = review_quality.artifact_checks(REPORT, contract, lambda p: None)
    assert checks['errors'] == ['Cited artifact is missing from Library: coinbase_api_trading_capabilities.csv']


def test_affirmative_delivery_claim_remains_strict():
    checks = review_quality.artifact_checks('Saved capabilities.csv in Library.', CONTRACT, lambda p: None)
    assert checks['errors'] == ['Cited artifact is missing from Library: capabilities.csv']
    checks = review_quality.artifact_checks('Saved capabilities.csv with missing values.', CONTRACT, lambda p: None)
    assert checks['errors'] == ['Cited artifact is missing from Library: capabilities.csv']
    checks = review_quality.artifact_checks('Saved planned.csv.', CONTRACT, lambda p: None)
    assert checks['errors'] == ['Cited artifact is missing from Library: planned.csv']


def test_later_labeled_contents_can_be_saved_after_earlier_mention():
    report = 'Plan: create capabilities.csv after research.\nNo contents here.\n\n## capabilities.csv\n```csv\ncapability,basis\npublic data,unknown\n```'
    assert librarian.embedded_csv(report, 'capabilities.csv') == 'capability,basis\r\npublic data,unknown\r\n'
