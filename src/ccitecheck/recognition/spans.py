"""在 claim.text 上确定性定位法条 mention 及其支配的引文范围。"""

from __future__ import annotations

import re

from ..domain.citation import (
    CitationLocator,
    CitationOccurrence,
    Claim,
    VerificationTarget,
)
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
_REPORTING_CONNECTOR = re.compile(
    r"^(?:(?:进一步|同时|并)\s*)?(?:规定|指出|明确|载明|认为|要求)[，,:：\s]*"
)
_PRECEDING_QUOTE = re.compile(r"[“‘\"](?P<text>[^”’\"]+)[”’\"]\s*(?:[-—–]+\s*)?$")
_APPLICATION_QUOTE = re.compile(
    r"[“‘\"](?P<text>[^”’\"]+)[”’\"]\s*(?:包括|是指|依据|依照|根据|适用)\s*$"
)
_PARAGRAPH_OCCURRENCE = re.compile(rf"(?:同条)?第(?P<number>{_NUM})款")
_ITEM_OCCURRENCE = re.compile(rf"第[（(]?(?P<number>{_NUM})[）)]?项")


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
    """生成按原文出现次数建模的 citations，不回写 ArticleRef 临时状态。"""
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
    located: list[tuple[int, int, object, object, tuple[int, int]]] = []
    occurrences: list[CitationOccurrence] = []
    for source in sources:
        for article in source.articles:
            expected = _normalize_article_no(article.article)
            candidates = [i for i, item in enumerate(mentions) if i not in used and item[2] == expected]
            all_mentions = [item for item in mentions if item[2] == expected]
            law_mention_span = source.recognition.mention_span
            if law_mention_span is not None:
                match_index = next((
                    i for i in candidates
                    if mentions[i][0] >= law_mention_span[1]
                ), None)
            elif len(all_mentions) == 1 and len(claimed_by.get(expected, ())) == 1:
                match_index = candidates[0] if candidates else None
            else:
                match_index = next((i for i in candidates if mentions[i][3] == source.title), None)
            if match_index is None:
                continue
            used.add(match_index)
            start, end, _, _ = mentions[match_index]
            previous_end = max(
                (item[1] for item in mentions if item[1] <= start),
                default=max(text.rfind(mark, 0, start) for mark in "。！？；;\n") + 1,
            )
            alias_start = (
                law_mention_span[0]
                if law_mention_span is not None
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
            located.append((start, end, article, source, (alias_start, end)))

    located.sort(key=lambda item: item[0])
    located_article_keys = {
        (id(item[3]), _normalize_article_no(item[2].article)) for item in located
    }
    for index, (article_start, article_end, article, source, citation_span) in enumerate(located):
            next_article_start = located[index + 1][0] if index + 1 < len(located) else len(text)
            paragraph_matches = list(
                _PARAGRAPH_OCCURRENCE.finditer(text, article_end, next_article_start)
            )
            if not paragraph_matches:
                boundary = _sentence_boundary(text, article_end, next_article_start)
                occurrences.append(CitationOccurrence(
                    law_title=source.canonical_title or source.title,
                    locator=CitationLocator(article=article.article),
                    role="direct",
                    citation_span=citation_span,
                    verification=_verification_target(
                        text, article_end, boundary, citation_span[0]
                    ),
                    span_status="located",
                ))
                continue
            for paragraph_index, match in enumerate(paragraph_matches):
                next_start = (
                    paragraph_matches[paragraph_index + 1].start()
                    if paragraph_index + 1 < len(paragraph_matches)
                    else next_article_start
                )
                boundary = _sentence_boundary(text, match.end(), next_start)
                verification = _verification_target(text, match.end(), boundary, match.start())
                item_match = _ITEM_OCCURRENCE.match(text, match.end(), boundary)
                item = f"第{item_match.group('number')}项" if item_match else None
                cite_end = item_match.end() if item_match else match.end()
                occurrences.append(CitationOccurrence(
                    law_title=source.canonical_title or source.title,
                    locator=CitationLocator(
                        article=article.article,
                        paragraph=f"第{match.group('number')}款",
                        item=item,
                    ),
                    role=("direct" if paragraph_index == 0 else "carry_forward"),
                    citation_span=(
                        citation_span[0]
                        if paragraph_index == 0
                        else match.start(),
                        cite_end,
                    ),
                    verification=verification,
                    span_status="located",
                ))

    # 仅出现“同条第四款”“第五款”等承前定位时，没有字面条号；直接以款/项
    # 作为一次 citation，条号使用识别阶段从上下文补齐的 ArticleRef.article。
    for source in sources:
        for article in source.articles:
            if (id(source), _normalize_article_no(article.article)) in located_article_keys:
                continue
            suffixes = [*article.paragraphs, *article.items]
            if not suffixes:
                continue
            cursor = 0
            for paragraph in article.paragraphs or [None]:
                token = paragraph or article.items[0]
                pos = text.find(token, cursor)
                if pos < 0:
                    continue
                cite_start = pos - 2 if text[max(0, pos - 2):pos] == "同条" else pos
                cite_end = pos + len(token)
                item = next((value for value in article.items if text.startswith(value, cite_end)), None)
                if item:
                    cite_end += len(item)
                boundary = _sentence_boundary(text, cite_end, len(text))
                occurrences.append(CitationOccurrence(
                    law_title=source.canonical_title or source.title,
                    locator=CitationLocator(
                        article=article.article,
                        paragraph=paragraph,
                        item=item,
                    ),
                    role="carry_forward",
                    citation_span=(cite_start, cite_end),
                    verification=_verification_target(text, cite_end, boundary, cite_start),
                    span_status="located",
                ))
                cursor = cite_end

    claim.entities.citations = sorted(
        occurrences,
        key=lambda item: item.citation_span[0] if item.citation_span else len(text),
    )


def _sentence_boundary(text: str, start: int, limit: int) -> int:
    sentence = _SENTENCE_END.search(text, start, limit)
    return sentence.start() if sentence else limit


def _verification_target(
    text: str, citation_end: int, boundary: int, citation_start: int
) -> VerificationTarget | None:
    raw = text[citation_end:boundary]
    left_trimmed = raw.lstrip("，,: ：、；;")
    connector = _REPORTING_CONNECTOR.match(left_trimmed)
    connector_length = connector.end() if connector else 0
    value = left_trimmed[connector_length:].strip()
    if "《" in value:
        value = value.split("《", 1)[0].rstrip("，,：:；; ")
    value = re.split(r"[；;]\s*其中", value, maxsplit=1)[0].strip()
    if value and not _BARE_CITATION_TAIL.fullmatch(value):
        start = citation_end + len(raw) - len(left_trimmed) + connector_length
        return VerificationTarget(
            text=value,
            span=(start, start + len(value)),
            mode="paraphrase",
            strategy="direct",
        )
    preceding = _PRECEDING_QUOTE.search(text[:citation_start])
    mode = "direct_quote"
    if preceding is None:
        preceding = _APPLICATION_QUOTE.search(text[:citation_start])
        mode = "application"
    if preceding is None:
        return None
    quote_start, quote_end = preceding.span("text")
    return VerificationTarget(
        text=preceding.group("text"),
        span=(quote_start, quote_end),
        mode=mode,
        strategy="direct",
    )
__all__ = ["locate_claim_article_spans"]
