# 条号候选再提名

文书引用的条号与其转述内容疑似不对应。系统已按你此前提出的候选条号取回原文验证，均与文书内容不符。请基于全部已验证信息，提出下一个候选条号。

输入包含：

- `law_title`：法律名称；
- `document_quote`：文书原句；
- `cited_article`：文书写明的条号及该条现行原文（已确认与文书内容不对应）；
- `tried_candidates`：已验证过的候选列表，每项含条号、该条现行原文（或"未收录"）与不匹配说明。

## 任务

判断 `document_quote` 转述的规则实际对应 `law_title` 中的哪一条。答案必须与 `cited_article` 及所有 `tried_candidates` 都不同。只在有实质把握时提名；已无把握时返回 `null`，不得为了给出答案而猜测。

## 输出

仅输出一个可由 `JSON.parse()` 解析的 JSON 对象：

{"candidate_article_no":"第X条"|null,"reason":"一句话说明提名依据或放弃原因"}

- `candidate_article_no` 格式必须严格为“第X条”，X 用与法规原文一致的汉字数字（如“第一百八十八条”）；
- `reason` 面向系统日志，简短说明即可。
