"""在 claim.text 上确定性定位法条 mention 及其支配的引文范围。"""

from __future__ import annotations

import re

from ..domain.citation import Claim
from ..domain.legal_numbers import chinese_number_to_int

_ARTICLE_MENTION = re.compile(
    r"第[0-9○零一二三四五六七八九十百千两]+条(?:之[0-9○零一二三四五六七八九十]+)?"
)
_NUM = r"[0-9○零一二三四五六七八九十百千两]+"
_ARTICLE_ENUM = re.compile(rf"第(?P<values>{_NUM}(?:[、,，]{_NUM})+)条")
_ARTICLE_RANGE = re.compile(rf"第(?P<start>{_NUM})条(?:至|到)第?(?P<end>{_NUM})条")
_RELATIVE_ARTICLE = re.compile(r"前条|该条")
_SENTENCE_END = re.compile(r"[。！？\n]")
_BARE_CITATION_TAIL = re.compile(r"^(?:之)?规定[，,]?(?:判决|裁定|决定)如下$")


def _normalize_article_no(value: str) -> str:
    match = re.fullmatch(
        r"第(?P<base>[0-9○零一二三四五六七八九十百千两]+)条(?:之(?P<suffix>[0-9○零一二三四五六七八九十]+))?",
        value.strip(),
    )
    if not match:
        return value.strip()
    base = chinese_number_to_int(match.group("base"))
    suffix = chinese_number_to_int(match.group("suffix")) if match.group("suffix") else None
    return f"{base}:{suffix or ''}" if base is not None else value.strip()


def _raw_mentions(text: str) -> list[tuple[int, int, str]]:
    result: list[tuple[int, int, str]] = []
    occupied: list[tuple[int, int]] = []
    for match in _ARTICLE_RANGE.finditer(text):
        start_no = chinese_number_to_int(match.group("start"))
        end_no = chinese_number_to_int(match.group("end"))
        if start_no is not None and end_no is not None and 0 < end_no - start_no <= 50:
            result.extend((match.start(), match.end(), f"{value}:") for value in range(start_no, end_no + 1))
            occupied.append(match.span())
    for match in _ARTICLE_ENUM.finditer(text):
        if any(left <= match.start() < right for left, right in occupied):
            continue
        values = re.split(r"[、,，]", match.group("values"))
        for value in values:
            number = chinese_number_to_int(value)
            if number is not None:
                result.append((match.start(), match.end(), f"{number}:"))
        occupied.append(match.span())
    for match in _ARTICLE_MENTION.finditer(text):
        if not any(left <= match.start() < right for left, right in occupied):
            result.append((match.start(), match.end(), _normalize_article_no(match.group())))
    numeric = sorted(result)
    for match in _RELATIVE_ARTICLE.finditer(text):
        previous = next((item for item in reversed(numeric) if item[1] <= match.start()), None)
        if previous:
            base = int(previous[2].split(":", 1)[0])
            resolved = base - 1 if match.group() == "前条" else base
            if resolved > 0:
                result.append((match.start(), match.end(), f"{resolved}:"))
    return sorted(result)


def locate_claim_article_spans(claim: Claim) -> None:
    """只修饰 ArticleRef 的 0.3 定位字段；抽取模型不参与坐标生成。"""
    text = claim.text
    sources = list(getattr(claim.entities, "legal_sources", []))
    aliases: list[tuple[str, str]] = []
    for source in sources:
        if not source.title:
            continue
        aliases.append((source.title, source.title))
        short = source.title.removeprefix("中华人民共和国")
        if short != source.title:
            aliases.append((short, source.title))
    mentions = []
    for start, end, normalized in _raw_mentions(text):
        sentence_start = max(text.rfind(mark, 0, start) for mark in "。！？；;\n") + 1
        prefix = text[sentence_start:start]
        owners = [
            (prefix.rfind(alias), owner)
            for alias, owner in aliases
            if prefix.rfind(alias) >= 0
        ]
        owner = max(owners)[1] if owners else None
        mentions.append((start, end, normalized, owner))
    claimed_by: dict[str, set[str]] = {}
    for source in sources:
        for article in source.articles:
            claimed_by.setdefault(_normalize_article_no(article.article), set()).add(source.title)
    used: set[int] = set()
    located: list[tuple[int, int, object, str]] = []
    for source in getattr(claim.entities, "legal_sources", []):
        for article in source.articles:
            expected = _normalize_article_no(article.article)
            candidates = [i for i, item in enumerate(mentions) if i not in used and item[2] == expected]
            all_mentions = [item for item in mentions if item[2] == expected]
            if article.source_span is not None:
                match_index = next((
                    i for i in candidates
                    if mentions[i][0] >= article.source_span[1]
                ), None)
            elif len(all_mentions) == 1 and len(claimed_by.get(expected, ())) == 1:
                match_index = candidates[0] if candidates else None
            else:
                match_index = next((i for i in candidates if mentions[i][3] == source.title), None)
            if match_index is None:
                article.span_status = "error"
                article.mention_span = None
                article.citation_span = None
                article.quote_span = None
                continue
            used.add(match_index)
            start, end, _, _ = mentions[match_index]
            article.mention_span = (start, end)
            previous_end = max(
                (item[1] for item in mentions if item[1] <= start),
                default=max(text.rfind(mark, 0, start) for mark in "。！？；;\n") + 1,
            )
            alias_start = (
                article.source_span[0]
                if article.source_span is not None
                else max(
                    (
                        text.rfind(alias, previous_end, start)
                        for alias, owner in aliases
                        if owner == source.title and text.rfind(alias, previous_end, start) >= 0
                    ),
                    default=start,
                )
            )
            if alias_start > 0 and text[alias_start - 1] == "《":
                alias_start -= 1
            next_mention_start = min(
                (item[0] for item in mentions if item[0] > start),
                default=len(text),
            )
            citation_end = end
            for suffix in [*article.paragraphs, *article.items]:
                suffix_start = text.find(suffix, end, next_mention_start)
                if suffix_start >= 0:
                    citation_end = max(citation_end, suffix_start + len(suffix))
            article.citation_span = (alias_start, citation_end)
            article.span_status = "located"
            article.reference_role = "inherited" if source.resolution == "inherited" else "direct"
            located.append((start, end, article, source.title))

    # 承前引用兜底：文书写"同条第X款""第五款""前款"等，条号已由抽取承前
    # 解析，但文本无字面"第X条"、主对齐失败。定位到其实际款/项文字，使其
    # 可参与语义核查、可定位原文。
    anchor_end: dict[str, int] = {}
    for start, end, article, _title in located:
        key = _normalize_article_no(article.article)
        anchor_end[key] = max(anchor_end.get(key, 0), end)
    for source in getattr(claim.entities, "legal_sources", []):
        for article in source.articles:
            if article.span_status != "error":
                continue
            suffixes = [*article.paragraphs, *article.items]
            if not suffixes:
                continue
            search_from = anchor_end.get(_normalize_article_no(article.article), 0)
            pos = text.find(suffixes[0], search_from)
            if pos < 0:
                continue
            cite_start = pos - 2 if text[max(0, pos - 2):pos] == "同条" else pos
            cite_end = pos + len(suffixes[0])
            for suffix in suffixes[1:]:
                nxt = text.find(suffix, cite_end)
                if nxt >= 0:
                    cite_end = max(cite_end, nxt + len(suffix))
            article.mention_span = (cite_start, cite_end)
            article.citation_span = (cite_start, cite_end)
            article.span_status = "located"
            article.reference_role = "inherited"
            located.append((cite_start, cite_end, article, source.title))

    located.sort(key=lambda item: item[0])
    # 显式的另一法源出现在前一条引用的命题中，是内部转引。
    for index, (start, _end, article, title) in enumerate(located):
        if index == 0 or article.reference_role == "inherited":
            continue
        _prev_start, prev_end, parent, parent_title = located[index - 1]
        bridge = text[prev_end:start]
        short_title = title.removeprefix("中华人民共和国")
        if (title in bridge or short_title in bridge) and re.search(r"属于|所称|规定的|依照|根据", bridge):
            article.reference_role = "nested"
            article.parent_reference_id = (parent_title, parent.article)

    for index, (start, end, article, _title) in enumerate(located):
        next_start = next(
            (
                item[0] for item in located[index + 1:]
                if item[0] > start and item[2].reference_role != "nested"
            ),
            len(text),
        )
        sentence = _SENTENCE_END.search(text, end, next_start)
        boundary = sentence.start() if sentence else next_start
        proposition = text[end:boundary].strip("，,: ：、；;")
        if (
            proposition
            and "《" not in proposition
            and not _BARE_CITATION_TAIL.fullmatch(proposition)
        ):
            prop_start = end + len(text[end:boundary]) - len(text[end:boundary].lstrip("，,: ：、；;"))
            article.quote_span = (prop_start, boundary)
        else:
            article.quote_span = None

        if article.reference_role == "nested":
            article.quote_span = None


__all__ = ["locate_claim_article_spans"]
