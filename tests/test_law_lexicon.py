from pathlib import Path

from ccitecheck.infrastructure.database import init_db, upsert_law, connect
from ccitecheck.recognition.law_lexicon import LawLexicon, LawLexiconEntry
from ccitecheck.recognition.statutes import extract_legal_sources


def _small_lexicon() -> LawLexicon:
    return LawLexicon([
        LawLexiconEntry("民法典", "中华人民共和国民法典"),
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


def test_right_anchored_bare_law_uses_lexicon_without_noise_cleaning():
    sources = extract_legal_sources(
        "人民法院应予支持并依照民法典第157条规定处理。",
        _small_lexicon(),
    )

    assert len(sources) == 1
    assert sources[0].title == "民法典"
    assert sources[0].canonical_title == "中华人民共和国民法典"
    assert sources[0].resolution == "bare_lexicon"
    assert sources[0].source_span == (11, 14)
    assert sources[0].articles[0].source_span == (11, 14)


def test_explicit_and_bare_same_law_merge_articles_instead_of_dropping_bare_one():
    sources = extract_legal_sources(
        "依据《中华人民共和国民法典》第10条，并依照民法典第157条。",
        _small_lexicon(),
    )

    assert len(sources) == 1
    assert sources[0].canonical_title == "中华人民共和国民法典"
    assert [article.article for article in sources[0].articles] == ["第10条", "第157条"]


def test_unknown_bare_law_is_unresolved_and_keeps_deterministic_span():
    sources = extract_legal_sources("依照城市房地产管理法第38条", _small_lexicon())

    assert len(sources) == 1
    assert sources[0].title == ""
    assert sources[0].resolution == "bare_unresolved"
    assert sources[0].raw_title_candidate == "依照城市房地产管理法"
    assert sources[0].source_span == (9, 10)


def test_pseudo_law_suffix_is_not_recognized():
    assert extract_legal_sources("采用这种方法第10条。", _small_lexicon()) == []
