"""Shared instructions for evidence-backed agent deliverables."""

INSTRUCTIONS = (
    'EVIDENCE POLICY: Never include estimates, guesses, projections, assumed values, '
    'or unsupported judgments in your work. Report only facts supported by retrieved '
    'sources, inspected artifacts, or reproducible calculations using verified inputs. '
    'Cite the evidence and calculation method. If evidence is unavailable, say unknown '
    'or not verified; leave the value empty and explain the missing evidence. Never '
    'replace missing evidence with general knowledge or label an estimate verified. '
    'For tables and CSVs use basis=verified for supported values and basis=unknown '
    'for missing evidence. Verify every substantive finding, not merely a sample. '
    'Reviewers must reject estimated or unsupported findings. Synthetic test fixtures '
    'may be used only as explicitly labeled test inputs, never as market observations '
    'or research findings.'
    '\n\nPROJECT EMAIL DELIVERY: Reuse the existing player notification path for '
    'report delivery and agent communications. Content executors return notifyPlayer '
    'with kind, subject and body; sim applies that result via _queue_player_email, '
    'and _drain_email_outbox_sync delivers through notify.send_player_email_sync '
    'using existing configured credentials and recipient. Follow the same path used '
    'for blocked commands and finished spikes. Inspect world/notify.py and world/sim.py '
    'as implementation evidence. Do not research or introduce another email provider, '
    'create a separate SMTP connection, request replacement credentials, or change '
    'recipients for this integration. Queue reports through the existing outbox; '
    'do not claim an email was sent unless delivery evidence confirms it.'
)

EMAIL_INSTRUCTIONS = 'PROJECT EMAIL DELIVERY: ' + INSTRUCTIONS.split('PROJECT EMAIL DELIVERY: ', 1)[1]


def apply(messages):
    result = [dict(message) for message in messages]
    if any(INSTRUCTIONS in str(m.get('content', '')) for m in result if m.get('role') == 'system'):
        return result
    index = next((i for i, m in enumerate(result) if m.get('role') == 'system' and isinstance(m.get('content'), str)), None)
    if index is None:
        result.insert(0, {'role': 'system', 'content': INSTRUCTIONS})
    else:
        result[index]['content'] += '\n\n' + INSTRUCTIONS
    return result
