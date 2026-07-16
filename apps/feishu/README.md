# CCiteheck 飞书文档插件

这是独立的飞书文档浮动插件。它读取当前文档块快照，向
`/api/feishu/checks` 提交平台无关的数据，并展示共用核查流水线返回的结果。
插件不导入任何 Word 或 Office 代码。

## 浏览器预览

启动现有 API 服务后访问：

`http://127.0.0.1:3000/feishu-addon/topbar.html?demo=1`

## 飞书配置

1. 在飞书开放平台创建文档小组件。
2. 把 `app.json` 中的 `appID` 和 `blockTypeID` 替换为控制台实际值。
3. 使用 Node.js 18 和 opdev 3.3 以上版本打包并上传本目录。

SDK 入口负责读取标题、正文块、标题层级和表格坐标，并保留稳定块 ID，
用于核查结果定位。浏览器演示通过 `host.js` 隔离 SDK，便于在申请正式
App ID 前测试界面和后端契约。
