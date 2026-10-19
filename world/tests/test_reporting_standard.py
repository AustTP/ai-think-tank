import evidence_policy
import reporting_standard
import serve


def test_reporting_standard_reaches_model_prompts_without_duplicate_injection():
    original = [{'role': 'system', 'content': 'Return the required JSON contract.'},
                {'role': 'user', 'content': 'Prepare a portfolio report.'}]
    result = evidence_policy.apply(original)
    assert reporting_standard.SOURCE_URL in result[0]['content']
    assert 'JSON/tool response contracts still apply' in result[0]['content']
    assert result == evidence_policy.apply(result)
    assert original[0]['content'] == 'Return the required JSON contract.'


def test_new_agents_receive_report_format_in_generated_instructions():
    rendered = serve._render_agents_md('Example', 'Analyst', {'instructions': []})
    assert reporting_standard.INSTRUCTIONS in rendered
    assert evidence_policy.EMAIL_INSTRUCTIONS in rendered


def test_short_automation_report_keeps_one_line_for_each_item():
    items = ['1. Approved: task-a', '2. Recovery attempt: task-b']
    report = reporting_standard.automation_body('Two actions; no completed stories.', items)
    for heading in ('Executive summary', 'Findings: action log', 'Conclusion', 'Next steps', 'Sources'):
        assert heading in report
    assert reporting_standard.SOURCE_URL in report
    assert all(report.count(item) == 1 for item in items)
