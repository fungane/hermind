"""Privacy contracts at pre-compression egress and fallback, using synthetic input."""
import io
import json

import pytest


@pytest.fixture
def llm_stub(provider, monkeypatch):
    from hermind import llm

    sent = []

    def respond(request, timeout):
        prompt = json.loads(request.data)['messages'][0]['content']
        sent.append(prompt)
        answer = '{"ids": []}' if 'Candidates:' in prompt else '[]'
        return io.BytesIO(json.dumps({'choices': [{'message': {'content': answer}}]}).encode())

    monkeypatch.setattr(llm, '_send', respond)
    provider._llm = llm.LLMClient({'base_url': 'https://llm.invalid/v1', 'model': 'stub',
                                  'rerank': True, 'pii': 'off'})
    return sent


@pytest.mark.parametrize('closing', ['</private>', ''], ids=['closed', 'unclosed'])
def test_precompress_redacts_whole_messages_before_request_window(provider, llm_stub, closing):
    visible = 'We chose a blue glaze.'
    messages = [
        {'role': 'user', 'content': visible + ' <private>' + 'studio color swatch. ' * 350 + closing},
        {'role': 'assistant', 'content': 'The kiln is ready.'},
    ]
    provider.on_pre_compress(messages)
    assert len(llm_stub) == 1
    assert 'studio color swatch' not in llm_stub[0]
    assert visible in llm_stub[0] and 'The kiln is ready.' in llm_stub[0]


@pytest.mark.parametrize('use_llm', [False, True], ids=['offline', 'empty-extraction'])
def test_precompress_fallback_keeps_only_public_decisions(provider, request, use_llm):
    sent = request.getfixturevalue('llm_stub') if use_llm else []
    visible = 'We chose a blue glaze.'
    messages = [{'role': 'user', 'content': visible +
                 ' <private>We decided on lavender. We installed a trellis.</private>'}]
    block = provider.on_pre_compress(messages)
    assert len(sent) == int(use_llm)
    if use_llm:
        assert visible in sent[0] and 'lavender' not in sent[0] and 'trellis' not in sent[0]
    assert visible in block
    assert 'lavender' not in block and 'trellis' not in block
    saved = [r[0] for r in provider._store.conn.execute('SELECT content FROM notes')]
    assert saved == [visible]


def test_rerank_and_search_log_controls(provider, llm_stub):
    store = provider._store
    store.add('Glazed ceramics drying instructions', kind='decision')
    store.add('Glazed ceramics firing schedule', kind='decision')
    query = 'glazed ceramics <private>lavender</private>'
    result = json.loads(provider.handle_tool_call('memory_search', {'query': query}))
    assert result['count'] == 2
    assert len(llm_stub) == 1 and 'glazed ceramics' in llm_stub[0] and 'lavender' not in llm_stub[0]
    rows = store.conn.execute("SELECT kind, query FROM usage_log WHERE kind IN ('search','rerank')").fetchall()
    assert {kind for kind, _ in rows} == {'search', 'rerank'}
    assert all(q == '' for _, q in rows)
