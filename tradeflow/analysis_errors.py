"""Allowlisted public analysis failures; never echo an exception or source data."""
from __future__ import annotations

import re


_PUBLIC_FAILURES = {
    "analysis_failed": ("询价分析失败，未保存分析结果。请检查服务状态或联系管理员。", False),
    "agent_not_configured": ("尚未配置分析服务，请先在本机配置服务地址、密钥和模型。", False),
    "agent_invalid_configuration": ("分析服务配置无效，请检查服务地址、响应格式、输出长度和超时设置。", False),
    "agent_output_incomplete": ("分析输出被截断或未正常结束，未保存结果。请调高输出长度或拆分询价后重试。", False),
    "agent_evidence_mismatch": ("分析结果的引用或字段值与询价原文不一致，未保存结果。请核对原文后重新分析。", False),
    "agent_network_failed": ("分析服务连接失败或超时，未产生翻译结果。请检查网络和服务状态后重试。", True),
    "agent_invalid_json": ("分析服务未返回有效的 JSON 数据，未保存结果。请检查服务的响应格式配置。", False),
    "agent_refused": ("分析服务拒绝处理本次询价，未产生翻译结果。请人工核对询价内容。", False),
    "agent_invalid_result": ("分析结果未通过技术中文或数据结构校验，未保存结果。请检查模型和响应格式配置。", False),
    "agent_invalid_response": ("分析服务响应无效或无法解析，未保存结果。请检查服务和响应格式配置。", False),
    "agent_response_too_large": ("分析服务返回内容超过大小上限，未保存结果。请拆分询价或调整服务输出设置。", False),
    "agent_redirect_rejected": ("分析服务地址发生重定向，请配置直接服务地址后重试。", False),
    "agent_authentication_failed": ("分析服务拒绝授权，请检查本机密钥及模型访问权限。", False),
    "agent_rate_limited": ("分析服务请求受限，请稍后重试并检查服务额度。", True),
    "agent_service_unavailable": ("分析服务暂时不可用，未产生翻译结果。请稍后重试。", True),
    "agent_http_timeout": ("分析服务请求超时，未产生翻译结果。请稍后重试。", True),
    "agent_http_error": ("分析服务请求失败，未产生翻译结果。请检查服务地址、模型和响应格式配置。", False),
    "inquiry_source_unverified": ("询价来源含未核验内容或没有可分析原文，请先核对导入警示、公式、修订及图片。", False),
    "inquiry_body_missing": ("没有可分析的询价正文，请先导入含文字的询价文件。", False),
    "agent_translation_guard_failed": ("商品译文的规格、术语或否定含义未通过校验，本块未保存。请重试或核对技术原文。", False),
    "agent_source_row_mismatch": ("商品译文与原表格的行、料号或数量归属不一致，本块未保存。请核对来源后重试。", False),
}

_KNOWN_MESSAGES = {
    "Agent 未配置：需要 TRADEFLOW_AGENT_BASE_URL、TRADEFLOW_AGENT_API_KEY、TRADEFLOW_AGENT_MODEL": "agent_not_configured",
    "Agent RESPONSE_FORMAT 仅支持 json_schema 或 json_object": "agent_invalid_configuration",
    "Agent THINKING 仅支持留空、enabled 或 disabled": "agent_invalid_configuration",
    "Agent MAX_TOKENS 必须为1至32768的整数或留空": "agent_invalid_configuration",
    "Agent BASE_URL 必须为有效服务地址，不能包含凭据或查询参数": "agent_invalid_configuration",
    "远程 Agent API 必须使用 HTTPS": "agent_invalid_configuration",
    "Agent timeout 必须在 0 至 120 秒之间": "agent_invalid_configuration",
    "Agent 响应截断或未正常结束；结果不接受": "agent_output_incomplete",
    "Agent 原文引用不在邮件正文中；结果不接受": "agent_evidence_mismatch",
    "Agent requirement value 不在其原文引用中；结果不接受": "agent_evidence_mismatch",
    "Agent 网络请求失败或超时；未产生翻译结果": "agent_network_failed",
    "Agent 返回无效 JSON；结果不接受": "agent_invalid_json",
    "Agent JSON 含重复字段；结果不接受": "agent_invalid_json",
    "Agent 未返回 JSON 文本": "agent_invalid_json",
    "Agent 拒绝分析；需要人工处理": "agent_refused",
    "Agent JSON 字段与约定不一致": "agent_invalid_result",
    "Agent 未返回有效技术中文": "agent_invalid_result",
    "Agent questions/warnings 必须为非空字符串数组": "agent_invalid_result",
    "Agent requirements 必须为数组": "agent_invalid_result",
    "Agent requirement 字段与约定不一致": "agent_invalid_result",
    "Agent requirement 必须包含非空字符串": "agent_invalid_result",
    "Agent requirement 缺失中文说明": "agent_invalid_result",
    "Agent 请求或响应解析失败；未产生翻译结果": "agent_invalid_response",
    "Agent 返回错误；请检查服务及响应格式配置": "agent_invalid_response",
    "Agent 响应 choices 格式无效": "agent_invalid_response",
    "Agent 响应 message 格式无效": "agent_invalid_response",
    "Agent 响应格式无效；结果不接受": "agent_invalid_response",
    "Agent 返回内容超过大小上限": "agent_response_too_large",
    "询价 Word 含未核验内容或没有可分析原文；请先核对域、修订、图片等来源提示后再分析": "inquiry_source_unverified",
    "询价工作簿没有可分析的单元格原文，请先处理图片或嵌入对象": "inquiry_source_unverified",
    "询价工作簿含未核验公式或 Excel 错误值；请核对并提供明确原值后再分析，缓存值不能作为技术事实": "inquiry_source_unverified",
    "缺失可分析邮件正文": "inquiry_body_missing",
    "询价表格结构尚未核验；禁止按扁平文本推断商品归属": "inquiry_source_unverified",
}

for _message in (
    "商品译文未返回有效技术中文", "商品译文的规格数字与原描述不一致；结果不接受",
    "商品译文未保留受控术语或否定含义；结果不接受", "商品译文包含非原描述的数量断言；结果不接受",
    "商品译文包含未提供的配套关系；结果不接受",
):
    _KNOWN_MESSAGES[_message] = "agent_translation_guard_failed"
for _message in (
    "商品结构行缺失；结果不接受", "商品结构行与当前原文不一致；结果不接受",
    "商品数量未绑定唯一原始单元格；结果不接受", "商品翻译行数与原表格不一致；结果不接受",
    "商品料号或数量归属与原表格不一致；结果不接受", "商品译文缺少来源核对字段；结果不接受",
    "商品汇总与逐行结果不一致；结果不接受", "商品翻译响应字段无效；结果不接受",
):
    _KNOWN_MESSAGES[_message] = "agent_source_row_mismatch"

_HTTP_FAILURE = re.compile(r"Agent HTTP 请求失败 \(([1-5][0-9]{2})\)；请检查地址、模型及响应格式配置\Z")
_HTTP_REDIRECT = re.compile(r"Agent HTTP 重定向被拒绝 \((3[0-9]{2})\)；请配置直接服务地址，密钥未发送至重定向地址\Z")


def safe_analysis_failure(exc: ValueError) -> dict:
    """Return a fresh allowlisted ``code/message/retryable`` mapping.

    ``retryable`` identifies transient infrastructure failures for the UI; this
    function performs no retry or API call. Unknown text cannot become public
    output, including strings that merely start with a known error message.
    """
    text = str(exc)
    code = _KNOWN_MESSAGES.get(text, "analysis_failed")
    if _HTTP_REDIRECT.fullmatch(text):
        code = "agent_redirect_rejected"
    else:
        match = _HTTP_FAILURE.fullmatch(text)
        if match:
            status = int(match.group(1))
            if status in {401, 403}:
                code = "agent_authentication_failed"
            elif status == 429:
                code = "agent_rate_limited"
            elif status in {408, 504}:
                code = "agent_http_timeout"
            elif status >= 500:
                code = "agent_service_unavailable"
            else:
                code = "agent_http_error"
    message, retryable = _PUBLIC_FAILURES[code]
    return {"code": code, "message": message, "retryable": retryable}
