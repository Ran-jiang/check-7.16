"""
CCiteheck CLI 实现。

用法:
  PYTHONPATH=src python -m apps.cli.main sample.docx --out result.json
  PYTHONPATH=src python -m apps.cli.main sample.docx --out result.json --claims-out claims.json
  PYTHONPATH=src python -m apps.cli.main sample.docx --claims-out claims.json --verify-out verify.json --law-db data/laws.sqlite


流程：
  1. 解析 DOCX → ParsedDocument
  2. 构建 chunks
  3. 校验解析产物 → 写入 JSON
  4. 如指定 --claims-out → 识别引用 → 写入 claims JSON
  5. 如指定 --verify-out → 执行溯源与判定 → 写入前端 JSON
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from ccitecheck.domain.document import ParsedDocument
from ccitecheck.domain.citation import ClaimDocument
from ccitecheck.application import (
    DocumentPipelineError,
    extract_document_claims,
    parse_and_validate_document,
    verify_document_claims,
)
from ccitecheck.infrastructure.runtime_checks import check_runtime

app = typer.Typer(
    name="ccitecheck",
    help="CCiteheck - DOCX 法律文档中间表示层 + 可验证主张识别层",
)


@app.command()
def cache(
    action: str = typer.Argument(
        "status", help="status=查看 / refresh=再验证过期条目 / clear=清空"
    ),
):
    """管理北大法宝查询缓存（data/pkulaw_cache.sqlite）。"""
    from ccitecheck.tracing.sources.pkulaw.cache import cache_clear, cache_refresh, cache_status

    if action == "status":
        info = cache_status()
        typer.echo(f"缓存库: {info['path']}")
        if not info["groups"]:
            typer.echo("缓存为空")
        for group in info["groups"]:
            typer.echo(
                f"  {group['kind']}/{group['status']}: {group['n']} 条"
            )
        typer.echo(f"已过期待处理: {info['expired']} 条")
    elif action == "refresh":
        outcome = cache_refresh()
        typer.echo(
            f"再验证续期 {outcome['revalidated']} 条，清除失效 {outcome['removed']} 条"
        )
    elif action == "clear":
        count = cache_clear()
        typer.echo(f"已清空 {count} 条缓存")
    else:
        typer.echo(f"未知操作: {action}（可用 status/refresh/clear）")
        raise typer.Exit(code=1)


@app.command()
def doctor(
    law_db: str = typer.Option(
        "data/laws.sqlite", "--law-db", help="SQLite 本地法规库路径"
    ),
):
    """检查本地运行所需资源。"""
    results = check_runtime(law_db)
    failed = False
    for result in results:
        status = "OK" if result.ok else "FAIL"
        typer.echo(f"{status} {result.name}: {result.message}")
        failed = failed or not result.ok
    if failed:
        raise typer.Exit(code=1)


@app.command()
def parse(
    input_file: str = typer.Argument(..., help="DOCX 文件路径"),
    out: Optional[str] = typer.Option(None, "--out", help="ParsedDocument JSON 输出文件路径"),
    claims_out: Optional[str] = typer.Option(
        None, "--claims-out", help="ClaimDocument JSON 输出文件路径"
    ),
    verify_out: Optional[str] = typer.Option(
        None, "--verify-out", help="前端核查 JSON 输出文件路径"
    ),
    law_db: str = typer.Option(
        "data/laws.sqlite", "--law-db", help="SQLite 本地法规库路径"
    ),
    semantic_check: bool = typer.Option(
        True,
        "--semantic-check/--no-semantic-check",
        help="千问语义核查（默认开启；仅存在性核查时用 --no-semantic-check 关闭）",
    ),
    qwen_model: Optional[str] = typer.Option(
        None, "--qwen-model", help="语义检查使用的千问模型"
    ),
    include_statutes: bool = typer.Option(
        True,
        "--include-statutes/--no-include-statutes",
        help="是否核查法规引用（默认开启）",
    ),
    include_cases: bool = typer.Option(
        True,
        "--include-cases/--no-include-cases",
        help="是否同时核查司法案例案号（默认开启）",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", help="逐条打印识别出的主张明细"
    ),
):
    """
    解析 DOCX 文件，生成 ParsedDocument、ClaimDocument 和前端核查 JSON。
    """
    input_path = Path(input_file)
    if not input_path.exists():
        typer.echo(f"Error: File not found: {input_file}", err=True)
        raise typer.Exit(code=1)

    if input_path.suffix.lower() not in (".docx",):
        typer.echo(f"Error: Not a .docx file: {input_file}", err=True)
        raise typer.Exit(code=1)

    if out is None and claims_out is None and verify_out is None:
        typer.echo("Error: 必须指定 --out、--claims-out 或 --verify-out", err=True)
        raise typer.Exit(code=1)
    if not (include_statutes or include_cases):
        typer.echo("Error: 法规引用与司法案例至少选择一种", err=True)
        raise typer.Exit(code=1)

    try:
        parsed_doc = parse_and_validate_document(input_path)
    except DocumentPipelineError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)

    # 输出文档解析结果。
    if out is not None:
        _write_parsed_document(parsed_doc, out)
        numbering_unresolved = any(b.numbering_unresolved for b in parsed_doc.blocks)
        typer.echo("Parsed successfully.")
        typer.echo(f"Blocks: {len(parsed_doc.blocks)}")
        typer.echo(f"Anchors: {len(parsed_doc.anchors)}")
        typer.echo(f"Chunks: {len(parsed_doc.chunks)}")
        typer.echo(f"Numbering unresolved: {numbering_unresolved}")
        typer.echo(f"Output: {out}")

    # 按需识别引用并输出核查结果。
    claim_doc: ClaimDocument | None = None
    if claims_out is not None or verify_out is not None:
        claim_doc = _extract_claims(
            parsed_doc,
            verbose,
            include_statutes=include_statutes,
            include_cases=include_cases,
        )

    if claims_out is not None and claim_doc is not None:
        _write_claim_document(claim_doc, claims_out)

    if verify_out is not None and claim_doc is not None:
        _write_verification(
            claim_doc,
            verify_out,
            law_db,
            semantic_check,
            qwen_model,
            include_statutes,
            include_cases,
        )


# ============================================================
# 辅助函数
# ============================================================

def _write_parsed_document(parsed_doc: ParsedDocument, out_path_str: str) -> None:
    """写入文档解析结果。"""
    out_path = Path(out_path_str)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(parsed_doc.model_dump_json(indent=2), encoding="utf-8")


def _extract_claims(
    parsed_doc: ParsedDocument,
    verbose: bool = False,
    include_statutes: bool = True,
    include_cases: bool = True,
) -> ClaimDocument:
    """识别文档中的可核查引用。"""
    from collections import Counter

    try:
        claim_doc = extract_document_claims(
            parsed_doc,
            include_statutes=include_statutes,
            include_cases=include_cases,
        )
    except DocumentPipelineError as e:
        typer.echo(f"Claim validation FAILED:\n{e}", err=True)
        raise typer.Exit(code=2)

    # 打印摘要
    type_counts = Counter(c.claim_type.value for c in claim_doc.claims)
    typer.echo("\n=== 引用识别摘要 ===")
    typer.echo(f"Total claims: {len(claim_doc.claims)}")
    for ct in ["legal_source_claim", "case_citation", "case_holding_paraphrase"]:
        count = type_counts.get(ct, 0)
        if count > 0:
            typer.echo(f"  {ct}: {count}")

    # 逐条明细默认不刷屏，需要时用 --verbose 打开
    if verbose:
        for claim in claim_doc.claims:
            typer.echo(f"\n[{claim.claim_id}] {claim.claim_type.value}")
            text = claim.text[:150].replace('\n', ' ')
            typer.echo(f"  Text: {text}{'...' if len(claim.text) > 150 else ''}")
            typer.echo(f"  Anchors: {', '.join(claim.anchor_ids)}")
            typer.echo(f"  Route: {claim.verification_route.value}")

    return claim_doc


def _write_claim_document(claim_doc: ClaimDocument, claims_out: str) -> None:
    """写入引用识别结果。"""
    out_path = Path(claims_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(claim_doc.model_dump_json(indent=2), encoding="utf-8")

    typer.echo(f"Claims output: {claims_out}")


def _write_verification(
    claim_doc: ClaimDocument,
    verify_out: str,
    law_db: str,
    semantic_check: bool,
    qwen_model: str | None,
    include_statutes: bool = True,
    include_cases: bool = True,
) -> None:
    """执行溯源与判定并写入前端核查结果。"""
    try:
        frontend_doc = verify_document_claims(
            claim_doc,
            law_db,
            semantic_check=semantic_check,
            qwen_model=qwen_model,
            include_statutes=include_statutes,
            include_cases=include_cases,
        )
    except DocumentPipelineError as exc:
        typer.echo(f"Semantic check error: {exc}", err=True)
        raise typer.Exit(code=1)
    out_path = Path(verify_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        frontend_doc.model_dump_json(indent=2),
        encoding="utf-8",
    )
    typer.echo(
        "Verification checks: "
        f"{len(frontend_doc.statute_results) + len(frontend_doc.case_results)}"
    )
    typer.echo(f"Verification output: {verify_out}")


def main():
    app()


if __name__ == "__main__":
    main()
