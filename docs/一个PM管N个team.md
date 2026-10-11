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

---

## 7. Kandown（kander）研究笔记（2026-10-11，读了官方文档四篇）

用户点名要「深度学习 kander」。搜到两个同名候选，读的是 **Kandown**（kandown.dev；
另一个是 kanban.rs 的 `kanban`，纯 TUI + MCP，形状相近）。读的四篇：Board & views、
Data model、For AI agents、Terminal UI / Launching agents。

### 7.1 它最值得抄的四条

1. **一个任务一个文件，没有索引** —— `tasks/t1.md` 带 frontmatter，目录 listing **就是**
   board 状态。三条不变量写在文档里：状态只在 `tasks/*.md`；依赖门禁 / 解析器 / daemon
   **各只有一份实现**，被 Web UI、终端 UI、CLI 三个界面共用；归档是移动不是删除。
   → xteam 已经是这个形状（`.xteam/tasks/<slice>/`，闭合不删）。**N 组也不能破它**：
   加 `team` 字段，不加新索引文件。
2. **agent 是看板的另一个用户，不是集成** —— `kandown work` 一条命令吐出确定的上下文文档；
   **stdout 只出数据**（✓ Created… 全走 stderr），退出码有意义，所以 `$(kandown create …)`
   拿到的东西永远干净。
   → xteam 的 `whoami` 就是它的 `work`。N 组之后要 `xteam whoami --team a`。另外
   **「stdout 只出数据」这条值得全项目学** —— 现在 `xteam status` 的表格混着装饰输出。
3. **依赖门禁从任何界面都拦** —— 「agent 不能把依赖未满足的任务挪到终栏」。门禁是共享代码，
   不是某个界面的规则。
   → xteam 的 `verdict.json` 单作者 + `STAGE_FILES` 就是门禁，已在协议层。N 组时它必须
   还是**协议层的一段代码**，绝不能变成 board 里的 if/else。
4. **每个 agent 有自己的启动形状**（交互式会话 / 一次性 prompt / 上下文写 /tmp），而且
   **只列 PATH 上存在的**。
   → xteam 已有 `_boot_role` + `ONESHOT_VERIFIED`。N 组里「哪个 agent 能无人值守跑」
   会决定一组能不能真并行 —— 这张表要变成显式的（`xteam agents` 里标 `unattended`）。

### 7.2 它启发、但我们该走自己的路的两条

- **agent hook（按 `g` 把任务 POST 到你自己的编排器）**。xteam 的对应物不是 webhook，是
  `xteam say --team a dev "…"`（门铃直投）。**但这条给出了「PM 怎么把活交给一组」的最小
  形状：一个显式的投递动作 + 一条可回放的记录** —— 现在 doorbell.log 就是。
- **`report:` 行把完成的任务变成工作日志**，并在 UI 里显著渲染。xteam 的对应物就是闭合切片
  里的 spec / delivered / verdict —— 看板刚补的「翻开这一片全部文档」正是干这个。

### 7.3 明确不抄的

- **Web daemon + 浏览器看板**：多一个常驻服务、一个端口、一份 token 管理。终端看板 + 文件
  协议已经覆盖使用场景，抄它等于把「状态是文件」这条底线换成「状态是 daemon 的缓存」。
- **可拖拽的列**：3 个角色下没有「拖」的必要（v0.2.17 的结论），kander 有价值是因为它的
  列是用户自定义的。

### 7.4 对 N-team 设计的三点修正

1. **切片上加 `team` + `assignee` + `ownerType`**（对齐 kander 的 assignee/ownerType），
   派组这件事从「PM 口头说」变成「文件里的一行」，board 按 ownerType 过滤「人的活 vs agent 的活」。
2. **门禁仍是一个 PM 一个 verdict**，但**冻结粒度降到组**：A 组待判不冻 B 组，只冻 touches
   冲突的片。这把并行度上限暴露成 `1/(PM gate 时长)`，并逼出下一个议题：多 PM。
3. **`xteam work --team <组>`** 作为每组 agent 的唯一上下文入口（对齐 `kandown work`），
   这样「一个 PM 盯 N 组」才有便宜的读法：PM 读 N 份摘要，细节按需。

### 7.5 判断不变

仍然建议**先做独立录入界面**：kander 的前提是「任务从哪来」有一个明确的入口（`n` 新建、
`a` 派给 agent）。我们没有这个入口之前，N 组只是并行地等需求。

