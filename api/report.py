"""生成可交付的核查报告（自包含 HTML，可直接打印或另存为 PDF）。"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone, timedelta

from .schema import ReportRequest

CST = timezone(timedelta(hours=8))

LOOKUP_STATUS_LABELS = {
    "article_found": "已取得法条原文",
    "relevant_articles_found": "已召回相关条款",
    "law_found_article_missing": "法规存在，未找到该条",
    "law_found_text_unavailable": "法规存在，条文全文不可用",
    "law_not_found": "未检索到该法规",
    "source_not_configured": "数据源未配置",
    "source_error": "数据源调用失败",
    "not_verifiable": "非法条类文件，不做条文核验",
}

CASE_STATUS_LABELS = {
    "verified": "案例已验证",
    "not_found": "案例未命中，疑似有误或不存在",
    "manual_review": "候选案例需人工确认",
    "source_not_configured": "数据源未配置",
    "source_error": "数据源调用失败",
}

DECISION_LABELS = {
    "accepted": "已接受",
    "ignored": "已忽略",
}

VERDICT_LABELS = {
    "pass": "语义通过",
    "issue": "需核实",
    "bug": "无法判断",
}


def render_report_html(request: ReportRequest) -> str:
    generated_at = datetime.now(CST).strftime("%Y-%m-%d %H:%M（北京时间）")
    summary = request.summary
    decision_counts = {"accepted": 0, "ignored": 0}
    for value in request.decisions.values():
        if value in decision_counts:
            decision_counts[value] += 1

    rows = []
    for check in request.verification.legal_checks:
        rows.append(_legal_check_section(check, request.decisions.get(check.check_id)))
    case_rows = [
        _case_check_section(check, request.decisions.get(check.check_id))
        for check in request.verification.case_checks
    ]

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>CCiteheck 核查报告 - {_esc(request.file_name)}</title>
<style>
  body {{ font-family: "Songti SC", "SimSun", serif; margin: 40px auto; max-width: 860px;
         color: #1a1a1a; line-height: 1.7; }}
  h1 {{ font-size: 22px; border-bottom: 2px solid #1a1a1a; padding-bottom: 8px; }}
  h2 {{ font-size: 17px; margin-top: 32px; }}
  .meta, .summary {{ background: #f7f6f2; padding: 12px 16px; border-radius: 6px; }}
  .meta td {{ padding: 2px 18px 2px 0; }}
  .check {{ border: 1px solid #d9d5cc; border-radius: 6px; padding: 14px 16px; margin: 14px 0;
            page-break-inside: avoid; }}
  .check-head {{ display: flex; justify-content: space-between; gap: 12px; }}
  .source {{ font-weight: bold; }}
  .pill {{ font-size: 12px; padding: 1px 10px; border-radius: 10px; border: 1px solid #999;
           white-space: nowrap; align-self: flex-start; }}
  .pill.issue {{ border-color: #b3261e; color: #b3261e; }}
  .pill.pass {{ border-color: #1b6e3c; color: #1b6e3c; }}
  blockquote {{ margin: 8px 0; padding: 6px 12px; background: #fbfaf7; border-left: 3px solid #b9b3a5;
                font-size: 14px; }}
  .field {{ font-size: 13px; margin: 4px 0; }}
  .field b {{ color: #555; }}
  .finding {{ background: #fdf3f2; border-left: 3px solid #b3261e; padding: 6px 10px;
              margin: 6px 0; font-size: 13px; }}
  .statute {{ font-size: 13px; white-space: pre-wrap; background: #f4f7f4; padding: 8px 12px;
              border-radius: 4px; max-height: 260px; overflow: auto; }}
  .decision {{ font-size: 13px; margin-top: 8px; font-weight: bold; }}
  .trace {{ font-size: 12px; margin: 5px 0; padding: 6px 9px; background: #f5f5f5;
            border-radius: 4px; overflow-wrap: anywhere; }}
  footer {{ margin-top: 40px; font-size: 12px; color: #777; border-top: 1px solid #ccc;
            padding-top: 10px; }}
  @media print {{ .statute {{ max-height: none; }} }}
</style>
</head>
<body>
<h1>CCiteheck 法律引用核查报告</h1>
<table class="meta">
  <tr><td><b>核查文件</b></td><td>{_esc(request.file_name)}</td></tr>
  <tr><td><b>报告生成时间</b></td><td>{generated_at}</td></tr>
  <tr><td><b>语义核查</b></td><td>{"已开启（千问）" if request.semantic_check else "未开启（仅存在性核查）"}</td></tr>
</table>

<h2>一、核查摘要</h2>
<div class="summary">
  共识别法规或案例引用 <b>{summary.total}</b> 处：通过 <b>{summary.passed}</b>，
  需核实 <b>{summary.issues}</b>，无法判断 <b>{summary.bugs}</b>；
  案号验证通过 <b>{summary.cases_verified}</b>，案号未命中 <b>{summary.cases_not_found}</b>。<br>
  人工处理记录：接受 <b>{decision_counts["accepted"]}</b>，
  忽略 <b>{decision_counts["ignored"]}</b>。
</div>

<h2>二、法律引用明细（{len(rows)} 项）</h2>
{"".join(rows) if rows else "<p>未识别到法律引用。</p>"}

<h2>三、案例核验（{len(case_rows)} 项）</h2>
{"".join(case_rows) if case_rows else "<p>未识别到案例引用。</p>"}

<footer>
  本报告由 CCiteheck 自动生成，核查过程与数据来源已结构化记录，可供审计追溯。
  核查结论仅针对法律引用的存在性、时效与语义对应关系，不构成法律意见。
</footer>
</body>
</html>"""


def _legal_check_section(check, decision: str | None) -> str:
    findings = list(check.rule_findings) + list(
        check.semantic_comparison.issues if check.semantic_comparison else []
    )
    verdict = check.semantic_comparison.verdict.value if check.semantic_comparison else None
    if findings:
        pill_class, pill_text = "issue", "需核实"
    elif verdict == "pass":
        pill_class, pill_text = "pass", "语义通过"
    elif verdict == "bug":
        pill_class, pill_text = "", "无法判断"
    else:
        pill_class, pill_text = "", "未做语义核查"

    parts = [
        '<div class="check">',
        '<div class="check-head">',
        f'<span class="source">《{_esc(check.law_title)}》{_esc(check.article_no or "")}'
        f'　<small>{_esc(check.check_id)}</small></span>',
        f'<span class="pill {pill_class}">{pill_text}</span>',
        "</div>",
        f"<blockquote>{_esc(check.claim_text)}</blockquote>",
        f'<div class="field"><b>溯源状态：</b>'
        f'{LOOKUP_STATUS_LABELS.get(check.lookup_status.value, check.lookup_status.value)}</div>',
    ]
    evidence = check.evidence
    if evidence is not None:
        source = evidence.data_source
        origin = _esc(source.source_name)
        url = _plain_url(source.source_url)
        if url:
            origin += f'（<a href="{_esc(url)}">{_esc(url)}</a>）'
        parts.append(f'<div class="field"><b>数据来源：</b>{origin}</div>')
        if evidence.version_label or evidence.version_status:
            parts.append(
                f'<div class="field"><b>时效状态：</b>'
                f"{_esc(evidence.version_label or evidence.version_status or '')}</div>"
            )
    for finding in findings:
        parts.append(
            f'<div class="finding"><b>{_esc(finding.risk_level.value)} · '
            f"{_esc(finding.error_type.value)}</b><br>"
            f"{_esc(finding.diff_summary)}<br>建议：{_esc(finding.suggestion)}</div>"
        )
    if check.semantic_comparison and check.semantic_comparison.notes:
        parts.append(
            f'<div class="field"><b>备注：</b>{_esc(check.semantic_comparison.notes)}</div>'
        )
    if evidence is not None and evidence.article_text:
        article_no = evidence.article_no or check.article_no or ""
        heading = f"《{evidence.law_title or check.law_title}》{article_no}"
        article_text = _strip_repeated_article_heading(evidence.article_text, article_no)
        parts.append(f'<div class="statute">{_esc(heading)}　{_esc(article_text)}</div>')
    parts.extend(_source_attempt_sections(check.source_attempts))
    if decision in DECISION_LABELS:
        parts.append(f'<div class="decision">人工处理：{DECISION_LABELS[decision]}</div>')
    parts.append("</div>")
    return "".join(parts)


def _strip_repeated_article_heading(text: str, article_no: str) -> str:
    if not article_no:
        return text
    return re.sub(
        r"^\s*第[〇零一二三四五六七八九十百千万两0-9]+条(?:之[〇零一二三四五六七八九十百千万两0-9]+)?[\s　]*",
        "",
        text,
        count=1,
    )


def _case_check_section(check, decision: str | None) -> str:
    status = CASE_STATUS_LABELS.get(check.lookup_status.value, check.lookup_status.value)
    parts = [
        '<div class="check">',
        '<div class="check-head">',
        f'<span class="source">{_esc(check.cited_case_number or check.cited_case_name or "案例线索")}</span>',
        f'<span class="pill {"pass" if check.lookup_status.value == "verified" else "issue"}">'
        f"{status}</span>",
        "</div>",
        f"<blockquote>{_esc(check.claim_text)}</blockquote>",
    ]
    if check.evidence is not None:
        parts.append(
            f'<div class="field"><b>命中案例：</b>{_esc(check.evidence.title)}'
            f"（{_esc(check.evidence.court)}）</div>"
        )
        case_url = _plain_url(check.evidence.url)
        if case_url:
            parts.append(
                f'<div class="field"><b>溯源链接：</b>'
                f'<a href="{_esc(case_url)}">{_esc(case_url)}</a></div>'
            )
    if check.message:
        parts.append(f'<div class="field"><b>说明：</b>{_esc(check.message)}</div>')
    parts.extend(_source_attempt_sections(check.source_attempts))
    if decision in DECISION_LABELS:
        parts.append(f'<div class="decision">人工处理：{DECISION_LABELS[decision]}</div>')
    parts.append("</div>")
    return "".join(parts)


def _source_attempt_sections(attempts) -> list[str]:
    if not attempts:
        return []
    parts = ['<div class="field"><b>全链路溯源记录：</b></div>']
    for attempt in attempts:
        url = _plain_url(getattr(attempt, "source_url", None))
        source = _esc(attempt.source_name)
        if url:
            source += f' · <a href="{_esc(url)}">{_esc(url)}</a>'
        fetched_at = getattr(attempt, "fetched_at", None)
        time_text = f" · 获取时间 {_esc(fetched_at)}" if fetched_at else " · 获取时间未提供"
        status = getattr(attempt.status, "value", attempt.status)
        message = f" · {_esc(attempt.message)}" if attempt.message else ""
        parts.append(
            f'<div class="trace">{source} · 状态 {_esc(status)}{time_text}{message}</div>'
        )
        metadata = getattr(attempt, "metadata", {})
        for route in metadata.get("route_attempts", []):
            route_name = _esc(route.get("service", "unknown"))
            route_status = _esc(route.get("status", "unknown"))
            candidate_count = route.get("candidate_count")
            candidate_text = (
                f" · 候选 {int(candidate_count)} 条"
                if isinstance(candidate_count, int)
                else ""
            )
            parts.append(
                f'<div class="trace">↳ MCP {route_name} · 状态 '
                f"{route_status}{candidate_text}</div>"
            )
    return parts


def _plain_url(value: object) -> str:
    """北大法宝部分接口返回 Markdown 形式的链接（[文本](URL)），归一化为纯 URL。"""
    if not value:
        return ""
    text = str(value).strip()
    match = re.search(r"\((https?://[^)]+)\)", text)
    if match:
        return match.group(1)
    return text if text.startswith("http") else ""


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)
