"""SYNTHETIC failures only: no secrets, customer content or external API calls."""
import json

import pytest

from tradeflow.analysis_errors import safe_analysis_failure


@pytest.mark.parametrize('message,code,retryable', [
    ('Agent 响应截断或未正常结束；结果不接受', 'agent_output_incomplete', False),
    ('Agent 原文引用不在邮件正文中；结果不接受', 'agent_evidence_mismatch', False),
    ('Agent requirement value 不在其原文引用中；结果不接受', 'agent_evidence_mismatch', False),
    ('Agent 网络请求失败或超时；未产生翻译结果', 'agent_network_failed', True),
    ('Agent 未配置：需要 TRADEFLOW_AGENT_BASE_URL、TRADEFLOW_AGENT_API_KEY、TRADEFLOW_AGENT_MODEL', 'agent_not_configured', False),
    ('Agent RESPONSE_FORMAT 仅支持 json_schema 或 json_object', 'agent_invalid_configuration', False),
    ('Agent THINKING 仅支持留空、enabled 或 disabled', 'agent_invalid_configuration', False),
    ('Agent MAX_TOKENS 必须为1至32768的整数或留空', 'agent_invalid_configuration', False),
    ('Agent BASE_URL 必须为有效服务地址，不能包含凭据或查询参数', 'agent_invalid_configuration', False),
    ('远程 Agent API 必须使用 HTTPS', 'agent_invalid_configuration', False),
    ('Agent timeout 必须在 0 至 120 秒之间', 'agent_invalid_configuration', False),
    ('Agent 返回无效 JSON；结果不接受', 'agent_invalid_json', False),
    ('Agent JSON 含重复字段；结果不接受', 'agent_invalid_json', False),
    ('Agent 未返回 JSON 文本', 'agent_invalid_json', False),
    ('Agent 拒绝分析；需要人工处理', 'agent_refused', False),
    ('Agent JSON 字段与约定不一致', 'agent_invalid_result', False),
    ('Agent 未返回有效技术中文', 'agent_invalid_result', False),
    ('Agent questions/warnings 必须为非空字符串数组', 'agent_invalid_result', False),
    ('Agent requirements 必须为数组', 'agent_invalid_result', False),
    ('Agent requirement 字段与约定不一致', 'agent_invalid_result', False),
    ('Agent requirement 必须包含非空字符串', 'agent_invalid_result', False),
    ('Agent requirement 缺失中文说明', 'agent_invalid_result', False),
    ('Agent 请求或响应解析失败；未产生翻译结果', 'agent_invalid_response', False),
    ('Agent 返回错误；请检查服务及响应格式配置', 'agent_invalid_response', False),
    ('Agent 响应 choices 格式无效', 'agent_invalid_response', False),
    ('Agent 响应 message 格式无效', 'agent_invalid_response', False),
    ('Agent 响应格式无效；结果不接受', 'agent_invalid_response', False),
    ('Agent 返回内容超过大小上限', 'agent_response_too_large', False),
    ('缺失可分析邮件正文', 'inquiry_body_missing', False),
    ('询价 Word 含未核验内容或没有可分析原文；请先核对域、修订、图片等来源提示后再分析', 'inquiry_source_unverified', False),
    ('询价工作簿没有可分析的单元格原文，请先处理图片或嵌入对象', 'inquiry_source_unverified', False),
    ('询价工作簿含未核验公式或 Excel 错误值；请核对并提供明确原值后再分析，缓存值不能作为技术事实', 'inquiry_source_unverified', False),
])
def test_known_static_failures_use_safe_distinct_categories(message, code, retryable):
    result = safe_analysis_failure(ValueError(message))
    assert set(result) == {'code', 'message', 'retryable'}
    assert result['code'] == code
    assert result['retryable'] is retryable
    assert any('\u3400' <= char <= '\u9fff' for char in result['message'])


@pytest.mark.parametrize('status,code,retryable', [
    (400, 'agent_http_error', False), (401, 'agent_authentication_failed', False),
    (403, 'agent_authentication_failed', False), (404, 'agent_http_error', False),
    (408, 'agent_http_timeout', True), (422, 'agent_http_error', False),
    (429, 'agent_rate_limited', True), (500, 'agent_service_unavailable', True),
    (502, 'agent_service_unavailable', True), (503, 'agent_service_unavailable', True),
    (504, 'agent_http_timeout', True),
])
def test_numeric_http_status_uses_fixed_safe_categories(status, code, retryable):
    result = safe_analysis_failure(ValueError(f'Agent HTTP 请求失败 ({status})；请检查地址、模型及响应格式配置'))
    assert result['code'] == code
    assert result['retryable'] is retryable
    assert str(status) not in result['message']


@pytest.mark.parametrize('status', [301, 302, 307, 308])
def test_redirects_require_direct_configuration_and_are_not_automatically_retried(status):
    result = safe_analysis_failure(ValueError(f'Agent HTTP 重定向被拒绝 ({status})；请配置直接服务地址，密钥未发送至重定向地址'))
    assert result['code'] == 'agent_redirect_rejected'
    assert result['retryable'] is False


@pytest.mark.parametrize('message', [
    'SYNTHETIC unknown sk-SYNTHETIC-SECRET https://example.invalid/private 客户SYNTHETIC报价文本',
    'Agent 未返回 JSON 文本 sk-SYNTHETIC-SECRET',
    'Agent 原文引用不在邮件正文中；结果不接受\nhttps://example.invalid/private',
    'Agent HTTP 请求失败 (401)；请检查地址、模型及响应格式配置 sk-SYNTHETIC-SECRET',
    'Agent HTTP 请求失败 (999)；请检查地址、模型及响应格式配置',
    'Agent HTTP 请求失败 (401)；请检查地址、模型及响应格式配置\n',
    'sk-SYNTHETIC-SECRET Agent 响应截断或未正常结束；结果不接受',
    '',
])
def test_unknown_or_appended_sensitive_data_is_never_echoed(message):
    result = safe_analysis_failure(ValueError(message))
    serialized = json.dumps(result, ensure_ascii=False)
    assert result['code'] == 'analysis_failed'
    assert result['retryable'] is False
    for sensitive in ['sk-SYNTHETIC-SECRET', 'https://', 'example.invalid', '客户SYNTHETIC报价文本']:
        assert sensitive not in serialized


def test_each_result_is_fresh_and_cannot_mutate_other_public_errors():
    first = safe_analysis_failure(ValueError('Agent 响应截断或未正常结束；结果不接受'))
    first['message'] = 'SYNTHETIC mutated'
    second = safe_analysis_failure(ValueError('Agent 响应截断或未正常结束；结果不接受'))
    assert second['message'] != 'SYNTHETIC mutated'
    assert '未保存结果' in second['message']


def test_rejected_evidence_and_incomplete_output_do_not_suggest_accepting_partial_results():
    incomplete = safe_analysis_failure(ValueError('Agent 响应截断或未正常结束；结果不接受'))
    mismatched = safe_analysis_failure(ValueError('Agent requirement value 不在其原文引用中；结果不接受'))
    assert '未保存结果' in incomplete['message']
    assert '未保存结果' in mismatched['message']
    assert incomplete['code'] != 'agent_not_configured'
    assert mismatched['code'] != 'agent_not_configured'
