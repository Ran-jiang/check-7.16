"""
CCitecheck v0.3 法条检索层：路由器 + 外部数据源。

数据源优先级（router 编排）：
  1. local      — statutedb 本地库（权威、完整、可控）
  2. gov_search — 搜索接口 + site:gov.cn 限定
  3. pkulaw     — 北大法宝 MCP 兜底
"""
