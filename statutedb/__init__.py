"""
CCitecheck v0.3 本地法条库（第一层数据源）。

模块职责：
  - cn_num: 中文数字与条款号标签的双向转换
  - normalizer: 法规名规范化与别名 key 生成
  - law_parser: 官方 Word/txt 法规文本 → StatuteDoc 结构
  - db: SQLite schema 与连接管理
  - importer: StatuteDoc 入库（含别名、FTS、来源哈希）
  - store: 检索 API（法规解析 / 条款直取 / 全文兜底）
"""
