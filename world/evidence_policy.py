"""Shared instructions for evidence-backed research and review replies."""

EVIDENCE_INSTRUCTIONS = (
    'EVIDENCE POLICY: Never include estimates, guesses, projections, assumed values, '
    'or unsupported judgments in your work. Report only facts supported by retrieved '
    'sources, inspected artifacts, or reproducible calculations using verified inputs. '
    'Cite the evidence and calculation method. If evidence is unavailable, say unknown '
    'or not verified; leave the value empty and explain the missing evidence. Never '
    'replace missing evidence with general knowledge or label an estimate verified. '
    'For tables and CSVs use basis=verified for supported values and basis=unknown '
    'for missing evidence. Verify every substantive finding, not merely a sample. '
    'Reviewers must reject estimated or unsupported findings. Synthetic test fixtures '
    'may be used only as explicitly labeled test inputs, never as research findings.'
)

SOURCE_URL_INSTRUCTIONS = (
    'SOURCE URL POLICY: Use exact URLs supplied in the task, retrieved Library '
    'evidence, search results or real page links. Never invent URL paths. Open each '
    'substantive citation and verify the final page title and relevant content before '
    'marking its claim verified. HTTP success alone is insufficient. Redirects to '
    'homepages, bot challenges, access blocks and rate limits do not verify a claim. '
    'Search for a relevant accessible source if retrieval fails; preserve unknown '
    'status until evidence is obtained. Never silently substitute a homepage.'
)

INSTRUCTIONS = EVIDENCE_INSTRUCTIONS + '\n\n' + SOURCE_URL_INSTRUCTIONS


def apply(messages):
    """Append the policy once to the first system message, preserving roles."""
    result = [dict(message) for message in messages]
    if any(INSTRUCTIONS in str(m.get('content', '')) for m in result
           if m.get('role') == 'system'):
        return result
    index = next((i for i, m in enumerate(result)
                  if m.get('role') == 'system' and isinstance(m.get('content'), str)), None)
    if index is None:
        result.insert(0, {'role': 'system', 'content': INSTRUCTIONS})
    else:
        result[index]['content'] += '\n\n' + INSTRUCTIONS
    return result
