"""FastAPI application serving both the Word add-in and checking API."""

from __future__ import annotations

import base64
import binascii
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from document_pipeline import (
    DocumentPipelineError,
    extract_document_claims,
    parse_and_validate_document,
    verify_document_claims,
)
from verification.schema import CaseLookupStatus, ComparisonVerdict

from .schema import CheckSummary, DocumentCheckRequest, DocumentCheckResponse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADDIN_ROOT = PROJECT_ROOT / "word-addin"
LAW_DB = PROJECT_ROOT / "data" / "laws.sqlite"
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024

app = FastAPI(title="CCitecheck API", version="1.0.0")
app.mount("/assets", StaticFiles(directory=ADDIN_ROOT / "assets"), name="assets")


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


@app.post("/api/checks", response_model=DocumentCheckResponse)
def check_document(request: DocumentCheckRequest) -> DocumentCheckResponse:
    document_bytes = _decode_document(request.docx_base64)
    if len(document_bytes) > MAX_DOCUMENT_BYTES:
        raise HTTPException(status_code=413, detail="文档超过 25 MB 限制")
    if not document_bytes.startswith(b"PK"):
        raise HTTPException(status_code=400, detail="文件不是有效的 DOCX 文档")

    try:
        with tempfile.NamedTemporaryFile(suffix=".docx") as temporary_file:
            temporary_file.write(document_bytes)
            temporary_file.flush()
            parsed_document = parse_and_validate_document(temporary_file.name)
            claim_document = extract_document_claims(parsed_document)
            verification = verify_document_claims(
                claim_document,
                LAW_DB,
                semantic_check=request.semantic_check,
            )
    except DocumentPipelineError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return DocumentCheckResponse(
        file_name=Path(request.file_name).name,
        semantic_check=request.semantic_check,
        summary=_summarize(verification),
        verification=verification,
    )


def _decode_document(encoded: str) -> bytes:
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="DOCX Base64 数据无效") from exc


def _summarize(verification) -> CheckSummary:
    passed = issues = bugs = exact_matches = 0
    for check in verification.legal_checks:
        if check.exact_comparison and check.exact_comparison.exact_match:
            exact_matches += 1
        comparison = check.semantic_comparison
        if comparison is None:
            continue
        if comparison.verdict == ComparisonVerdict.PASS:
            passed += 1
        elif comparison.verdict == ComparisonVerdict.ISSUE:
            issues += 1
        else:
            bugs += 1
    cases_verified = sum(
        1 for check in verification.case_checks
        if check.lookup_status == CaseLookupStatus.VERIFIED
    )
    cases_not_found = sum(
        1 for check in verification.case_checks
        if check.lookup_status == CaseLookupStatus.NOT_FOUND
    )
    return CheckSummary(
        total=len(verification.legal_checks),
        passed=passed,
        issues=issues,
        bugs=bugs,
        exact_matches=exact_matches,
        cases_verified=cases_verified,
        cases_not_found=cases_not_found,
    )
