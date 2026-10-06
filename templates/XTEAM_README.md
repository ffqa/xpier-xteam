# .xteam —— xteam 在本项目的工作目录

这个目录由 `xteam` 自动创建和维护，**已在项目 .gitignore 里，不入库**。
它是 agent 的工作区与记忆，不是产品文档。

## 里面有什么

| 路径 | 谁写 | 内容 |
|---|---|---|
| `PROTOCOL.md` | xteam | 协作协议全文（agent 每次上线先读它）|
| `QUEUE.md` | pm/tl | 待办队列，唯一待办真相 |
| `REPORT.md` | pm | 结项报告（全部切片闭合后写，目标/进度/部署/账号/边界） |
| `tasks/<切片>/` | 三个角色 | 一个切片的完整工件链（见 PROTOCOL.md 的状态机）|
| `watch/events.log` | 巡检 | 巡检事件（告警 / 卡死 / 升级）|
| `watch/timeline.log` | 各角色 + 巡检 | 三方共享时间线，谁在何时把活交给谁 |
| `watch/idle.state` | 巡检 | 计时状态 |
| `escalations.jsonl` | 各角色 | 阻塞升级链：谁问的 → PM → 人类 |
| `index.json` | 巡检 | **全量状态单文件快照**（各 task 的阶段/义务/轮次），不用挨个翻目录 |
| `wiki/*.md` | 各角色 | 累积的决策 / 教训 / 约定，`xteam search` 可检索 |
| `session.json` | xteam | 当前 workspace 与 pane 映射 |

## 先搜再动手

```bash
xteam search 结算          # 之前关于结算做过什么决定、踩过什么坑
xteam restore              # 整体进度：闭合了什么、欠谁什么、最后发生了什么
```

PM 接手任何切片前先 `search` 一次——`wiki/` 里是这个项目**累积的教训**，
比重新踩一遍坑便宜得多。

## 与项目文档的分工

**项目文档是产品契约，xteam 只读不改。**

- 项目已有 `docs/`（含自己的 adr / specs / 设计决策）→ **以它为准**。
  xteam 的 spec 会引用既有决策，不会另起一套说法。
- 项目没有 `docs/`（简化开发）→ xteam 用 `tasks/<切片>/spec.md` +
  本目录的 `wiki/` 承载决策记录。

无论哪种，**xteam 都不会去改项目里的其它文件**。它只写 `.xteam/`，
外加对 `.gitignore` 的追加（只增不覆盖）。

## 为什么它不入库

里面是 agent 的中间产物：临时 spec、验证输出、pane id、文件路径。
入库噪音大，且可能带进本机路径或凭据片段。真正的产品文档请写进项目自己的
`docs/` 或 `specs/`。

## 关掉之后

`xteam down` 只关 workspace，**不删这个目录**。下次：

```bash
xteam restore        # 看还剩什么、欠什么、时间线最后发生了什么
xteam up . --watch   # 接着干
```
