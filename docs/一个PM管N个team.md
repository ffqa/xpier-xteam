# 一个 PM 管 N 个 team：设计与代价

> 动机（用户 2026-10-10）：现在一个项目 = 一套 pm/tl/dev，开发只能串行。如果 **一个 PM
> 管 N 个 tl/dev 组**，实现就能并行，这才是「PM 真正代替人」的形状，也才是可用的简易
> kander。本文只做设计，不动代码 —— 它是架构题，不是功能题。

## 1. 现在到哪了

| 事实 | 代码位置 |
|---|---|
| 一个项目 = 一个 `.xteam/` = 一套三角色 | `cmd_up` 一次起 pm/tl/dev 三个 pane |
| workspace 按**项目目录名**命名 | `resolve_ws_label()` |
| `projects.json` 是**多仓库**声明（前端/API/管理端共用一个 `.xteam`），不是多 team | `docs/多项目与契约.md` |
| 门禁判据只有一个作者：`verdict.json` 永远是 PM | `bin/xteam:810` 附近 |
| 冻结规则：有待判片就冻全场（v0.2.5 改成「只拦 touches 冲突」） | `debts()` |

## 2. 目标形态

```
                    ┌─ pm（唯一，跨组）─────────────┐
                    │ 欠：a 组 gate / b 组 gate      │
                    └───────┬──────────────┬────────┘
                      group A              group B
                    ┌── tl ── dev ──┐   ┌── tl ── dev ──┐
                    │ slice-a1      │   │ slice-b1      │
                    └───────────────┘   └───────────────┘
```

- **组 = 一套 tl/dev**。PM 仍然只有一个（它是唯一裁决者，也是人的替身）。
- 切片归属写在 `.xteam/tasks/<slice>/task.json` 的 `team` 字段；没写的归 `default`。
- 每个组一个 workspace（pane 名前带组前缀），PM 自己一个 workspace。
- 投递路由从「按角色」变成「按 (team, role)」：`say tl` 要能说清是哪个组的 tl。

## 3. 必须先回答的三个问题

1. **一个切片派给哪个组？** 候选：PM 自己决定（写进 request）、按 touches 冲突自动避开、
   按组当前负载。**建议第一步只做 PM 显式指定** —— 自动均衡看起来美，但它会把「派错组」
   从人的错误变成系统的错误，而前者一眼能看出来。
2. **两组同时待 gate 时 PM 怎么排？** PM 是单点，`verdict.json` 一个作者，所以 gate 天然
   串行。能并行的是**实现**。冻结粒度要从「项目」降到「组」：A 组待判时不冻 B 组，只冻
   与 A 的 touches 冲突的片。**这把并行度上限暴露出来了：1/(PM gate 时长)** —— 想再快只能
   多个 PM，那是下一个议题，不是这个。
3. **一个 PM 盯 N 组，上下文怎么办？** PM 的 context 会被 N 组同时拉扯。每组一份
   `memory/<team>-snapshot.md`（现成机制），PM 只看「组摘要 + 欠什么」，细节按需读。
   **这是这套东西能不能长跑的关键**，比 pane 数量重要得多。

## 4. 会被波及的面（这就是它贵的原因）

`up` / `swap` / `down` / `reopen`（pane 命名与映射）、`status` / `board`（按组分区）、
`say`（路由）、`watch` 巡检（逐组）、`debts()` 与冻结规则（按组）、`board` 的 bring-up
（逐组拉）、协议文档（`team` 字段）、e2e（多组场景）。

## 5. 建议的第一步（increment 1）

只做「1 个 PM + 2 组」的骨架，把上面三个问题里最短的那条先立起来：

1. `xteam team add <名字>` / `xteam up --teams 2`：起 PM + 每组一套 tl/dev。
2. `task.json` 加 `team`；`debts()` / `slice_order()` 认它；待 gate 按组分开数。
3. `board` 顶部加一条「组状态带」（每组 working/idle/blocked + 手上那片），点进去看那组
   的 tl/dev —— 现在看板已经是分区渲染，加这一层是自然的。
4. `status` / `say` 支持 `--team`。

明确**不做**的：PM 的自动派组、跨组负载均衡、多 PM。

## 6. 判断

值得做，但**排在「独立录入界面（notes → 推给 PM）」之后**：那个界面决定「需求从哪来」，
它没定之前，N 组并行只是把「人等 PM」变成「PM 等需求」，并行度再高也空转。
