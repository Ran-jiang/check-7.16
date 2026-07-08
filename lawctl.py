"""
本地法条库与检索路由器管理 CLI。

用法:
  python lawctl.py init
  python lawctl.py import 民法典.docx --type law --url https://flk.npc.gov.cn/... --alias 民法典
  python lawctl.py list
  python lawctl.py lookup 民法典 第一千零八十四条
  python lawctl.py search "自甘风险"
  python lawctl.py resolve claims.json --out provisions.json

独立于 main.py（v0.1/v0.2 解析入口），避免改变其单命令用法。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import typer

from statutedb.db import DEFAULT_DB_PATH, connect, init_db
from statutedb.importer import import_statute
from statutedb.law_parser import parse_law_file
from statutedb.store import StatuteStore

app = typer.Typer(name="lawctl", help="CCitecheck v0.3 法条检索层 CLI")

_DB_OPTION = typer.Option(
    str(DEFAULT_DB_PATH), "--db", help="SQLite 数据库路径"
)


def _load_dotenv() -> None:
    """轻量 .env 加载（不引入 python-dotenv 依赖）。"""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@app.command()
def init(db: str = _DB_OPTION):
    """建库建表（幂等）。"""
    conn = init_db(db)
    conn.close()
    typer.echo(f"数据库就绪: {db}")


@app.command("import")
def import_cmd(
    file: str = typer.Argument(..., help="法规文件（.docx 或 .txt）"),
    source_type: str = typer.Option(
        ..., "--type",
        help="law / judicial_interpretation / other_normative_document",
    ),
    url: Optional[str] = typer.Option(None, "--url", help="官方来源 URL"),
    title: Optional[str] = typer.Option(
        None, "--title", help="覆盖首行推断的标题"
    ),
    alias: list[str] = typer.Option(
        [], "--alias", help="人工补充简称（可多次）"
    ),
    authority: Optional[str] = typer.Option(
        None, "--authority", help="制定机关"
    ),
    db: str = _DB_OPTION,
):
    """导入一部法规（同名视为版本更新，整体替换）。"""
    path = Path(file)
    if not path.exists():
        typer.echo(f"文件不存在: {file}", err=True)
        raise typer.Exit(code=1)

    valid_types = {"law", "judicial_interpretation", "other_normative_document"}
    if source_type not in valid_types:
        typer.echo(f"--type 必须是 {'/'.join(sorted(valid_types))}", err=True)
        raise typer.Exit(code=1)

    doc = parse_law_file(str(path), title_override=title)

    if doc.warnings:
        typer.echo("⚠ 解析告警（请人工复核）:")
        for w in doc.warnings:
            typer.echo(f"  - {w}")

    conn = init_db(db)
    try:
        law_id = import_statute(
            conn, doc,
            source_type=source_type,
            source_url=url,
            extra_aliases=alias or None,
            issuing_authority=authority,
        )
    except ValueError as e:
        typer.echo(f"导入失败: {e}", err=True)
        raise typer.Exit(code=2)

    store = StatuteStore(conn)
    typer.echo(f"已导入《{doc.title}》 law_id={law_id}")
    typer.echo(f"  条文数: {store.article_count(law_id)}")
    typer.echo(f"  文号: {doc.doc_number or '-'}")
    typer.echo(f"  施行日期: {doc.effective_on or '-'}")
    conn.close()


@app.command("import-batch")
def import_batch(
    manifest: str = typer.Argument(..., help="manifest JSON（file/source_type/url/aliases）"),
    base_dir: Optional[str] = typer.Option(
        None, "--dir", help="法规文件所在目录（manifest 中 file 的基准路径）"
    ),
    db: str = _DB_OPTION,
):
    """
    按 manifest 批量导入法规。

    manifest 格式: {"laws": [{"file": "xx.docx", "source_type": "law",
    "url": "...", "aliases": ["简称"], "title": "可选，默认取文件名去日期后缀"}]}
    """
    manifest_path = Path(manifest)
    if not manifest_path.exists():
        typer.echo(f"manifest 不存在: {manifest}", err=True)
        raise typer.Exit(code=1)
    entries = json.loads(manifest_path.read_text(encoding="utf-8"))["laws"]
    root = Path(base_dir) if base_dir else manifest_path.parent

    conn = init_db(db)
    store = StatuteStore(conn)
    ok, failed = 0, []
    for entry in entries:
        if entry.get("pending"):
            typer.echo(f"- 跳过（待补文件）: {entry['file']}")
            continue
        file_path = root / entry["file"]
        title = entry.get("title") or _title_from_filename(file_path)
        try:
            doc = parse_law_file(str(file_path), title_override=title)
            law_id = import_statute(
                conn, doc,
                source_type=entry["source_type"],
                source_url=entry.get("url"),
                extra_aliases=entry.get("aliases") or None,
                issuing_authority=entry.get("authority"),
            )
        except Exception as e:  # noqa: BLE001 — 单部失败不中断批量
            failed.append((title, str(e)))
            typer.echo(f"✗ {title}: {e}", err=True)
            continue
        ok += 1
        n = store.article_count(law_id)
        max_label = conn.execute(
            """SELECT article_label FROM articles WHERE law_id=?
               ORDER BY article_num DESC, article_suffix DESC LIMIT 1""",
            (law_id,),
        ).fetchone()["article_label"]
        warn = f"  ⚠{len(doc.warnings)}条告警" if doc.warnings else ""
        typer.echo(f"✓ [{law_id:>3}] {title}  {n}条 (至{max_label}){warn}")
        for w in doc.warnings:
            typer.echo(f"     ⚠ {w}")

    typer.echo(f"\n完成: {ok} 部成功, {len(failed)} 部失败")
    conn.close()
    if failed:
        raise typer.Exit(code=2)


def _title_from_filename(path: Path) -> str:
    """"中华人民共和国民法典_20200528.docx" → "中华人民共和国民法典"。"""
    import re
    return re.sub(r"_\d{8}$", "", path.stem)


@app.command("list")
def list_cmd(db: str = _DB_OPTION):
    """列出已入库法规。"""
    conn = _open(db)
    store = StatuteStore(conn)
    laws = store.list_laws()
    if not laws:
        typer.echo("本地库为空。")
        return
    for law in laws:
        n = store.article_count(law.law_id)
        typer.echo(
            f"[{law.law_id:>3}] {law.title}  "
            f"({law.source_type}, {n}条, {law.status})"
        )
    conn.close()


@app.command()
def lookup(
    law_title: str = typer.Argument(..., help="法规名（全称或简称）"),
    article: str = typer.Argument(..., help="条号，如 第一千零八十四条"),
    db: str = _DB_OPTION,
):
    """按 法规名+条号 精确取条文。"""
    conn = _open(db)
    store = StatuteStore(conn)
    law = store.resolve_law(law_title)
    if law is None:
        typer.echo(f"本地库未收录: {law_title}", err=True)
        raise typer.Exit(code=1)
    art = store.get_article_by_label(law.law_id, article)
    if art is None:
        typer.echo(f"《{law.title}》中不存在 {article}（引注条号可能有误）", err=True)
        raise typer.Exit(code=2)
    typer.echo(f"《{law.title}》 {art.article_label}")
    if art.section_path:
        typer.echo(f"[{art.section_path}]")
    typer.echo(art.text)
    conn.close()


@app.command()
def search(
    text: str = typer.Argument(..., help="待反查的文本片段"),
    law: Optional[str] = typer.Option(None, "--law", help="限定某部法规"),
    limit: int = typer.Option(5, "--limit"),
    db: str = _DB_OPTION,
):
    """全文反查候选条文（FTS 兜底路径）。"""
    conn = _open(db)
    store = StatuteStore(conn)
    law_id = None
    if law:
        rec = store.resolve_law(law)
        if rec is None:
            typer.echo(f"本地库未收录: {law}", err=True)
            raise typer.Exit(code=1)
        law_id = rec.law_id
    hits = store.search_fulltext(text, law_id=law_id, limit=limit)
    if not hits:
        typer.echo("无命中。")
        return
    for h in hits:
        preview = h.article.text[:80].replace("\n", " ")
        typer.echo(f"[{h.score:8.3f}] 《{h.law_title}》{h.article.article_label}")
        typer.echo(f"           {preview}…")
    conn.close()


@app.command()
def resolve(
    claims_file: str = typer.Argument(..., help="v0.2 claims JSON 文件"),
    out: str = typer.Option("provisions.json", "--out", help="输出文件"),
    no_remote: bool = typer.Option(
        False, "--no-remote", help="只查本地库，不调外部源"
    ),
    db: str = _DB_OPTION,
):
    """对 claims.json 中的法条引注批量执行三层检索。"""
    _load_dotenv()

    claims_path = Path(claims_file)
    if not claims_path.exists():
        typer.echo(f"文件不存在: {claims_file}", err=True)
        raise typer.Exit(code=1)
    claims_doc = json.loads(claims_path.read_text(encoding="utf-8"))

    from retrieval.local_source import LocalSource
    from retrieval.router import ProvisionRouter, queries_from_claims

    conn = _open(db)
    local = LocalSource(StatuteStore(conn))

    fallbacks = [] if no_remote else _build_fallbacks()
    router = ProvisionRouter(local, fallbacks)

    queries = queries_from_claims(claims_doc)
    typer.echo(f"展开检索请求: {len(queries)} 条"
               f"（数据源: local{''.join(' → ' + f.name for f in fallbacks)}）")

    results = router.resolve_all(queries)

    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            [r.model_dump(mode="json") for r in results],
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )

    # 摘要
    from collections import Counter
    counts = Counter(r.status.value for r in results)
    typer.echo(f"\n=== 检索结果摘要 ===")
    for status, n in sorted(counts.items()):
        typer.echo(f"  {status}: {n}")
    for r in results:
        if r.status.value == "article_not_found":
            typer.echo(
                f"  ⚠ 引注条号可疑: 《{r.query.law_title}》"
                f"{r.query.article_label} (claim={r.query.claim_id})"
            )
    typer.echo(f"输出: {out}")
    conn.close()


def _build_fallbacks() -> list:
    """按已配置的凭据组装外部数据源。"""
    fallbacks = []
    if os.environ.get("TENCENTCLOUD_SECRET_ID"):
        from retrieval.gov_search import GovSearchSource
        fallbacks.append(GovSearchSource())
    else:
        typer.echo("（未配置腾讯云凭据，跳过 gov_search 层）")
    if os.environ.get("PKULAW_MCP_URL"):
        from retrieval.pkulaw_source import PkulawSource
        fallbacks.append(PkulawSource())
    else:
        typer.echo("（未配置法宝 MCP，跳过 pkulaw 层）")
    return fallbacks


def _open(db: str):
    if not Path(db).exists():
        typer.echo(f"数据库不存在，请先运行: python lawctl.py init", err=True)
        raise typer.Exit(code=1)
    return connect(db)


if __name__ == "__main__":
    app()
