// 浏览器演示模式不加载飞书 SDK；opdev 打包飞书插件时会解析该依赖。
const demo = new URLSearchParams(location.search).has("demo")
if (!demo) {
  const { BlockitClient } = await import("@lark-opdev/block-docs-addon-api")
  globalThis.DocMiniApp = new BlockitClient().initAPI()
}
await import("./addon.js")
