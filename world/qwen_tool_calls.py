"""Normalize Qwen textual function calls left unparsed by a local server."""
import json
import math
import re
import uuid

CALL = re.compile(r'<tool_call>\s*<function=([A-Za-z_][\w.-]*)>(.*?)</function>\s*</tool_call>', re.S)
PARAMETER = re.compile(r'<parameter=([A-Za-z_][\w.-]*)>(.*?)</parameter>', re.S)


def validate(value, schema):
    kind = schema.get('type')
    valid = {'string': isinstance(value, str), 'integer': isinstance(value, int) and not isinstance(value, bool),
             'number': isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
             'boolean': isinstance(value, bool), 'object': isinstance(value, dict),
             'array': isinstance(value, list), 'null': value is None}
    if kind in valid and not valid[kind]:
        raise ValueError('Local tool argument has the wrong type')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError('Local tool argument is outside its allowed values')
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        if any(key not in value for key in schema.get('required', [])):
            raise ValueError('Local tool request is missing a required argument')
        if schema.get('additionalProperties') is False and set(value) - set(properties):
            raise ValueError('Local tool request contains an unknown argument')
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key])
    if isinstance(value, list) and 'items' in schema:
        for item in value:
            validate(item, schema['items'])


def normalize(result, tools):
    if not tools:
        return result
    definitions = {t.get('function', {}).get('name'): t.get('function', {}) for t in tools if t.get('type') == 'function'}
    for choice in result.get('choices', []):
        message = choice.get('message') or {}
        text = message.get('content') or ''
        if message.get('tool_calls') or not isinstance(text, str) or '<tool_call>' not in text:
            continue
        matches = list(CALL.finditer(text))
        if not matches or len(matches) != text.count('<tool_call>'):
            raise ValueError('Local model returned an incomplete or malformed textual tool call')
        calls = []
        for match in matches:
            name, body = match.groups()
            if name not in definitions:
                raise ValueError('Local model requested a tool that was not supplied')
            schema = definitions[name].get('parameters', {})
            properties = schema.get('properties', {})
            parameters = list(PARAMETER.finditer(body))
            if PARAMETER.sub('', body).strip():
                raise ValueError('Local model returned malformed tool parameters')
            arguments = {}
            for parameter in parameters:
                key, raw = parameter.groups()
                if key in arguments or key not in properties:
                    raise ValueError('Local tool request has duplicate or unknown parameters')
                raw = raw.strip()
                if properties[key].get('type') == 'string':
                    value = raw
                else:
                    try:
                        value = json.loads(raw)
                    except ValueError as error:
                        raise ValueError('Local tool argument is not valid JSON') from error
                arguments[key] = value
            validate(arguments, schema)
            calls.append({'id': 'call_' + uuid.uuid4().hex, 'type': 'function',
                          'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}})
        choice['_raw_tool_call_content'] = text
        message['content'] = CALL.sub('', text).strip() or None
        message['tool_calls'] = calls
        choice['finish_reason'] = 'tool_calls'
    return result
