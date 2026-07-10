"""Shared document-checking pipeline used by CLI and HTTP API."""

from __future__ import annotations

from pathlib import Path

from claims.extractor import extract_claims
from claims.schema import ClaimDocument
from parser.chunk_builder import build_chunks
from parser.docx_parser import parse_docx
from parser.schema import ParsedDocument
from parser.validators import validate_parsed_document
from verification.resolver import verify_claim_document_for_frontend
from verification.schema import FrontendVerificationDocument
from verification.semantic import QwenSemanticChecker, SemanticCheckError


class DocumentPipelineError(RuntimeError):
    pass


def parse_and_validate_document(input_path: str | Path) -> ParsedDocument:
    try:
        parsed_document = build_chunks(parse_docx(str(input_path)))
    except Exception as exc:
        raise DocumentPipelineError(f"文档解析失败：{exc}") from exc

    violations = validate_parsed_document(parsed_document)
    if violations:
        details = "\n".join(f"{index}. {item}" for index, item in enumerate(violations, 1))
        raise DocumentPipelineError(f"文档结构校验失败：\n{details}")
    return parsed_document


def extract_document_claims(parsed_document: ParsedDocument) -> ClaimDocument:
    try:
        return extract_claims(parsed_document)
    except ValueError as exc:
        raise DocumentPipelineError(str(exc)) from exc


def verify_document_claims(
    claim_document: ClaimDocument,
    law_db: str | Path,
    semantic_check: bool,
    qwen_model: str | None = None,
) -> FrontendVerificationDocument:
    semantic_checker = None
    if semantic_check:
        try:
            semantic_checker = QwenSemanticChecker.from_env(qwen_model)
        except SemanticCheckError as exc:
            raise DocumentPipelineError(str(exc)) from exc

    return verify_claim_document_for_frontend(
        claim_document,
        law_db,
        semantic_checker=semantic_checker,
    )
