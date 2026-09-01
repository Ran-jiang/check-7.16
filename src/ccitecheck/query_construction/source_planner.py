"""按法域生成数据源宏观计划；执行顺序由 Scheduler 消费。"""

from ..domain.queries import SourcePlanItem


def plan_sources(jurisdiction: str) -> list[SourcePlanItem]:
    if jurisdiction == "EU":
        return [SourcePlanItem(source_id="eurlex", priority=1, reason="eu_statute")]
    if jurisdiction == "CN":
        return [
            SourcePlanItem(source_id="local_laws", priority=1, reason="local_authoritative_copy"),
            SourcePlanItem(source_id="pkulaw", priority=2, reason="authoritative_fallback"),
        ]
    return []


__all__ = ["plan_sources"]
