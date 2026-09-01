from pathlib import Path

from ccitecheck.infrastructure.database import init_db, upsert_law, connect
from ccitecheck.query_construction.name_resolution import LawLexicon, LawLexiconEntry
from ccitecheck.query_construction.service import QueryResources, resolve_identity_candidates
from ccitecheck.recognition.statutes import (
    extract_alias_declarations,
    extract_legal_sources,
)


def _small_lexicon() -> LawLexicon:
    return LawLexicon([
        LawLexiconEntry("民法典", "中华人民共和国民法典"),
        LawLexiconEntry("民事诉讼法", "中华人民共和国民事诉讼法"),
        LawLexiconEntry("反不正当竞争法", "中华人民共和国反不正当竞争法"),
    ])


def test_longest_suffix_match_prefers_longest_known_title():
    lexicon = LawLexicon([
        LawLexiconEntry("诉讼法", "某诉讼法"),
        LawLexiconEntry("民事诉讼法", "中华人民共和国民事诉讼法"),
    ])

    matched = lexicon.longest_suffix_match("除可依照民事诉讼法")

    assert matched is not None
    assert matched.surface_title == "民事诉讼法"
    assert matched.canonical_title == "中华人民共和国民事诉讼法"


def test_ambiguous_alias_is_never_reintroduced_by_later_duplicate():
    lexicon = LawLexicon([
        LawLexiconEntry("共用别名", "法规甲"),
        LawLexiconEntry("共用别名", "法规乙"),
        LawLexiconEntry("共用别名", "法规甲"),
    ])

    assert lexicon.longest_suffix_match("根据共用别名") is None


def test_sqlite_lexicon_maps_alias_to_canonical_title(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        upsert_law(conn, {
            "title": "中华人民共和国民事诉讼法",
            "source_type": "law",
            "aliases": ["民诉法"],
        })
    LawLexicon.clear_cache()

    matched = LawLexicon.load(db_path).longest_suffix_match("依据民诉法")

    assert matched is not None
    assert matched.canonical_title == "中华人民共和国民事诉讼法"


def test_missing_sqlite_falls_back_to_json_with_explicit_aliases(tmp_path: Path):
    LawLexicon.clear_cache()

    matched = LawLexicon.load(tmp_path / "missing.sqlite").longest_suffix_match("根据民诉解释")

    assert matched is not None
    assert matched.canonical_title == "最高人民法院关于适用《中华人民共和国民事诉讼法》的解释"


def test_recognition_keeps_bare_title_raw_and_query_layer_resolves_it():
    source = extract_legal_sources(
        "人民法院应予支持并依照民法典第157条规定处理。"
    )[0]
    assert source.title == "民法典"
    assert source.canonical_title is None
    assert source.jurisdiction is None
    assert source.recognition.form == "bare"

    candidates = resolve_identity_candidates(
        source.title,
        QueryResources(lexicon=_small_lexicon()),
        raw_title_candidate=source.raw_title_candidate,
    )
    assert [item.title for item in candidates] == [
        "民法典", "中华人民共和国民法典"
    ]


def test_recognition_does_not_merge_distinct_raw_titles():
    sources = extract_legal_sources(
        "依据《中华人民共和国民法典》第10条，并依照民法典第157条。"
    )
    assert [(item.title, [a.article for a in item.articles]) for item in sources] == [
        ("中华人民共和国民法典", ["第10条"]),
        ("民法典", ["第157条"]),
    ]


def test_short_name_declaration_is_recorded_without_resolution():
    text = "《中华人民共和国民法典》（以下简称《民法典》）第10条。"
    declarations = extract_alias_declarations(text)
    assert [(item.full_name_raw, item.alias_raw) for item in declarations] == [
        ("中华人民共和国民法典", "民法典")
    ]


def test_distinct_explicit_laws_in_same_sentence_are_not_merged():
    sources = extract_legal_sources("依据《民法典》第10条和《刑法》第20条。")
    assert [(source.title, [a.article for a in source.articles]) for source in sources] == [
        ("民法典", ["第10条"]),
        ("刑法", ["第20条"]),
    ]


def test_bare_law_collects_following_sibling_articles():
    sources = extract_legal_sources("除可依照民事诉讼法第114条、第117条采取措施外。")
    assert [(item.title, [a.article for a in item.articles]) for item in sources] == [
        ("民事诉讼法", ["第114条", "第117条"])
    ]


def test_bare_article_chain_stops_before_another_law():
    sources = extract_legal_sources("依照民事诉讼法第114条及刑法第20条处理。")
    assert [(source.title, [a.article for a in source.articles]) for source in sources] == [
        ("民事诉讼法", ["第114条"]),
        ("刑法", ["第20条"]),
    ]


def test_unknown_bare_law_remains_raw_candidate():
    source = extract_legal_sources("依照城市房地产管理法第38条")[0]
    assert source.title == "城市房地产管理法"
    assert source.raw_title_candidate == "城市房地产管理法"
    assert source.canonical_title is None


def test_pseudo_law_suffix_is_not_recognized():
    assert extract_legal_sources("采用这种方法第10条。") == []
