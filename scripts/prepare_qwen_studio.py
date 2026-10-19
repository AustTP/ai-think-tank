"""Validate exports with the actual Qwen3.5 tokenizer; never truncate examples."""
import copy
import json
import re
import sys
from pathlib import Path


def clean_context(messages):
    """Remove only consecutive identical system instructions; preserve the target."""
    cleaned = []
    removed = 0
    for index, message in enumerate(messages):
        if index < len(messages) - 1 and message.get('role') == 'system' and cleaned and message == cleaned[-1]:
            removed += 1
            continue
        cleaned.append(copy.deepcopy(message))
    return cleaned, removed


def prepare(folder, model='unsloth/Qwen3.5-4B', max_tokens=16384):
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(model, local_files_only=True)
    report = {'model': model, 'token_limit': max_tokens, 'examples': [], 'train': 0, 'eval': 0}
    maximum = 0
    manifest = [json.loads(line) for line in (folder / 'manifest.jsonl').read_text(encoding='utf-8').splitlines()]
    for split in ('train', 'eval'):
        output = []
        ids = [r['id'] for r in manifest if r['split'] == split]
        rows = [json.loads(line) for line in (folder / f'{split}.jsonl').read_text(encoding='utf-8').splitlines()]
        for index, row in enumerate(rows):
            entry = {'id': ids[index], 'split': split}
            try:
                row = copy.deepcopy(row)
                row['messages'], removed = clean_context(row['messages'])
                entry['duplicate_system_messages_removed'] = removed
                for message in row['messages']:
                    for call in message.get('tool_calls') or []:
                        function = call.get('function', call)
                        if isinstance(function.get('arguments'), str):
                            function['arguments'] = json.loads(function['arguments'])
                        if not isinstance(function.get('arguments'), dict):
                            raise ValueError('Tool arguments must be a JSON object')
                rendered = tokenizer.apply_chat_template(row['messages'], tools=row.get('tools'), tokenize=False, add_generation_prompt=False)
                # Studio conversation import can drop nested tool_calls/tools. Materialize
                # the model-native tool instructions, calls and results into content.
                blocks = re.findall(r'<\|im_start\|>(system|user|assistant)\n(.*?)<\|im_end\|>', rendered, re.S)
                if not blocks or blocks[-1][0] != 'assistant':
                    raise ValueError('No final assistant message in formatted conversation')
                messages = [{'role': role, 'content': content} for role, content in blocks]
                target = messages[-1]['content']
                if row['messages'][-1].get('tool_calls') and '<tool_call>' not in target:
                    raise ValueError('Final tool call disappeared during formatting')
                if not target.strip():
                    raise ValueError('Empty formatted assistant target')
                # This is the exact structured dataset Studio will format once.
                final = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)
                tokens = tokenizer.encode(final, add_special_tokens=False)
                marker = '<|im_start|>assistant\n'
                target_text = final.rsplit(marker, 1)[-1].split('<|im_end|>', 1)[0]
                target_tokens = len(tokenizer.encode(target_text, add_special_tokens=False))
                if marker not in final or target_tokens == 0:
                    raise ValueError('No trainable final assistant response')
                entry.update(original_tokens=len(tokens), response_tokens=target_tokens)
                if len(tokens) > max_tokens:
                    raise ValueError(f'Over token limit: {len(tokens)} > {max_tokens}')
                entry.update(status='ready', tokens=len(tokens), response_tokens=target_tokens)
                maximum = max(maximum, len(tokens))
                output.append({'messages': messages})
            except (ValueError, TypeError, KeyError) as error:
                entry.update(status='excluded', reason=str(error))
            report['examples'].append(entry)
        report[split] = len(output)
        (folder / f'{split}.studio.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in output), encoding='utf-8')
    report['max_tokens'] = maximum
    report['required_max_seq_length'] = max_tokens
    report['excluded'] = sum(r['status'] == 'excluded' for r in report['examples'])
    # Old proposals remain in dated archives; clear the obsolete latest file.
    (folder / 'compact-review.jsonl').write_text('', encoding='utf-8')
    report['settings'] = {'format': 'messages', 'train_on_completions': True,
                          'max_seq_length': report['required_max_seq_length']}
    report['note'] = 'Use train.studio.jsonl and eval.studio.jsonl with the model chat template. Context length must be at least required_max_seq_length. Actual Studio masking must still be checked in its preview/logs; response-token counts here verify template boundaries, not Studio labels.'
    (folder / 'studio-validation.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return {k: report[k] for k in ('model', 'train', 'eval', 'max_tokens', 'required_max_seq_length', 'excluded')}


if __name__ == '__main__':
    print(json.dumps(prepare(Path(sys.argv[1]), max_tokens=int(sys.argv[2]) if len(sys.argv) > 2 else 16384), ensure_ascii=False))
