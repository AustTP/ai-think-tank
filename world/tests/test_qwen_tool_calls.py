import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qwen_tool_calls as qwen

TOOLS = [{'type':'function','function':{'name':'search_library','parameters':{
    'type':'object','properties':{'query':{'type':'string'}},'required':['query']}}}]
BEN_REPLY = '<tool_call>\n<function=search_library>\n<parameter=query>\ncoinbase historical data API comparison coverage frequency costs\n</parameter>\n</function>\n</tool_call>'


def response(text):
    return {'choices':[{'message':{'role':'assistant','content':text,'tool_calls':None},'finish_reason':'stop'}]}


def test_bens_actual_format_becomes_executable_tool_call():
    result = qwen.normalize(response(BEN_REPLY), TOOLS)
    message = result['choices'][0]['message']
    assert message['content'] is None
    assert result['choices'][0]['_raw_tool_call_content'] == BEN_REPLY
    call = message['tool_calls'][0]
    assert call['function']['name']=='search_library'
    assert json.loads(call['function']['arguments'])['query']=='coinbase historical data API comparison coverage frequency costs'
    assert result['choices'][0]['finish_reason']=='tool_calls'


@pytest.mark.parametrize('text', [BEN_REPLY.replace('search_library','execute_anything'),
    BEN_REPLY.replace('<parameter=query>','<parameter=password>'),
    BEN_REPLY.replace('</tool_call>',''), '<tool_call><function=search_library></function></tool_call>'])
def test_invalid_calls_are_rejected(text):
    with pytest.raises(ValueError):
        qwen.normalize(response(text), TOOLS)


def test_plain_text_and_native_calls_are_unchanged():
    plain = response('Here is the report.')
    assert qwen.normalize(plain, TOOLS)==plain
    native = response(BEN_REPLY)
    native['choices'][0]['message']['tool_calls']=[{'id':'native'}]
    assert qwen.normalize(native, TOOLS)['choices'][0]['message']['tool_calls']==[{'id':'native'}]
    assert qwen.normalize(response(BEN_REPLY), [])['choices'][0]['message']['content']==BEN_REPLY


def test_typed_arguments_and_multiple_calls():
    tools=[{'type':'function','function':{'name':'sample','parameters':{'type':'object',
        'properties':{'count':{'type':'integer'},'options':{'type':'object'}},'required':['count']}}}]
    text='<tool_call><function=sample><parameter=count>3</parameter><parameter=options>{"daily":true}</parameter></function></tool_call>'
    calls=qwen.normalize(response(text+text),tools)['choices'][0]['message']['tool_calls']
    assert len(calls)==2 and calls[0]['id']!=calls[1]['id']
    assert json.loads(calls[0]['function']['arguments'])=={'count':3,'options':{'daily':True}}
