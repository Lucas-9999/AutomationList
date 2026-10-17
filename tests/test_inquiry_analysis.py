"""SYNTHETIC resumable-analysis fixtures; no external model calls."""
from copy import deepcopy
import hashlib

import pytest

import tradeflow.inquiry_analysis as analysis
from tradeflow.store import Store


BODY = 'SYNTHETIC A 10 PCS\nSYNTHETIC B 20 PCS\nSYNTHETIC A 10 PCS\n'


def synthetic_plan(source):
    body, chunks, offset = source['body'], [], 0
    digest = hashlib.sha256(body.encode()).hexdigest()
    for index, text in enumerate(body.splitlines(keepends=True), 1):
        chunks.append({'id': f'SYNTHETIC-chunk-{digest}-{index}', 'index': index,
                       'start': offset, 'end': offset + len(text), 'text': text,
                       'context_before': body[max(0, offset - 10):offset],
                       'context_after': body[offset + len(text):offset + len(text) + 10],
                       'characters': len(text)})
        offset += len(text)
    return {'plan_id': 'SYNTHETIC-plan-' + digest, 'version': 'SYNTHETIC-v1',
            'body_sha256': digest, 'chunk_count': len(chunks), 'chunks': chunks}


class SyntheticClient:
    configuration_fingerprint = 'SYNTHETIC-configuration-A'

    def __init__(self, *, fail_index=None, result_override=None):
        self.calls = []
        self.fail_index = fail_index
        self.result_override = result_override

    def analyze_inquiry(self, source):
        self.calls.append(deepcopy(source))
        if source['chunk_context']['index'] == self.fail_index:
            raise ValueError('SYNTHETIC model failure')
        if self.result_override:
            return self.result_override(source)
        quote = source['body'].strip()
        return {'technical_chinese': '合成中文：' + quote,
                'requirements': [{'field': 'quantity', 'original_quote': quote,
                                  'chinese': '合成数量', 'value': '20' if 'B 20' in quote else '10'}],
                'questions': ['合成待确认'], 'warnings': ['合成警示']}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(analysis, 'build_inquiry_plan', synthetic_plan)
    store = Store(tmp_path / 'SYNTHETIC-store')
    source = {'subject': 'SYNTHETIC Inquiry', 'body': BODY,
              'sha256': 'SYNTHETIC-original-file-hash', 'sender': '', 'message_id': '',
              'attachments': [{'filename': 'SYNTHETIC.png', 'analysis_status': 'not_analyzed'}],
              'warnings': ['SYNTHETIC 原件尚有图片']}
    return store, store.create_record('inquiry', 'SYNTHETIC-inquiry', source)


def test_sequential_complete_analysis_preserves_exact_body_and_chunk_context(environment):
    store, inquiry = environment
    client = SyntheticClient()
    record = analysis.analyze_inquiry_all(store, inquiry, client)
    assert [request['chunk_context']['index'] for request in client.calls] == [1, 2, 3]
    assert ''.join(request['body'] for request in client.calls) == BODY
    assert all(request['sha256'] == inquiry['payload']['sha256'] for request in client.calls)
    assert all(request['attachments'] == inquiry['payload']['attachments'] for request in client.calls)
    assert all(request['warnings'] == inquiry['payload']['warnings'] for request in client.calls)
    assert all(request['chunk_context']['count'] == 3 for request in client.calls)
    assert record['kind'] == 'analysis'
    assert record['source_key'] == f"{inquiry['id']}:{client.configuration_fingerprint}:{record['payload']['plan_id']}"
    assert len(store.list_records('analysis_chunk')) == 3
    assert len(record['payload']['analysis_chunk_ids']) == 3
    assert record['payload']['result']['technical_chinese'] == '\n\n'.join('合成中文：' + line for line in BODY.splitlines())
    assert len(store.list_records('analysis')) == 1


def test_failure_only_saves_successful_chunks_retry_skips_them_and_only_then_publishes(environment):
    store, inquiry = environment
    client = SyntheticClient(fail_index=2)
    with pytest.raises(ValueError, match='SYNTHETIC model failure'):
        analysis.analyze_inquiry_all(store, inquiry, client)
    assert len(store.list_records('analysis_chunk')) == 1
    assert not store.list_records('analysis')
    plan = synthetic_plan(inquiry['payload'])
    assert analysis.get_cached_chunk_ids(store, inquiry, client, plan) == [plan['chunks'][0]['id']]
    client.fail_index = None
    final = analysis.analyze_inquiry_all(store, inquiry, client)
    assert [request['chunk_context']['index'] for request in client.calls] == [1, 2, 2, 3]
    assert final['kind'] == 'analysis'
    assert len(store.list_records('analysis')) == 1
    assert analysis.get_cached_chunk_ids(store, inquiry, client, plan) == [chunk['id'] for chunk in plan['chunks']]


def test_single_chunk_is_immutable_idempotent_and_can_complete_before_first(environment):
    store, inquiry = environment
    client = SyntheticClient()
    plan = synthetic_plan(inquiry['payload'])
    second = plan['chunks'][1]
    first_record = analysis.analyze_inquiry_chunk(store, inquiry, client, second['id'], plan_id=plan['plan_id'])
    retry = analysis.analyze_inquiry_chunk(store, inquiry, client, second['id'], plan_id=plan['plan_id'])
    assert retry == first_record
    assert len(client.calls) == 1
    assert not store.list_records('analysis')
    analysis.analyze_inquiry_all(store, inquiry, client)
    assert [request['chunk_context']['index'] for request in client.calls] == [2, 1, 3]


def test_complete_result_is_reused_without_new_model_calls(environment):
    store, inquiry = environment
    client = SyntheticClient()
    first = analysis.analyze_inquiry_all(store, inquiry, client)
    assert analysis.analyze_inquiry_all(store, inquiry, client) == first
    assert len(client.calls) == 3


def test_configuration_fingerprints_and_inquiry_records_do_not_share_cache(environment):
    store, inquiry = environment
    first = SyntheticClient()
    analysis.analyze_inquiry_all(store, inquiry, first)
    second = SyntheticClient()
    second.configuration_fingerprint = 'SYNTHETIC-configuration-B'
    analysis.analyze_inquiry_all(store, inquiry, second)
    assert len(second.calls) == 3
    other = store.create_record('inquiry', 'SYNTHETIC-other-inquiry', inquiry['payload'])
    third = SyntheticClient()
    analysis.analyze_inquiry_all(store, other, third)
    assert len(third.calls) == 3
    assert len(store.list_records('analysis')) == 3
    assert len(store.list_records('analysis_chunk')) == 9


def test_full_four_field_exact_deduplication_and_all_supporting_sources(environment):
    store, inquiry = environment
    record = analysis.analyze_inquiry_all(store, inquiry, SyntheticClient())
    payload = record['payload']
    requirements = payload['result']['requirements']
    assert [item['value'] for item in requirements] == ['10', '20']
    assert payload['result']['questions'] == ['合成待确认']
    assert payload['result']['warnings'].count('合成警示') == 1
    assert 'SYNTHETIC 原件尚有图片' in payload['result']['warnings']
    assert any('附件' in warning for warning in payload['result']['warnings'])
    support_a = payload['requirement_sources'][0]
    assert support_a['requirement_index'] == 0
    assert support_a['requirement'] == requirements[0]
    assert [source['index'] for source in support_a['sources']] == [1, 3]
    for item in payload['requirement_sources']:
        for source in item['sources']:
            assert source['chunk_id']
            for position in source['quote_occurrences']:
                assert BODY[position['start']:position['end']] == item['requirement']['original_quote']


def test_different_chinese_or_field_is_not_silently_merged(environment):
    store, inquiry = environment
    ordinary = SyntheticClient()
    def result(source):
        item = ordinary.analyze_inquiry(source)
        if source['chunk_context']['index'] == 3:
            item['requirements'][0]['chinese'] = '另一条合成说明'
        return item
    record = analysis.analyze_inquiry_all(store, inquiry, SyntheticClient(result_override=result))
    assert len(record['payload']['result']['requirements']) == 3


def test_mock_cannot_reference_neighbor_context_or_other_chunk(environment):
    store, inquiry = environment
    def cross_reference(source):
        return {'technical_chinese': '合成说明', 'requirements': [
            {'field': 'quantity', 'original_quote': 'SYNTHETIC B 20 PCS', 'chinese': '合成数量', 'value': '20'}],
            'questions': [], 'warnings': []}
    with pytest.raises(ValueError, match='原文引用不在邮件正文'):
        analysis.analyze_inquiry_chunk(store, inquiry, SyntheticClient(result_override=cross_reference),
                                      synthetic_plan(inquiry['payload'])['chunks'][0]['id'])
    assert not store.list_records('analysis_chunk')
    assert not store.list_records('analysis')


def test_mock_cannot_bypass_schema_or_value_quote_validation(environment):
    store, inquiry = environment
    def incorrect_value(source):
        return {'technical_chinese': '合成说明', 'requirements': [
            {'field': 'quantity', 'original_quote': source['body'].strip(), 'chinese': '合成数量', 'value': '999999'}],
            'questions': [], 'warnings': []}
    with pytest.raises(ValueError, match='value 不在其原文引用'):
        analysis.analyze_inquiry_all(store, inquiry, SyntheticClient(result_override=incorrect_value))
    assert not store.list_records('analysis_chunk')


def test_changed_plan_or_unknown_chunk_rejected_without_call(environment):
    store, inquiry = environment
    client = SyntheticClient()
    plan = synthetic_plan(inquiry['payload'])
    with pytest.raises(ValueError, match='计划已变化'):
        analysis.analyze_inquiry_chunk(store, inquiry, client, plan['chunks'][0]['id'], plan_id='SYNTHETIC-outdated')
    with pytest.raises(ValueError, match='分块不存在'):
        analysis.analyze_inquiry_chunk(store, inquiry, client, 'SYNTHETIC-nonexistent')
    with pytest.raises(ValueError, match='计划已变化'):
        analysis.get_cached_chunk_ids(store, inquiry, client, {**plan, 'plan_id': 'SYNTHETIC-outdated'})
    assert not client.calls


def test_old_monolithic_analysis_cache_is_not_reused(environment):
    store, inquiry = environment
    client = SyntheticClient()
    store.create_record('analysis', f"{inquiry['id']}:{client.configuration_fingerprint}",
                        {'inquiry_id': inquiry['id'], 'result': {'technical_chinese': 'SYNTHETIC old cache'}})
    record = analysis.analyze_inquiry_all(store, inquiry, client)
    assert len(client.calls) == 3
    assert record['payload']['plan_id']


def test_single_chunk_technical_chinese_is_not_rewritten(environment):
    store, inquiry = environment
    source = {**inquiry['payload'], 'body': 'SYNTHETIC A 10 PCS'}
    single = store.create_record('inquiry', 'SYNTHETIC-one-chunk', source)
    result = analysis.analyze_inquiry_all(store, single, SyntheticClient())
    assert result['payload']['result']['technical_chinese'] == '合成中文：SYNTHETIC A 10 PCS'


def test_unverified_source_is_blocked_even_with_mock_client(environment):
    store, inquiry = environment
    source = {**inquiry['payload'], 'source_type': 'inquiry_document',
              'source': {'text_analysis_available': False, 'analysis_blockers': ['SYNTHETIC-field']}}
    blocked = store.create_record('inquiry', 'SYNTHETIC-unverified', source)
    client = SyntheticClient()
    with pytest.raises(ValueError, match='未核验'):
        analysis.analyze_inquiry_all(store, blocked, client)
    assert not client.calls


def test_concurrent_chunk_winner_is_validated_and_reused_without_overwrite(environment, monkeypatch):
    store, inquiry = environment
    original_create = store.create_record
    winners = []
    def racing_create(kind, key, payload):
        if kind == 'analysis_chunk':
            winner_payload = deepcopy(payload)
            winner_payload['result']['technical_chinese'] = '先完成请求的合成中文'
            winners.append(original_create(kind, key, winner_payload))
            # Real Store raises ConflictError when this second, different
            # response attempts the now occupied immutable identity.
        return original_create(kind, key, payload)
    monkeypatch.setattr(store, 'create_record', racing_create)
    plan = synthetic_plan(inquiry['payload'])
    record = analysis.analyze_inquiry_chunk(store, inquiry, SyntheticClient(), plan['chunks'][0]['id'])
    assert record == winners[0]
    assert record['payload']['result']['technical_chinese'] == '先完成请求的合成中文'
    assert len(store.list_records('analysis_chunk')) == 1


def test_bad_cached_chunk_cannot_bypass_evidence_validation(environment):
    store, inquiry = environment
    client = SyntheticClient()
    plan = synthetic_plan(inquiry['payload'])
    chunk = plan['chunks'][0]
    key = f"{inquiry['id']}:{client.configuration_fingerprint}:{plan['plan_id']}:{chunk['id']}"
    store.create_record('analysis_chunk', key, {
        'inquiry_id': inquiry['id'], 'plan_id': plan['plan_id'],
        'configuration_fingerprint': client.configuration_fingerprint,
        'chunk': chunk, 'result': {'technical_chinese': '合成无效缓存',
            'requirements': [{'field': 'quantity', 'original_quote': 'NOT IN SYNTHETIC CHUNK',
                              'chinese': '合成缓存', 'value': 'NOT'}], 'questions': [], 'warnings': []}})
    with pytest.raises(ValueError, match='原文引用不在邮件正文'):
        analysis.analyze_inquiry_all(store, inquiry, client)
    assert not client.calls
    assert not store.list_records('analysis')


def test_client_private_state_is_not_serialized(environment):
    store, inquiry = environment
    client = SyntheticClient()
    client._api_key = 'sk-' + 'SYNTHETIC-DO-NOT-PERSIST'
    client.private_url = 'https://example.invalid/SYNTHETIC-private'
    analysis.analyze_inquiry_all(store, inquiry, client)
    serialized = str(store.list_records('analysis_chunk')) + str(store.list_records('analysis'))
    assert client._api_key not in serialized
    assert client.private_url not in serialized


def test_real_planner_integration_covers_long_inquiry_without_summary_model_call(environment, monkeypatch):
    from tradeflow.inquiry_chunks import build_inquiry_plan
    monkeypatch.setattr(analysis, 'build_inquiry_plan', build_inquiry_plan)
    store, inquiry = environment
    body = 'SYNTHETIC A 10 PCS\n' * 130
    source = {**inquiry['payload'], 'body': body}
    long_inquiry = store.create_record('inquiry', 'SYNTHETIC-real-plan-long-body', source)
    plan = build_inquiry_plan(source)
    client = SyntheticClient()
    record = analysis.analyze_inquiry_all(store, long_inquiry, client)
    assert plan['chunk_count'] > 1
    assert len(client.calls) == plan['chunk_count']
    assert ''.join(call['body'] for call in client.calls) == body
    assert all(call['body'] == body[call['chunk_context']['start']:call['chunk_context']['end']]
               for call in client.calls)
    assert record['payload']['plan_id'] == plan['plan_id']
    assert len(record['payload']['analysis_chunk_ids']) == plan['chunk_count']
