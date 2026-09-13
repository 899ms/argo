# ego-search 浏览器运行时参考（低频：复杂交互才需要）


## 直接浏览器操作

复杂交互路径用浏览器运行时命令 `ego-browser nodejs` heredoc 直接编排（本子技能继承完整
运行时）。核心纪律：

```bash
ego-browser nodejs <<'EOF'
// 同名任务空间跨轮次复用；同一用户目标不新建空间
const task = await useOrCreateTaskSpace('竞品调研')
await openOrReuseTab('https://example.com', { wait: true, timeout: 20 })
cliLog(await snapshotText())   // 语义快照，带 [ref=N, loc=..., url=...]
EOF
```

**Helpers 速查**：

- 任务空间：`useOrCreateTaskSpace` / `listTaskSpaces` / `claimTaskSpace` / `handOffTaskSpace` / `takeOverTaskSpace` / `completeTaskSpace`
- 导航：`openOrReuseTab` / `gotoAndWait` / `pageInfo` / `listTabs` / `switchTab`
- 观察：`snapshotText` / `captureScreenshot` / `drainEvents`
- 动作：`click` / `fillInput` / `typeText` / `pressKey` / `scrollToBottomUntil` / `hover` / `uploadFile`
- 等待：`wait` / `waitForElement` / `waitForNetworkIdle`
- 提取：`js`（页面内 JS）/ `cdp`（浏览器协议）
- 输出：`cliLog`（唯一输出通道；`-e` 模式下输出到 stderr）

**关键纪律**：

1. **任务空间隔离 + 登录态继承**：Agent 在独立空间操作，不抢用户标签页；登录态默认继承，可访问已登录站点。
2. **跨轮次复用**：Node 运行时每次 heredoc 退出即释放，后续轮次用 `useOrCreateTaskSpace(同名)` 或
   `takeOverTaskSpace`（用户确认继续后）恢复。
3. **归属权**：用户接管空间时（"user is controlling"）是硬停——问用户并等待，不重试不抢回；
   `handOffTaskSpace` 交还用户后，只有用户明确确认（Ask 的 Continue）才 `takeOverTaskSpace`。
4. **收尾**：任务完成必须 `completeTaskSpace(name, { keep: false })` 关掉空间；
   仅当用户明确要求保留页面/需人工操作/结果无法用 URL 交付时才 `{ keep: true }`。
5. **`js()` 用法**：页面内逻辑包成单个 IIFE 一次返回；`js()` 返回求值结果而非 JSON 字符串，
   不要包 `JSON.parse`；模板字符串里正则反斜杠要双写或 `String.raw`。
6. **输出通道**：heredoc 模式 `cliLog` 输出到 stdout；`ego-browser nodejs -e "..."` 模式输出到 **stderr**。

**三种工作流**：普通 DOM 页用语义流（`snapshotText` + `@N`/`loc=` 引用）；canvas/富编辑器
（Google Docs/Notion/Figma 等）用视觉流（截图 + 坐标 + 键盘）；需要浏览器态/紧凑数据提取用
直接 DOM/CDP 流（`js`/`cdp`）。
