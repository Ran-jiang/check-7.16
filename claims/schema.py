"""
CCitecheck v0.2 数据结构定义。

本模块使用 Pydantic v2 定义可验证主张识别层的所有数据结构。

核心设计决策（代码层面体现）：
  - claim.text 由系统从 anchors 重建，不由抽取器提供
    → Claim 不含 text 参数，由 arbiter 在构建时填入重建文本
  - entities 按 claim_type 使用 pydantic 子模型，不用 dict[str, Any]
    → 序列化后仍是 JSON object，同时获得类型安全和校验
  - ClaimCandidate 是内部中间类型，不进入最终 JSON
    → 抽取器产出 Candidate，必须经 Arbiter 裁决后才成为 Claim
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Union
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class VerificationRoute(str, Enum):
    """
    核查路由 — claim 级别的粗粒度路由。

    注意：这是 claim 级粗路由。混合法源时（如既有法律又有司法解释），
    后续核查模块按 entities 内每个法源自带的 source_type 细分流，
    这正是 LegalSourceType 必须保留的原因。
    """
    STATUTE_DATABASE = "statute_database"
    JUDICIAL_INTERPRETATION_DATABASE = "judicial_interpretation_database"
    CASE_DATABASE_EXACT = "case_database_exact"
    CASE_DATABASE_SEARCH = "case_database_search"
    CASE_DATABASE_FULLTEXT = "case_database_fulltext"


class LegalSourceType(str, Enum):
    """
    法律规范类型 — v0.2 简化版。

    只保留三个值，对应明确可路由的数据库：
      - law: 法律（全国人大及其常委会制定）
      - judicial_interpretation: 司法解释（最高法/最高检）
      - other_normative_document: 行政法规、部门规章、地方性法规、
        地方政府规章、规范性文件等所有其他法律规范

    后缀→类型映射及推断规则见 legal_citation.infer_source_type。
    后缀白名单完整文档见 README v0.2 章节。
    """
    LAW = "law"
    JUDICIAL_INTERPRETATION = "judicial_interpretation"
    OTHER_NORMATIVE_DOCUMENT = "other_normative_document"


class CaseReferenceType(str, Enum):
    """案例引用类型"""
    WITH_CASE_NUMBER = "with_case_number"
    WITHOUT_CASE_NUMBER = "without_case_number"


class ExtractionMethod(str, Enum):
    """抽取方法来源"""
    RULE = "rule"
    LLM = "llm"


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
    """case_citation 的实体"""
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
    model = {
        ClaimType.LEGAL_SOURCE_CLAIM: LegalSourceClaimEntities,
        ClaimType.CASE_CITATION: CaseCitationEntities,
        ClaimType.CASE_HOLDING_PARAPHRASE: CaseHoldingParaphraseEntities,
        ClaimType.LEGAL_SOURCE_CLAIM.value: LegalSourceClaimEntities,
        ClaimType.CASE_CITATION.value: CaseCitationEntities,
        ClaimType.CASE_HOLDING_PARAPHRASE.value: CaseHoldingParaphraseEntities,
    }.get(claim_type)
    if model is not None:
        data = dict(data)
        data["entities"] = model.model_validate(raw_entities)
    return data


# ============================================================
# 内部中间类型：ClaimCandidate（不进入最终 JSON）
# ============================================================

class ClaimDebug(BaseModel):
    """
    claim 的调试信息。

    记录该 claim 的候选来源和交叉比对结果。

    methods 使用 list[str] 而非 list[ExtractionMethod]，因为：
      - 去重合并时 methods 来自不同候选，需要灵活拼接
      - 最终校验由 validate_claim_document 确保值仅含 rule/llm
    """
    methods: list[str] = Field(
        default_factory=list,
        description="抽取方法来源列表"
    )
    candidate_count: int = Field(
        default=0,
        description="合并前的候选数量"
    )
    text_mismatch: bool = Field(
        default=False,
        description="LLM 返回文本与系统重建文本是否不一致"
    )


class ClaimCandidate(BaseModel):
    """
    抽取器产出的中间候选。

    这是内部类型，不直接进入最终 ClaimDocument JSON。
    所有候选必须经过 Claim Arbiter 裁决后才成为 Claim。

    抽取器只负责回答"哪些 anchor 构成一个 claim、是什么类型、含哪些实体"。
    claim.text 由 arbiter 从 anchor 重建，不由抽取器提供。
    llm_text 仅作交叉比对用。
    """
    claim_type: ClaimType = Field(description="主张类型")
    anchor_ids: list[str] = Field(description="anchor 编号列表")
    entities: ClaimEntities = Field(description="实体信息（各 claim_type 的对应子模型）")
    method: ExtractionMethod = Field(description="抽取方法")
    chunk_id: Optional[str] = Field(
        default=None,
        description="LLM 候选的来源 chunk_id（规则候选为 None）"
    )
    llm_text: Optional[str] = Field(
        default=None,
        description="LLM 返回的文本（仅作交叉比对，不以之作为最终 text）"
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @model_validator(mode="before")
    @classmethod
    def restore_entity_type(cls, data):
        return _coerce_entities(data)


# ============================================================
# 最终输出类型：Claim 和 ClaimDocument
# ============================================================

class Claim(BaseModel):
    """
    最终的可验证主张。

    text 由 Claim Arbiter 从 anchor_ids 对应的 anchor.text 按序拼接重建。
    这是"不改写原文"的结构性保证——claim.text 永远等于原文锚点文本的精确拼接。

    block_ids 由 anchor.block_id 派生，仅作溯源，不参与 claim 判断。
    """
    claim_id: str = Field(description="claim 唯一 ID，格式 cl_00001")
    claim_type: ClaimType = Field(description="主张类型")
    text: str = Field(description="从 anchors 重建的完整主张文本")
    anchor_ids: list[str] = Field(description="anchor 编号列表")
    block_ids: list[str] = Field(description="派生 block ID 列表")
    verification_route: VerificationRoute = Field(description="核查路由")
    entities: ClaimEntities = Field(description="实体信息")
    context_text: str = Field(
        default="",
        description="主张所在语义块的上下文；仅供检索与引用忠实度比对",
    )
    location_text: str = Field(
        default="",
        description="用于 Word 精确定位的原文；表格配对引用定位到内容单元格",
    )
    debug: ClaimDebug = Field(
        default_factory=ClaimDebug,
        description="调试信息"
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @model_validator(mode="before")
    @classmethod
    def restore_entity_type(cls, data):
        return _coerce_entities(data)


class ClaimMeta(BaseModel):
    """claim 文档元信息"""
    schema_version: str = Field(default="0.2", description="schema 版本号")
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
    llm_used: bool = Field(default=False, description="是否使用了 LLM 抽取器")
    llm_chunk_failures: list[str] = Field(
        default_factory=list,
        description="LLM 调用失败的 chunk_id 列表"
    )


class ClaimDocument(BaseModel):
    """
    CCitecheck v0.2 产出的顶层结构。

    每次抽取视为一次快照（snapshot）。
    不包含 anchor_range、source_anchor_ids、verification_status、
    normalized_text、confidence、primary_method、needs_review 等字段。
    """
    claim_meta: ClaimMeta = Field(default_factory=ClaimMeta)
    claims: list[Claim] = Field(default_factory=list)
