"""
CCiteheck 引用识别领域模型。

本模块使用 Pydantic v2 定义可验证主张识别层的所有数据结构。

核心设计决策（代码层面体现）：
  - claim.text 由系统从 anchors 重建，不由抽取器提供
    → ClaimCandidate 不含 text，由 arbiter 构建 Claim 时填入重建文本
  - entities 按 claim_type 使用 pydantic 子模型，不用 dict[str, Any]
    → 序列化后仍是 JSON object，同时获得类型安全和校验
  - ClaimCandidate 是内部中间类型，不进入最终 JSON
    → 抽取器产出 Candidate，必须经 Arbiter 裁决后才成为 Claim
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional, Union
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


# ============================================================
# 枚举定义
# ============================================================

class ClaimType(str, Enum):
    """
    可验证主张类型。

    legal_source_claim: 含明确法律规范引用的完整主张
        - 句子中有《XX法》第X条等规范引用
        - 即使后半句包含法律判断（如"被告应当承担违约责任"），也抽取为此类型
        - 理由：后续至少需要检索该法条并返回原文

        - 法条号后紧跟"规定/明确/指出/载明"等触发词
        - 触发词后有实体转述内容

    case_citation: 案例引用
        - 有明确案号（如（2021）最高法民申1234号）
        - 或明确可检索线索（指导案例第X号、公报案例、X诉Y案等）

    case_holding_paraphrase: 带明确案例依据的裁判观点转述
        - 同一句中既有案例引用，又有"认为/指出/裁判要旨"等观点触发词
    """
    LEGAL_SOURCE_CLAIM = "legal_source_claim"
    CASE_CITATION = "case_citation"
    CASE_HOLDING_PARAPHRASE = "case_holding_paraphrase"


class LegalSourceType(str, Enum):
    """
    法律规范类型。

    只保留三个值，对应明确可路由的数据库：
      - law: 法律（全国人大及其常委会制定）
      - judicial_interpretation: 司法解释（最高法/最高检）
      - other_normative_document: 行政法规、部门规章、地方性法规、
        地方政府规章、规范性文件等所有其他法律规范

    后缀到类型的映射及推断规则见 recognition.statutes.infer_source_type。
    """
    LAW = "law"
    JUDICIAL_INTERPRETATION = "judicial_interpretation"
    OTHER_NORMATIVE_DOCUMENT = "other_normative_document"


class CaseReferenceType(str, Enum):
    """案例引用类型"""
    WITH_CASE_NUMBER = "with_case_number"
    WITHOUT_CASE_NUMBER = "without_case_number"


# ============================================================
# 实体子模型（按 claim_type 分别定义）
# ============================================================

class ArticleRef(BaseModel):
    """
    条款引用。

    article 保留"之一"等后缀，如"第一百八十四条之一"。
    条款号格式由规则抽取器保证。
    """
    article: str = Field(description="条款文本，如'第四十八条'、'第一百八十四条之一'")
    paragraphs: list[str] = Field(
        default_factory=list,
        description="款号列表，如['第一款', '第二款']"
    )
    items: list[str] = Field(
        default_factory=list,
        description="项号列表，如['第（一）项']"
    )
    mention_span: tuple[int, int] | None = None
    citation_span: tuple[int, int] | None = None
    quote_span: tuple[int, int] | None = None
    reference_role: Literal["direct", "nested", "inherited"] = "direct"
    parent_reference_id: tuple[str, str] | None = None
    span_status: Literal["located", "fallback", "error"] = "fallback"


class LegalSource(BaseModel):
    """
    法律规范来源。

    title 为书名号内文本，不含书名号本身。
    source_type 由 infer_source_type 确定性推断。

    resolution 标记法源的识别方式：
      - "explicit"：当前句内直接出现《》书名号引用
      - "inherited"：当前句只有条款号，法源名来自前向继承（承前省略法源名）
    inherited_from_anchor 仅当 resolution="inherited" 时填写，
    记录法源名来自哪个 anchor，方便调试和未来 UI 溯源。
    """
    title: str = Field(description="法规名称，不含书名号")
    source_type: LegalSourceType = Field(description="规范类型")
    articles: list[ArticleRef] = Field(
        default_factory=list,
        description="条款引用列表"
    )
    resolution: str = Field(
        default="explicit",
        description="法源识别方式：explicit（句中《》直接引用）或 inherited（前向继承）"
    )
    inherited_from_anchor: Optional[str] = Field(
        default=None,
        description="继承来源 anchor 编号（仅 resolution='inherited' 时有效）"
    )
    inherited_from_location: Optional["SourceLocation"] = Field(
        default=None,
        description="继承来源的稳定文档定位（仅 resolution='inherited' 时有效）",
    )


class CaseRef(BaseModel):
    """
    案例引用。

    有案号时 reference_type=with_case_number，填 case_number。
    无案号但可通过线索检索时 reference_type=without_case_number，
    填 case_name 或留空。
    """
    reference_type: CaseReferenceType = Field(description="引用类型")
    case_number: Optional[str] = Field(
        default=None,
        description="案号，如'（2021）最高法民申1234号'"
    )
    case_name: Optional[str] = Field(
        default=None,
        description="案例名称"
    )
    court: Optional[str] = Field(
        default=None,
        description="法院名称"
    )


class LegalSourceClaimEntities(BaseModel):
    """
    legal_source_claim 的实体。

    支持多法源（设计决策2.4）：
    一句引用多个法律规范时，全部列入 legal_sources，不拆分。
    """
    legal_sources: list[LegalSource] = Field(
        default_factory=list,
        description="法律规范来源列表"
    )


class CaseCitationEntities(BaseModel):
    """案例引用的实体。"""
    case_refs: list[CaseRef] = Field(
        default_factory=list,
        description="案例引用列表"
    )


class CaseHoldingParaphraseEntities(BaseModel):
    """
    case_holding_paraphrase 的实体。

    holding_text 必须是 claim.text 的子串（由 arbiter 校验）。
    没有明确 case_ref 时绝不抽取此类型——即使出现"法院认为""本院认为"。
    """
    case_refs: list[CaseRef] = Field(
        default_factory=list,
        description="案例引用列表（观点转述通常长度为1）"
    )
    holding_text: str = Field(
        default="",
        description="观点转述文本，必须是 claim.text 的子串"
    )


ClaimEntities = Union[
    LegalSourceClaimEntities,
    CaseCitationEntities,
    CaseHoldingParaphraseEntities,
]

_ENTITY_MODEL_BY_CLAIM_TYPE = {
    ClaimType.LEGAL_SOURCE_CLAIM: LegalSourceClaimEntities,
    ClaimType.CASE_CITATION: CaseCitationEntities,
    ClaimType.CASE_HOLDING_PARAPHRASE: CaseHoldingParaphraseEntities,
}


def _coerce_entities(data):
    """按 claim_type 还原实体子模型，保证 JSON 可以无损往返。"""
    if not isinstance(data, dict):
        return data
    raw_entities = data.get("entities")
    if raw_entities is None or isinstance(
        raw_entities,
        (LegalSourceClaimEntities, CaseCitationEntities, CaseHoldingParaphraseEntities),
    ):
        return data
    claim_type = data.get("claim_type")
    try:
        normalized_claim_type = ClaimType(claim_type)
    except (TypeError, ValueError):
        normalized_claim_type = None
    model = _ENTITY_MODEL_BY_CLAIM_TYPE.get(normalized_claim_type)
    if model is not None:
        data = dict(data)
        data["entities"] = model.model_validate(raw_entities)
    return data


# ============================================================
# 内部中间类型：ClaimCandidate（不进入最终 JSON）
# ============================================================

class SourceLocation(BaseModel):
    """平台无关的原文定位坐标，供 Word 或飞书输出适配器解释。"""

    platform: Literal["docx", "feishu"] = "docx"
    document_id: Optional[str] = None
    revision: Optional[str] = None
    block_id: str
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    anchor_text: str = Field(default="", description="用于来源平台按文本自愈定位的 Anchor 原文")
    occurrence: Optional[int] = Field(
        default=None,
        ge=0,
        description="anchor_text 在所属块内从零开始的出现序号，用于多命中消歧",
    )
    table_index: Optional[int] = None
    row_index: Optional[int] = None
    cell_index: Optional[int] = None
    row_start: Optional[int] = None
    row_end: Optional[int] = None
    col_start: Optional[int] = None
    col_end: Optional[int] = None


class ClaimCandidate(BaseModel):
    """
    抽取器产出的中间候选。

    这是内部类型，不直接进入最终 ClaimDocument JSON。
    所有候选必须经过 Claim Arbiter 裁决后才成为 Claim。

    抽取器只负责回答"哪些 anchor 构成一个 claim、是什么类型、含哪些实体"。
    claim.text 由 arbiter 从 anchor 重建，不由抽取器提供。
    """
    claim_type: ClaimType = Field(description="主张类型")
    anchor_ids: list[str] = Field(description="anchor 编号列表")
    entities: ClaimEntities = Field(description="实体信息（各 claim_type 的对应子模型）")

    @model_validator(mode="before")
    @classmethod
    def restore_entity_type(cls, data):
        return _coerce_entities(data)

    @model_validator(mode="after")
    def validate_entity_type(self):
        expected = _ENTITY_MODEL_BY_CLAIM_TYPE[self.claim_type]
        if not isinstance(self.entities, expected):
            raise ValueError(
                f"{self.claim_type.value} 必须使用 {expected.__name__}"
            )
        return self


# ============================================================
# 最终输出类型：Claim 和 ClaimDocument
# ============================================================

class Claim(BaseModel):
    """
    最终的可验证主张。

    text 由 Claim Arbiter 从 anchor_ids 对应的 anchor.text 按序拼接重建。
    这是"不改写原文"的结构性保证——claim.text 永远等于原文锚点文本的精确拼接。

    原文位置由 source_locations 表达；承前法源位置由
    LegalSource.inherited_from_location 表达。
    """
    claim_id: str = Field(description="claim 唯一 ID，格式 cl_00001")
    claim_type: ClaimType = Field(description="主张类型")
    text: str = Field(description="从 anchors 重建的完整主张文本")
    anchor_ids: list[str] = Field(description="anchor 编号列表")
    entities: ClaimEntities = Field(description="实体信息")
    context_text: str = Field(
        default="",
        description="主张所在语义块的上下文；仅供检索",
    )
    source_locations: list[SourceLocation] = Field(
        default_factory=list,
        description="Word 或飞书中的原文定位坐标",
    )
    @model_validator(mode="before")
    @classmethod
    def restore_entity_type(cls, data):
        return _coerce_entities(data)

    @model_validator(mode="after")
    def validate_entity_type(self):
        expected = _ENTITY_MODEL_BY_CLAIM_TYPE[self.claim_type]
        if not isinstance(self.entities, expected):
            raise ValueError(
                f"{self.claim_type.value} 必须使用 {expected.__name__}"
            )
        return self


class ClaimMeta(BaseModel):
    """引用文档元信息。"""
    schema_version: str = Field(default="0.3", description="schema 版本号")
    claim_doc_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="claim 文档唯一 ID（uuid4）"
    )
    source_doc_id: str = Field(default="", description="来源 ParsedDocument 的 doc_id")
    source_doc_hash: str = Field(default="", description="来源文件 SHA-256 摘要")
    source_file: str = Field(default="", description="原始 DOCX 文件名")
    extracted_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="抽取时间（ISO-8601）"
    )
    extractor_version: str = Field(default="0.2", description="抽取器版本")


class ClaimDocument(BaseModel):
    """
    CCiteheck 产出的顶层引用结构。

    每次抽取视为一次快照（snapshot）。
    不包含 anchor_range、source_anchor_ids、verification_status、
    normalized_text、confidence、primary_method、needs_review 等字段。
    """
    claim_meta: ClaimMeta = Field(default_factory=ClaimMeta)
    claims: list[Claim] = Field(default_factory=list)
