import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def test_tool_calls_and_token_report(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location('prepare_qwen', Path(__file__).with_name('prepare_qwen_studio.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class Tokenizer:
        def apply_chat_template(self, messages, **kwargs):
            blocks = []
            for message in messages:
                content = message.get('content') or ''
                for call in message.get('tool_calls', []):
                    assert isinstance(call['function']['arguments'], dict)
                    content += '<tool_call>' + json.dumps(call['function']) + '</tool_call>'
                blocks.append('<|im_start|>' + message['role'] + '\n' + content + '<|im_end|>')
            return ''.join(blocks)
        def encode(self, text, **kwargs):
            return list(text)
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=lambda *a, **k: Tokenizer())))
    row = {'messages': [{'role': 'user', 'content': 'Use a tool'}, {'role': 'assistant', 'tool_calls': [{'function': {'name': 'lookup', 'arguments': '{"query":"evidence"}'}}]}]}
    (tmp_path / 'train.jsonl').write_text(json.dumps(row) + '\n')
    (tmp_path / 'eval.jsonl').write_text('')
    (tmp_path / 'manifest.jsonl').write_text(json.dumps({'id': 'one', 'split': 'train'}) + '\n')
    result = module.prepare(tmp_path)
    ready = json.loads((tmp_path / 'train.studio.jsonl').read_text())
    assert '<tool_call>' in ready['messages'][-1]['content']
    assert 'evidence' in ready['messages'][-1]['content']
    assert result['required_max_seq_length'] >= result['max_tokens'] > 0
    row['messages'].insert(0, {'role': 'system', 'content': 'Long instructions ' * 1000})
    (tmp_path / 'train.jsonl').write_text(json.dumps(row) + '\n')
    limited = module.prepare(tmp_path, max_tokens=4096)
    assert limited['train'] == 0
    assert limited['excluded'] == 1
    assert (tmp_path / 'compact-review.jsonl').read_text() == ''
    row['messages'][-1]['tool_calls'][0]['function']['arguments'] = 'invalid json'
    (tmp_path / 'train.jsonl').write_text(json.dumps(row) + '\n')
    assert module.prepare(tmp_path)['train'] == 0
    assert json.loads((tmp_path / 'studio-validation.json').read_text())['examples'][0]['status'] == 'excluded'


def test_cleanup_preserves_evidence_and_target():
    spec = importlib.util.spec_from_file_location('prepare_qwen', Path(__file__).with_name('prepare_qwen_studio.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    system = {'role': 'system', 'content': 'Follow the instructions'}
    evidence = {'role': 'tool', 'content': 'Evidence'}
    target = {'role': 'assistant', 'content': 'Complete answer'}
    cleaned, removed = module.clean_context([system, system, evidence, evidence, target])
    assert removed == 1
    assert cleaned == [system, evidence, evidence, target]
