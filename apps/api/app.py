"""为 Word、飞书和其他客户端提供统一核查能力的 FastAPI 应用。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from ccitecheck.application import (
    DocumentPipelineError,
    extract_document_claims,
    parse_and_validate_document,
    validate_shared_document,
    verify_document_claims,
)
from ccitecheck.infrastructure.paths import PROJECT_ROOT
from ccitecheck.parsing.feishu import parse_feishu_snapshot
from ccitecheck.output import render_report_html, summarize_verification

from .schema import (
    DocumentCheckRequest,
    DocumentCheckResponse,
    FeishuDocumentCheckRequest,
    ReportRequest,
    ReportResponse,
    SelectionCheckRequest,
)

ADDIN_ROOT = PROJECT_ROOT / "apps" / "word_addin"
FEISHU_ADDIN_ROOT = PROJECT_ROOT / "apps" / "feishu"
LAW_DB = PROJECT_ROOT / "data" / "laws.sqlite"
REPORTS_DIR = PROJECT_ROOT / "reports"
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024

app = FastAPI(title="CCiteheck API", version="1.0.0")
app.mount("/assets", StaticFiles(directory=ADDIN_ROOT / "assets"), name="assets")
app.mount(
    "/feishu-addon",
    StaticFiles(directory=FEISHU_ADDIN_ROOT, html=True),
    name="feishu-addon",
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/taskpane.html")


@app.get("/taskpane.html", include_in_schema=False)
def taskpane() -> FileResponse:
    return FileResponse(ADDIN_ROOT / "taskpane.html")


@app.get("/help.html", include_in_schema=False)
def help_page() -> FileResponse:
    return FileResponse(ADDIN_ROOT / "help.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _validate_scope(request) -> None:
    if not (request.include_statutes or request.include_cases):
        raise HTTPException(status_code=400, detail="请至少选择一种核查范围（法规引用或司法案例）")


@app.post("/api/checks", response_model=DocumentCheckResponse)
def check_document(request: DocumentCheckRequest) -> DocumentCheckResponse:
    _validate_scope(request)
    document_bytes = _decode_document(request.docx_base64)
    if len(document_bytes) > MAX_DOCUMENT_BYTES:
        raise HTTPException(status_code=413, detail="文档超过 25 MB 限制")
    if not document_bytes.startswith(b"PK"):
        raise HTTPException(status_code=400, detail="文件不是有效的 DOCX 文档")

    try:
        with tempfile.TemporaryDirectory(prefix="ccitecheck-document-") as temporary_dir:
            document_path = Path(temporary_dir) / "document.docx"
            document_path.write_bytes(document_bytes)
            parsed_document = parse_and_validate_document(document_path)
            claim_document = extract_document_claims(
                parsed_document,
                include_statutes=request.include_statutes,
                include_cases=request.include_cases,
            )
            verification = verify_document_claims(
                claim_document,
                LAW_DB,
                semantic_check=request.semantic_check,
                include_statutes=request.include_statutes,
                include_cases=request.include_cases,
            )
    except DocumentPipelineError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return DocumentCheckResponse(
        file_name=Path(request.file_name).name,
        document_key="sha256:" + hashlib.sha256(document_bytes).hexdigest(),
        semantic_check=request.semantic_check,
        summary=summarize_verification(verification),
        verification=verification,
    )


@app.post("/api/checks/selection", response_model=DocumentCheckResponse)
def check_selection(request: SelectionCheckRequest) -> DocumentCheckResponse:
    """核查用户在 Word 中选中的文本片段：构造临时 DOCX 复用完整核查管线。"""
    _validate_scope(request)
    lines = [line.strip() for line in request.text.splitlines() if line.strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="选中内容为空，无法核查")

    from docx import Document as DocxDocument

    try:
        with tempfile.TemporaryDirectory(prefix="ccitecheck-selection-") as temporary_dir:
            selection_path = Path(temporary_dir) / "selection.docx"
            selection_doc = DocxDocument()
            for line in lines:
                selection_doc.add_paragraph(line)
            selection_doc.save(selection_path)
            parsed_document = parse_and_validate_document(selection_path)
            claim_document = extract_document_claims(
                parsed_document,
                include_statutes=request.include_statutes,
                include_cases=request.include_cases,
            )
            verification = verify_document_claims(
                claim_document,
                LAW_DB,
                semantic_check=request.semantic_check,
                include_statutes=request.include_statutes,
                include_cases=request.include_cases,
            )
            _rebase_selection_locations(verification, request.source_blocks)
    except DocumentPipelineError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return DocumentCheckResponse(
        file_name=f"{Path(request.file_name).name}（选中片段）",
        document_key="sha256:" + hashlib.sha256(
            (Path(request.file_name).name + "\0" + request.text).encode("utf-8")
        ).hexdigest(),
        semantic_check=request.semantic_check,
        summary=summarize_verification(verification),
        verification=verification,
    )


def _rebase_selection_locations(verification, source_blocks) -> None:
    """把临时选区 DOCX 的段落坐标映射回当前 Word 文档。"""
    for check in [*verification.legal_checks, *verification.case_checks]:
        rebased = []
        for location in check.source_locations:
            parts = location.block_id.split(":")
            if len(parts) != 3 or parts[:2] != ["word", "p"]:
                continue
            index = int(parts[2])
            if index >= len(source_blocks):
                continue
            source = source_blocks[index]
            location.block_id = source.block_id
            location.char_start += source.char_start
            location.char_end += source.char_start
            rebased.append(location)
        check.source_locations = rebased


@app.post("/api/feishu/checks", response_model=DocumentCheckResponse)
def check_feishu_document(request: FeishuDocumentCheckRequest) -> DocumentCheckResponse:
    """让飞书块快照复用与 Word 相同的识别、溯源和判定流水线。"""
    _validate_scope(request)
    try:
        parsed_document = validate_shared_document(
            parse_feishu_snapshot(request.snapshot)
        )
        claim_document = extract_document_claims(
            parsed_document,
            include_statutes=request.include_statutes,
            include_cases=request.include_cases,
        )
        verification = verify_document_claims(
            claim_document,
            LAW_DB,
            semantic_check=request.semantic_check,
            include_statutes=request.include_statutes,
            include_cases=request.include_cases,
        )
    except DocumentPipelineError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return DocumentCheckResponse(
        file_name=request.snapshot.title,
        document_key=parsed_document.doc_meta.doc_hash,
        semantic_check=request.semantic_check,
        summary=summarize_verification(verification),
        verification=verification,
    )


@app.post("/api/reports", response_model=ReportResponse)
def create_report(request: ReportRequest) -> ReportResponse:
    """由前端回传核查结果与人工标记，生成可交付、可审计的 HTML 核查报告。"""
    report_id = uuid.uuid4().hex
    REPORTS_DIR.mkdir(exist_ok=True)
    report_path = REPORTS_DIR / f"{report_id}.html"
    report_path.write_text(render_report_html(request), encoding="utf-8")
    return ReportResponse(report_id=report_id, url=f"/reports/{report_id}")


@app.get("/reports/{report_id}", include_in_schema=False)
def get_report(report_id: str) -> HTMLResponse:
    if not report_id.isalnum():
        raise HTTPException(status_code=404, detail="报告不存在")
    report_path = REPORTS_DIR / f"{report_id}.html"
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="报告不存在")
    return HTMLResponse(report_path.read_text(encoding="utf-8"))


def _decode_document(encoded: str) -> bytes:
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="DOCX Base64 数据无效") from exc
