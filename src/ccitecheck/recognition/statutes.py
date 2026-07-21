"""
CCiteheck 法源与条款号识别。

负责：
  1. 从文本中识别《》书名号引用的法律规范
  2. 提取条款号（条/款/项）并归属到对应的法源
  3. 推断法律规范类型（source_type）

设计决策：
  - 条款号必须出现在对应法源书名号之后、下一个法源书名号之前
  - 排除明显非法律规范文件（合同、协议、授权书等）
  - source_type 推断是确定性规则，机关名优先于标题后缀
  - 法源后无条款号时仍构成法源，articles 为空
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from ..domain.legal_numbers import chinese_number_to_int

from ..domain.citation import (
    ArticleRef,
    LegalSource,
    LegalSourceType,
    StructureRef,
    StructureUnit,
)
from .jurisdiction import detect_jurisdiction
from .law_lexicon import LawLexicon


# ============================================================
# 正则模式
# ============================================================

# 法源引用：《...》书名号对
# 支持中文书名号和全角书名号
# 注意：书名号内文本可能包含空格、标点、数字等
LEGAL_SOURCE_PATTERN = re.compile(r"《([^》]+)》")

# 国家标准/行业标准模式：GB/T XXXXX-XXXX 等（无书名号）
# 例：GB/T 35273-2020 / GB/T 45674-2025 / GB 12345-2020
# 标准编号本身就是唯一标识，不需要《》
STANDARD_PATTERN = re.compile(
    r'(GB(?:/T|/Z)?|GM/T|GA/T|GY/T|LD/T|MZ/T|NY/T|HJ/T|HJ|'
    r'YY/T|YY|WS/T|WS|DB\d{2}/T|DB\d{2})'
    r'\s*\d{4,6}(?:\.\d+)?[—\-]\d{2,4}'
)

# 条款号正则
# 条：第X条 或 第X条之Y
# 支持中文数字（一～十百千）和阿拉伯数字（0-9）
_CN_NUM = r"[一二三四五六七八九十百千零\d]+"
_CN_NUM_EXTRA = r"[一二三四五六七八九十]+"  # "之"后面的数字通常较小

ARTICLE_PATTERN = re.compile(
    rf"第({_CN_NUM})条(?:之({_CN_NUM_EXTRA}))?"
)

# 条号范围：第X条至第Y条
ARTICLE_RANGE_PATTERN = re.compile(
    rf"第({_CN_NUM})条至第({_CN_NUM})条"
)

# 款：第X款
PARAGRAPH_PATTERN = re.compile(
    rf"第({_CN_NUM})款"
)

# 项：第（X）项 / 第(X)项 / 第X项
ITEM_PATTERN = re.compile(
    rf"第[（(]?({_CN_NUM})[）)]?项"
)

# 法源引导词：用于辅助判断句子是否含法律依据
LEGAL_BASIS_WORDS = {"依据", "根据", "依照", "按照", "参照", "适用"}

_PARTIAL_REFERENCE_PREDICATE = re.compile(r"(?:之规定|规定|所称|明确)")


@dataclass(frozen=True)
class PartialArticleRef:
    """尚需从上文补齐条号的款、项引用。"""

    paragraphs: list[str] = field(default_factory=list)
    items: list[str] = field(default_factory=list)


# ============================================================
# 法律规范后缀白名单
# ============================================================
#
# 白名单内的标题才被识别为法律规范——宁紧勿松。
# 白名单外的《》书名号文本（如《※※收藏》《某某号MCN运营手册》
# 《法院回复函》《XX合同》等）一律不作为法源。
#
# 后缀来源：
#   - 《行政法规制定程序条例》(国务院令第321号) 第5条
#   - 《规章制定程序条例》
#   - 《党政机关公文处理工作条例》
#
# 排序注意：长后缀排在前面，避免"办法"被"法"误匹配。
# 如"实施办法"必须优先于"办法"检查。

LEGAL_TITLE_SUFFIXES = [
    # Law 级
    "法典",

    # 暂行办法/规定/规则 等（须优先于"法"匹配）
    "暂行实施办法",
    "暂行实施细则",
    "暂行条例",
    "暂行规定",
    "暂行规则",
    "暂行细则",
    "暂行通知",
    "暂行意见",
    "暂行标准",
    "暂行规程",

    # 试行办法/规定/规则 等
    "试行实施办法",
    "试行实施细则",
    "试行条例",
    "试行规定",
    "试行规则",
    "试行细则",
    "试行通知",
    "试行意见",
    "试行标准",
    "试行规程",

    # 实施/施行细则
    "实施办法",
    "实施细则",
    "施行细则",
    "施行办法",

    # 司法解释法定形式
    "解释",     # 最高人民法院关于...的解释
    "批复",     # 最高人民法院关于...的批复

    # 行政法规/规章/规范性文件后缀
    "条例",     # 行政法规、地方性法规
    "办法",     # 规章（不包括"实施办法"/"暂行办法"）
    "规定",     # 行政法规/规章/规范性文件
    "规则",
    "细则",
    "规程",
    "规范",
    "标准",
    "决定",
    "意见",
    "通知",
    "纪要",
    "通告",
    "公告",

    # Law 级 — "法" 放在最后，避免误匹配"办法"
    "法",
]

# 非法律规范关键词——白名单外的标题若有这些词，明确排除
# 注意：此列表只对不在白名单内的标题生效。
# 白名单内的标题（如"劳动合同法"）即使含"合同"也会保留。
NON_LEGAL_KEYWORDS = [
    "合同", "协议", "授权", "确认函", "公证书", "通知书",
    "证据目录", "发票", "订单", "截图", "收据", "催告函",
    "承诺函", "担保函", "询证函", "报价单", "验收单",
    "送货单", "结算单", "对账单", "欠条", "借条",
    "委托书", "声明书", "告知书", "答复书", "申请", "登记表",
    "营业执照", "章程", "股东名册", "出资证明",
    # 平台或企业发布的非法律文件。
    "手册", "公约", "服务协议", "合作政策",
    "运营手册", "运营规范", "入驻协议",
    "回复函", "答复函", "函",
    "账号管理", "用户协议",
]

# ============================================================
# 法源类型推断。
# ============================================================

def infer_source_type(title: str) -> LegalSourceType:
    """
    推断法律规范类型（确定性规则，机关名优先于后缀）。

    简化后只有三类：
      1. judicial_interpretation — 最高法/最高检机关名 或 含"解释""批复"
      2. law — 以"法""法典"结尾（排除"办法"等）
      3. other_normative_document — 白名单内其余全部后缀

    对于含括号注解的标题（如"反不正当竞争法（2019年修正）"），
    先剥离括号再判断，确保能匹配到正确的后缀。

    白名单外的标题不会进入此函数（由 _is_legal_source 过滤）。
    因此此函数不做 unknown 处理。

    Args:
        title: 法规名称（不含书名号）

    Returns:
        规范类型（绝不会返回 unknown）
    """
    # 使用剥离括号注解后的标题做判断
    check_title = _strip_parenthetical(title)

    # 规则1：司法解释（机关名 或 关键词优先）
    if ("最高人民法院" in check_title
            or "最高人民检察院" in check_title
            or "解释" in check_title
            or "批复" in check_title):
        return LegalSourceType.JUDICIAL_INTERPRETATION

    # 规则2：以"法"或"法典"结尾（排除"办法""实施办法""暂行办法"等）
    if check_title.endswith("法典") or _is_law_suffix(check_title):
        return LegalSourceType.LAW

    # 规则3：其余在白名单内的 → other_normative_document
    return LegalSourceType.OTHER_NORMATIVE_DOCUMENT


def _is_law_suffix(title: str) -> bool:
    """
    判断标题是否以"法"结尾且不属于规章后缀。

    "办法""实施办法""暂行办法""试行办法"等尽管以"法"结尾但不是法律。
    """
    # 先剥离括号注解
    title = _strip_parenthetical(title)
    if not title.endswith("法"):
        return False
    # 排除以"办法"结尾的（已在 LEGAL_TITLE_SUFFIXES 中列为独立项）
    if title.endswith("办法"):
        return False
    return True


# ============================================================
# 法源和条款识别
# ============================================================

def _has_legal_title_suffix(title: str) -> bool:
    """
    判断标题是否具有法律规范后缀（白名单匹配）。

    处理括号注解：如"反不正当竞争法（2019年修正）"先剥离"（2019年修正）"
    再检查"反不正当竞争法"的后缀。

    只有后缀在白名单 LEGAL_TITLE_SUFFIXES 中的标题才被识别为法源。
    白名单外的书名号文本（文章标题、平台文档、合同名、作品名等）
    一律过滤。

    仅白名单中的规范性文件后缀会被识别。

    Args:
        title: 书名号内文本

    Returns:
        True 如果标题后缀在白名单内
    """
    # 先检查原始标题
    for suffix in LEGAL_TITLE_SUFFIXES:
        if title.endswith(suffix):
            return True

    # 剥离括号注解后重试
    # 例："反不正当竞争法（2019年修正）" → "反不正当竞争法"
    # 例："商标法（修订）" → "商标法"
    stripped = _strip_parenthetical(title)
    if stripped != title:
        for suffix in LEGAL_TITLE_SUFFIXES:
            if stripped.endswith(suffix):
                return True

    return False


def _strip_parenthetical(title: str) -> str:
    """
    剥离标题末尾的括号注解。

    例：
      "反不正当竞争法（2019年修正）" → "反不正当竞争法"
      "商标法（修订）" → "商标法"
      "公司法（2023修订）" → "公司法"

    Args:
        title: 原始标题

    Returns:
        剥离后的标题
    """
    # 匹配末尾的括号注解：（...）或（...）
    return re.sub(r'[（(][^）)]*[）)]$', '', title).strip()


def _is_legal_source(title: str) -> bool:
    """
    判断书名号内文本是否为法律规范文件。

    采用后缀白名单机制：
      1. 标题后缀在白名单内 → 直接保留（如"劳动合同法"含"合同"也保留）
      2. 标题后缀不在白名单内 → 检查 NON_LEGAL_KEYWORDS
         （进一步排除合同、协议、手册、公约、回复函等）

    Args:
        title: 书名号内文本

    Returns:
        True 如果是法律规范文件
    """
    # 后缀白名单匹配 → 直接保留
    if _has_legal_title_suffix(title):
        return True

    # 不在白名单内 → 非法律规范文件
    # 但有些可能尚未收录到白名单，用关键词做二次确认
    for keyword in NON_LEGAL_KEYWORDS:
        if keyword in title:
            return False

    # 白名单外且无排除关键词 → 仍不作为法源（宁紧勿松）
    # 识别层只输出明确法源，不引入不确定状态。
    return False


def _extract_articles_from_text(text: str) -> list[ArticleRef]:
    """
    从文本中提取所有条款号引用。

    提取条、款、项，并建立归属关系。
    返回 ArticleRef 列表，每个 ArticleRef 包含条号及对应的款和项。

    款和项仅归属到它前面最近的条号；遇到下一个条号即停止。

    Args:
        text: 待分析的文本片段（通常是两个书名号之间的文本）

    Returns:
        ArticleRef 列表
    """
    # 查找所有"条"引用
    article_matches = list(ARTICLE_PATTERN.finditer(text))
    # 查找所有"款"引用
    paragraph_matches = list(PARAGRAPH_PATTERN.finditer(text))
    # 查找所有"项"引用
    item_matches = list(ITEM_PATTERN.finditer(text))

    if not article_matches:
        # 没有明确的条款号，返回空列表
        # 法源仍会被保留（articles 为空）
        return []

    articles: list[ArticleRef] = []
    for index, am in enumerate(article_matches):
        article_num = am.group(1)
        suffix = am.group(2)
        if suffix:
            article_text = f"第{article_num}条之{suffix}"
        else:
            article_text = f"第{article_num}条"

        article_end = am.end()
        next_article_start = (
            article_matches[index + 1].start()
            if index + 1 < len(article_matches)
            else len(text)
        )

        # 收集属于此条号的款（位于此条之后、下一条之前或文本末尾）
        paras: list[str] = []
        for pm in paragraph_matches:
            if article_end <= pm.start() < next_article_start:
                paras.append(f"第{pm.group(1)}款")

        # 收集属于此条号的项
        items: list[str] = []
        for im in item_matches:
            if article_end <= im.start() < next_article_start:
                items.append(f"第{im.group(1)}项")

        articles.append(ArticleRef(
            article=article_text,
            paragraphs=paras,
            items=items,
        ))

    # "第X条至第Y条"范围展开：补全被跳过的中间条号
    # （ARTICLE_PATTERN 只命中范围的两端，如"第四十三条至第四十五条"
    #   会漏掉第四十四条）
    existing = {a.article for a in articles}
    for rm in ARTICLE_RANGE_PATTERN.finditer(text):
        start = chinese_number_to_int(rm.group(1))
        end = chinese_number_to_int(rm.group(2))
        if start is None or end is None or not (0 < start < end and end - start <= 50):
            continue
        start_text = f"第{rm.group(1)}条"
        insert_at = next(
            (index + 1 for index, article in enumerate(articles) if article.article == start_text),
            len(articles),
        )
        for number in range(start + 1, end):
            article_text = f"第{_int_to_cn_num(number)}条"
            if article_text not in existing:
                existing.add(article_text)
                articles.insert(insert_at, ArticleRef(article=article_text))
                insert_at += 1

    return articles


def _int_to_cn_num(value: int) -> str:
    """将整数转换为中文数字，支持法条常用的 1 至 9999。"""
    if value <= 0 or value > 9999:
        return str(value)
    digits = "零一二三四五六七八九"
    parts = []
    thousands, rest = divmod(value, 1000)
    hundreds, rest = divmod(rest, 100)
    tens, ones = divmod(rest, 10)
    if thousands:
        parts.append(digits[thousands] + "千")
    if hundreds:
        parts.append(digits[hundreds] + "百")
    elif thousands and (tens or ones):
        parts.append("零")
    if tens:
        # "一十X" 习惯写作 "十X"（仅当没有更高位时）
        if tens == 1 and not thousands and not hundreds:
            parts.append("十")
        else:
            parts.append(digits[tens] + "十")
    elif (thousands or hundreds) and ones:
        parts.append("零")
    if ones:
        parts.append(digits[ones])
    return "".join(parts)


def extract_legal_sources(
    text: str,
    lexicon: LawLexicon | None = None,
) -> list[LegalSource]:
    """
    从文本中提取所有法律规范引用。

    找所有《》书名号对，过滤非法律规范文件，
    提取每个法源后的条款号并建立归属关系。

    条款号归属规则：
      - 条款号必须出现在对应法源书名号之后、下一个法源书名号之前
      - 法源后无条款号时（如"依据《民法典》及相关规定"），仍构成法源，articles 为空

    Args:
        text: 待分析的文本（通常是一个 anchor 的文本）

    Returns:
        LegalSource 列表，按原文出现顺序排列
    """
    # 查找所有《》引用
    matches = list(LEGAL_SOURCE_PATTERN.finditer(text))
    lexicon = lexicon or LawLexicon.load()
    bare_matches = _find_bare_citations(text, lexicon)

    legal_sources: list[LegalSource] = []

    if matches:
        for i, m in enumerate(matches):
            title = m.group(1).strip()
            if not title or not _is_legal_source(title):
                continue

            # 确定条款号搜索范围：从当前法源结束位置到下一个法源开始位置
            search_start = m.end()
            if i + 1 < len(matches):
                search_end = matches[i + 1].start()
            else:
                search_end = len(text)
            next_bare_start = next(
                (bare.title_start for bare in bare_matches if search_start <= bare.title_start < search_end),
                None,
            )
            if next_bare_start is not None:
                search_end = next_bare_start

            segment = text[search_start:search_end]

            # 从该区间提取条款号
            articles = _extract_articles_from_text(segment)

            source_type = infer_source_type(title)

            lexicon_match = lexicon.longest_suffix_match(title)
            canonical_title = (
                lexicon_match.canonical_title
                if lexicon_match is not None and lexicon_match.start == 0
                else title
            )
            legal_sources.append(LegalSource(
                title=title,
                canonical_title=canonical_title,
                source_type=source_type,
                jurisdiction=detect_jurisdiction(title, text[:m.start()]),
                articles=articles,
                # 章节引用只在无条款引用时抽取（有条号时章节仅是定位前缀）
                structures=(
                    _extract_structure_refs(segment) if not articles else []
                ),
            ))

    # ---- 补充：裸法条引用（无《》书名号）----
    # 例："……认定为反不正当竞争法第九条第四款所称的……"
    # 司法解释经常引用其解释的基础法律，且不加书名号。
    bare_sources = _extract_bare_law_citations(bare_matches)
    for source in bare_sources:
        key = source.canonical_title or source.title
        existing = next(
            (
                candidate for candidate in legal_sources
                if (candidate.canonical_title or candidate.title) == key and key
            ),
            None,
        )
        if existing is None:
            legal_sources.append(source)
            continue
        _merge_articles(existing.articles, source.articles)

    # ---- 补充：国家标准/行业标准（无书名号）----
    # 例：GB/T 35273-2020 / GB/T 45674-2025
    # 标准编号本身就是唯一标识，归入 other_normative_document
    seen_titles = {source.canonical_title or source.title for source in legal_sources if source.title}
    standard_sources = _extract_standard_citations(text, seen_titles)
    legal_sources.extend(standard_sources)

    return legal_sources


# 章节引用链：紧跟在《法名》之后的 第X编/分编/章/节 连写（无条号时）
_STRUCTURE_CHAIN_PATTERN = re.compile(
    r"^[\s　的]*((?:第[一二三四五六七八九十百千零两0-9]+(?:编|分编|章|节))+)"
)
_STRUCTURE_UNIT_PATTERN = re.compile(
    r"第([一二三四五六七八九十百千零两0-9]+)(编|分编|章|节)"
)


def _extract_structure_refs(segment: str) -> list[StructureRef]:
    """从法名后的紧邻文本抽取章节引用（如"第三编第四章"）。"""
    match = _STRUCTURE_CHAIN_PATTERN.match(segment)
    if not match:
        return []
    chain = match.group(1)
    units = []
    for unit_match in _STRUCTURE_UNIT_PATTERN.finditer(chain):
        number = chinese_number_to_int(unit_match.group(1))
        units.append(StructureUnit(
            unit=unit_match.group(2),
            number=number,
            number_text=unit_match.group(0),
        ))
    if not units:
        return []
    return [StructureRef(label=chain, units=units)]


# ============================================================
# 裸法条引用（无《》书名号）
# ============================================================

# 裸法条引用模式：XX法第X条 / XX法典第X条（无书名号包裹）
# 例："……认定为反不正当竞争法第九条第四款所称的……"
# 司法解释经常引用其解释的基础法律且不加书名号。
# 约束：
#   1. 法名以"法"或"法典"结尾（非贪婪匹配，避免吞掉谓语前缀）
#   2. 法名前必须是边界词（标点/谓语动词/句首），防止"人民法院→反不正当竞争法"
#   3. 法名后紧跟条款号引用（第X条）
#
# 边界词：这些词/标点后的"XX法"被视为法名起点
BARE_ARTICLE_ANCHOR = re.compile(
    r'(?P<law_suffix>法典|法)'
    r'(?P<article>'
    r'第[一二三四五六七八九十百千零\d]+条'
    r'(?:之[一二三四五六七八九十]+)?'
    r'(?:第[一二三四五六七八九十零\d]+款)?'
    r'(?:第[（(]?[一二三四五六七八九十零\d]+[）)]?项)?'
    r')'
)

# 伪法名后缀：以"法"结尾但不是法律名的词
BARE_LAW_EXCLUDE_SUFFIXES = [
    "办法", "方法", "做法", "手法", "司法", "历法", "语法",
    "书法", "笔法", "技法", "用法", "玩法", "疗法",
    "说法", "看法", "想法", "算法", "写法", "读法",
    "乘法", "除法", "加法", "减法",
    # 指代词（代指前文提到的法律，不是独立的法律名）
    "本法", "该法", "此法", "前法", "上述法律",
    # 非特指法律的通用词
    "现行法", "相关法", "有关法", "其他法",
]


@dataclass(frozen=True)
class BareCitationMatch:
    title: str
    canonical_title: str | None
    raw_title_candidate: str | None
    title_start: int
    title_end: int
    citation_end: int
    article_text: str
    resolution: str


_BARE_WINDOW_BOUNDARY = re.compile(r"[，。！？；：、\n]")


def _find_bare_citations(text: str, lexicon: LawLexicon) -> list[BareCitationMatch]:
    results: list[BareCitationMatch] = []
    previous_end = 0
    window_limit = max(80, lexicon.max_surface_length + 16)
    for anchor in BARE_ARTICLE_ANCHOR.finditer(text):
        law_end = anchor.end("law_suffix")
        window_start = max(previous_end, law_end - window_limit)
        prefix = text[window_start:law_end]
        boundaries = [match.end() for match in _BARE_WINDOW_BOUNDARY.finditer(prefix)]
        book_end = prefix.rfind("》") + 1
        relative_start = max([0, book_end, *boundaries])
        window_start += relative_start
        window = text[window_start:law_end]

        matched = lexicon.longest_suffix_match(window, offset=window_start)
        if matched is not None:
            results.append(BareCitationMatch(
                title=matched.surface_title,
                canonical_title=matched.canonical_title,
                raw_title_candidate=None,
                title_start=matched.start,
                title_end=matched.end,
                citation_end=anchor.end(),
                article_text=anchor.group("article"),
                resolution="bare_lexicon",
            ))
            previous_end = anchor.end()
            continue

        raw = window.strip()
        if not raw or not _is_valid_bare_law_name(raw):
            previous_end = anchor.end()
            continue
        # 未解析引用只记录确定的右锚位置，不猜测法名左边界。
        results.append(BareCitationMatch(
            title="",
            canonical_title=None,
            raw_title_candidate=raw,
            title_start=anchor.start("law_suffix"),
            title_end=law_end,
            citation_end=anchor.end(),
            article_text=anchor.group("article"),
            resolution="bare_unresolved",
        ))
        previous_end = anchor.end()
    return results


def _extract_bare_law_citations(matches: list[BareCitationMatch]) -> list[LegalSource]:
    results: list[LegalSource] = []
    by_key: dict[str, LegalSource] = {}
    for match in matches:
        articles = _extract_articles_from_text(match.article_text)
        for article in articles:
            article.source_span = (match.title_start, match.title_end)
        key = match.canonical_title or f"unresolved:{match.title_start}:{match.citation_end}"
        existing = by_key.get(key)
        if existing is not None:
            _merge_articles(existing.articles, articles)
            continue
        source = LegalSource(
            title=match.title,
            canonical_title=match.canonical_title,
            raw_title_candidate=match.raw_title_candidate,
            source_span=(match.title_start, match.title_end),
            source_type=(
                infer_source_type(match.canonical_title or match.title)
                if match.canonical_title or match.title
                else LegalSourceType.LAW
            ),
            articles=articles,
            resolution=match.resolution,
        )
        results.append(source)
        by_key[key] = source
    return results


def _merge_articles(target: list[ArticleRef], incoming: list[ArticleRef]) -> None:
    for article in incoming:
        existing = next((
            item for item in target
            if item.article == article.article
            and (
                item.source_span == article.source_span
                or item.source_span is None
                or article.source_span is None
            )
        ), None)
        if existing is None:
            target.append(article)
            continue
        existing.paragraphs = list(dict.fromkeys([*existing.paragraphs, *article.paragraphs]))
        existing.items = list(dict.fromkeys([*existing.items, *article.items]))


def _is_valid_bare_law_name(title: str) -> bool:
    """
    判断裸法名是否为有效法律名称。

    排除：
      - 以伪法名后缀结尾的（办法、方法、做法等）
      - 过短的（<3 字，如单独的"法"）

    Args:
        title: 待检查的法名

    Returns:
        True 如果是有效法律名称
    """
    if len(title) < 3:
        return False
    for exclude in BARE_LAW_EXCLUDE_SUFFIXES:
        if title.endswith(exclude):
            return False
    return True


def has_article_reference(text: str) -> bool:
    """
    判断文本是否包含条款号引用（条/款/项）但不一定含《》法源名。

    用于法源前向继承：当前 anchor 有条款号但无法源名时，
    向上查找最近的法源引用 anchor，继承其法源。

    Args:
        text: 待检查文本

    Returns:
        True 如果包含条款号引用
    """
    if ARTICLE_PATTERN.search(text):
        return True
    if PARAGRAPH_PATTERN.search(text):
        return True
    if ITEM_PATTERN.search(text):
        return True
    return False


def extract_articles_only(text: str) -> list:
    """
    从文本中提取条款号但不要求存在《》法源。

    用于法源前向继承场景：条款号会继承前一个 anchor 的法源名。

    Args:
        text: 待分析文本

    Returns:
        ArticleRef 列表
    """
    return _extract_articles_from_text(text)


def extract_partial_refs(text: str) -> PartialArticleRef | None:
    """保守提取省略条号的款、项；结果不能脱离承前法源独立使用。"""
    if ARTICLE_PATTERN.search(text):
        return None
    paragraph_matches = list(PARAGRAPH_PATTERN.finditer(text))
    item_matches = list(ITEM_PATTERN.finditer(text))
    if not paragraph_matches and not item_matches:
        return None

    last_end = max(match.end() for match in [*paragraph_matches, *item_matches])
    predicate_window = text[last_end:last_end + 8]
    has_legal_predicate = bool(_PARTIAL_REFERENCE_PREDICATE.search(predicate_window))
    if not has_legal_predicate and not any(word in text for word in LEGAL_BASIS_WORDS):
        return None

    return PartialArticleRef(
        paragraphs=[f"第{match.group(1)}款" for match in paragraph_matches],
        items=[f"第{match.group(1)}项" for match in item_matches],
    )


def has_legal_basis_words(text: str) -> bool:
    """
    检测句子是否包含法源引导词。

    引导词：依据、根据、依照、按照、参照、适用

    Args:
        text: 句子文本

    Returns:
        True 如果包含引导词
    """
    for word in LEGAL_BASIS_WORDS:
        if word in text:
            return True
    return False


# ============================================================
# 国家标准/行业标准引用（无书名号）
# ============================================================

def _extract_standard_citations(
    text: str,
    seen_titles: set[str],
) -> list[LegalSource]:
    """
    提取国家标准/行业标准引用（无《》书名号形式）。

    标准编号如 GB/T 35273-2020、GB/T 45674-2025 本身即是唯一标识，
    归入 other_normative_document。

    Args:
        text: 待分析文本
        seen_titles: 已通过《》或裸引用提取过的标题集合（避免重复）

    Returns:
        LegalSource 列表
    """
    results: list[LegalSource] = []
    seen: set[str] = set()

    for m in STANDARD_PATTERN.finditer(text):
        std_id = m.group(0).strip()

        # 去重
        if std_id in seen_titles or std_id in seen:
            continue
        seen.add(std_id)

        # 尝试提取标准名称（紧跟在编号后的中文描述）
        # 例："GB/T 35273-2020    信息安全技术 个人信息安全规范"
        # 标准名称部分不做强制要求，有则更好
        results.append(LegalSource(
            title=std_id,
            source_type=LegalSourceType.OTHER_NORMATIVE_DOCUMENT,
            articles=[],
            resolution="explicit",
        ))

    return results
