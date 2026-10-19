"""Player-selected report format shared by all agent reporting paths."""

SOURCE_URL = 'https://visme.co/blog/report-writing-format/'
INSTRUCTIONS = (
    'REPORT FORMAT POLICY (player instruction): Use ' + SOURCE_URL +
    ' as the shared format reference for every report, including portfolio, research, technical, '
    'status, recovery and email reports. For substantial reports organize: title (author/date/audience), '
    'contents, executive summary, introduction, methodology, findings, conclusion, recommendations, '
    'and supporting references/appendices. For short updates use a concise title, summary, findings, '
    'and next steps; include sources and limitations where relevant. Scale sections to purpose; '
    'avoid empty sections. Use clear headings, consistent tables and useful visuals. '
    'The player has now supplied the report-format preference: use this guide when a story '
    'is waiting for the preferred report format. Preserve specific output/schema requirements; '
    'attach machine-readable data separately when required. Formatting never substitutes for '
    'verified evidence, genuine output artifacts, passing acceptance criteria, or independent review. '
    'Do not fabricate data, citations or results to fill the template. Exact JSON/tool response '
    'contracts still apply; format the report artifact, not the protocol response.'
)


def automation_body(summary, lines):
    return ('Approval and recovery report\n\nExecutive summary\n' + summary
            + '\n\nFindings: action log\n' + '\n'.join(lines)
            + '\n\nConclusion\nApprovals are permissions; queued recovery is not completed work.'
            + '\n\nNext steps\nInspect recurring failures, verify repaired artifacts and acceptance checks, '
            'and continue recovery within the selected approval policy and remaining budget.'
            + '\n\nSources\nPersisted approval and recovery event records. Format reference: ' + SOURCE_URL)
