# xteam 协作协议

三个 pane（pm / tl / dev）之间的全部协作协议。**没有看板，没有卡，没有分支/worktree。**
状态就是文件的存在性。

## 目录

```
.xteam/
  QUEUE.md              队列：唯一待办真相
  PROTOCOL.md           本文件
  tasks/
    <slug>/
      spec.md        PM 写   需求成型（含与用户 grill 的结论）
      spec.json      PM 写   {"round": N}  spec 版本号（每次改 spec +1）
      assessment.json TL 写  {"spec_round": N, "verdict": "agree"|"object", "objections": [...]}
                                    —— TL 对每版 spec 的评估，有异议无异议都必须回
      agreement.json PM 写   {"spec_round": N, "note": "…"}  达成一致的确认
      blocked.json   TL/DEV  {"round": N, "role": "tl|dev", "where": "…", "question": "…"}
                                    —— 卡住了，需要 PM 定
      pm-response.json PM 写 {"round": N, "decision": "…"}  对 blocked 的回应
      request.md     TL 写   拆解 + 细化到 dev 可执行（达成一致后才写）
      delivered.json DEV 写  实现完成 + 自测结果（round = 本次交付序号）
      ready.json     TL 写  工作 review 通过（delivery = 已通过的交付序号）
      verdict.json   PM 写  门禁 PASS/FAIL（round = 判次，delivery = 判的是哪次交付）
      consumed.json  DEV 写 已取 verdict（round 对齐）
      closed.md      PM 写  本切片闭合
  session.json         xteam up 写的会话记录（workspace / pane 映射）
  team.json            角色用的 agent / model（可选，up 自动落盘）
  watch/               巡检状态与事件
```

## 三套轮次

`spec`、`delivery`、`verdict` 是**三个独立计数**，不要混用：

- `spec.json.round` —— PM 每改一版 spec 涨 1
- `delivered.json.round` —— dev 每交一次涨 1
- `verdict.json.round` —— PM 每判一次涨 1

交接工件记录**自己针对哪一版/哪一次**：

- `assessment.json.spec_round` / `agreement.json.spec_round` —— 针对哪版 spec
- `ready.json.delivery` / `verdict.json.delivery` —— 针对第几次交付
- `blocked.json.round` / `pm-response.json.round` —— 阻塞的第几轮

**为什么交接工件必须记版本/轮次。** 工件一旦 FAIL 或提异议就一直留在目录里，下一轮
还会被看到。没有轮次就无法区分「上一轮的 ready」和「这轮该出的 ready」，于是 FAIL
之后链路死锁：TL 不欠 review（ready 还在）、PM 不欠 gate（verdict 还在）、dev 也不欠
（delivered 还在）——三个人都不动。带轮次之后，纯靠比较就能立刻看出「有更新的东西
没人看」。

## 状态机

```
阶段一 · 需求共识（PM ↔ TL 必须谈一轮，TL 不许沉默）
  (spec.md + spec.json round=k)     PM 写
      ↓
  (assessment.json spec_round=k)    TL 写   ← 必须回！有异议无异议都回
      ├── verdict=object → PM 讨论：改 spec（round+1 重发） 或 写明为什么不改
      │        ↓ 循环，直到达成一致
      └── verdict=agree
      ↓
  (agreement.json spec_round=k)     PM 写   ← 确认并明确引导 TL 开始
      ↓
阶段二 · 执行
  (request.md)                      TL 写    ← 门铃 dev
      ↓
  (delivered.json  round=n)         DEV 写   ← 门铃 tl
      ↓
  (ready.json     delivery=n)       TL 写    ← 门铃 pm
      ↓
  (verdict.json   round=m, delivery=n)  PM 写  ← 门铃 dev
      ├── PASS → (consumed.json round=m) → (closed.md) → 下一项
      └── FAIL → dev 改完重交 delivered round=n+1 → 回到 TL review

贯穿全程 · 解阻塞（任何角色卡住都能触发，PM 优先处理）
  (blocked.json round=p)   TL/DEV 写  ← 门铃 pm
      ↓
  (pm-response.json round=p)  PM 写  能自己定就定；真要人类拍板就 --waiting
```

共识阶段在产出 `request.md` 之后就退出：需求中途变化走执行阶段的 review 打回，
不回退到共识阶段（否则两个角色来回震荡）。

## 两条硬规则

**PM 出 spec 前必须自检判据。** TL 第一轮提的异议里，最大一类是「判据不可判定」——
而这些本该 PM 自己发现。逐条对着问：每条判据能用**一条命令**跑出来吗？判据之间**不互相
矛盾**吗？判据**不违反 spec 自己写的边界**吗？期望值**写死到逐字节**吗？依赖的**前置条件**
写全了吗？

做法是自己把每条判据的命令在仓库里真跑一遍，跑不出来的就改到能跑出来再发。实测一轮
真实协作：不做自检时 TL 第一轮提 7 条异议；做了自检通常 1–2 轮内收敛。

**TL 不许沉默。** PM 把 spec 给 TL 之后，TL 必须回 `assessment.json`——有异议回
`object`，没异议回 `agree`，**不许不回**。因为 PM 不知道 TL 有没有意见，就不会让 dev
开工；一旦允许沉默，异议就要拖到 review 阶段才炸，返工大得多。这条由状态机的 `assess`
义务强制（TL 不回 → 巡检会一直催它回）。

**遇选择题默认选推荐项继续。** agent 抛出带选项的选择题（「下批做哪一项？」「A 还是
B？」）时，**默认选推荐的那项继续执行**，不要停下来等人类。推荐项本来就是基于现状算的
最优解；为一个已经有答案的问题打断整条链，代价远大于选错的代价（真选错了，后面 gate
会拦下来）。只有破坏性/不可逆操作、需求本身有歧义、需要人类授权（凭据/付费）才停下来问。

机制上分两种：
- **agent 停在 UI 选项上**（`agent_status: blocked`）→ 巡检读 pane 尾部解析出选项，
  自动选推荐项并送进去（只答选择题，不代答破坏性操作）
- **agent 走了协议**（写了 `blocked.json`）→ PM 读内容，能自己定就定并写
  `pm-response.json`；真要人类拍板就 `--waiting` 说明在等什么。PM 的第一职责是让
  事情继续下去，不是把问题攒着。

## 角色义务

`xteam status` 里每个角色的 `owes` 字段由下表**纯文件 + 轮次推导**：

| 角色 | 义务 | 触发条件 |
|---|---|---|
| pm | `unblock` | `blocked.json` 在且 `blocked.round > pm-response.round`（**优先级最高**） |
| pm | `version-spec` | `spec.md` 在但 `spec.json` 不在（TL 无从判断该评估哪一版） |
| tl | `assess` | `assessment.spec_round < spec.round`（**必须回，不许沉默**） |
| pm | `settle` | TL 已回评估，但 `agreement.spec_round < spec.round`（PM 要回应） |
| tl | `decompose` | 已达成一致，`request.md` 不在 |
| dev | `implement` | `request.md` 在但没交付；或上次 verdict 是 FAIL 且判的就是当前这次交付 |
| dev | `consume` | 已交付、verdict 存在，且 `verdict.round != consumed.round` |
| tl | `chase` | `delivered.json` 在，但 `ready.json` 不在或 `ready.delivery < delivered.round` |
| pm | `gate` | `ready.json` 在，但 `verdict.json` 不在或 `verdict.delivery < ready.delivery` |
| pm | `next-slice` | 该 task 已 `closed`（该派下一项了） |

几个细节是刻意的：

- **`unblock` 压过一切。** 一个没人拍板的问题挂着，下游整条链都是死的——PM 的第一
  职责是让事情继续下去。
- **TL 不许沉默。** `assess` 独立成一个义务，而不是「拆解时顺手提一嘴」：一旦允许
  沉默，PM 就会在不知情的情况下让 dev 开工，异议要到 review 阶段才炸，返工大得多。
- `consume` 用 `!=` 而不是 `>`。`verdict.round < consumed.round` 是 verdict 丢了轮次的
  回退事故，此时不能读成「已完成」——那会让该切片永久卡死再无人催。宁可多催一次。
- `implement` 优先于 `consume`。FAIL 打回时 dev 要做的是改代码重交，不是先写个
  consumed 了事。


**`owes: none` + `idle` = 真做完了，合法终态。** 这是这套协议和「idle 超时就报警」的根本区别：
不做无用功的报警，也不让真正卡住的角色安静地待着。

## 解阻塞：PM 的两条出路（都不能停）

TL 或 dev 写了 `blocked.json` 之后，`unblock` 是 PM 的最高优先级义务。PM 必须回应，
而且**只有两条合法的出路**——都不是「停下来等」：

### 出路 A · 按推荐项继续

如果阻塞点有明确的推荐解（多数情况如此），**直接定下来**并写 `pm-response.json`：

```json
{"round": 1, "decision": "按 TL 推荐的方案 A 执行：…", "route": "recommend"}
```

然后门铃对方继续。**为一道已经有答案的题停工，代价远大于选错的代价**——真选错了，
review 阶段会拦下来。

### 出路 B · 拆小重规划

如果阻塞的根因是**任务粒度太大**（TL 拆不动、dev 做不完、依赖横跨多个模块），
那就不要硬啃：**改 spec、拆成更小的切片**，让 dev 现在就能动手做其中一块。

```json
{"round": 1, "decision": "根因是粒度太大：拆成 结算 / 工单 两片，先做结算",
 "route": "replan", "new_slices": ["checkout-settle", "checkout-workorder"]}
```

拆分后**至少要放行一个能立刻开工的小切片**。拆而不放行等于把阻塞换了个地方。

### 什么时候才轮到人类

只有这两类，其余一律 PM 自己定：

- 破坏性 / 不可逆：删数据、force push、发布、动生产
- 需求本身有歧义：不是「选哪个实现」，而是「到底要什么」

轮到人类时 PM 必须 `stamp --waiting --wait-for …` 说清在等什么，**不能只是沉默**。

### 催不动 PM 怎么办

巡检催 PM 两轮仍无响应 → **升级到人类**（`NEEDS-HUMAN` 事件 + macOS 通知 + 时间线
WAITING 标记），此后每 15 分钟重提醒一次，**直到有人接手为止**。

**绝不静默放弃。** 曾经的设计在催满两轮后直接跳过，于是「全队唯一的解阻塞人退出」——
整条链死得比没有监控更彻底，且无人知晓。

## 门铃

```bash
xteam say <role> "<短消息，一两句话指路>"
```

- 短消息走 pane，完整内容走文件。这是「门铃 vs 信封」：注入 pane 的文本会进对方输入
  缓冲区并可能打断它正在执行的回合，长文本硬塞既触发长度告警又挤掉上下文。
- **返回码不代表已提交。** 目标非 `working` 时文本只进 TUI 队列，`xteam say` 已自动补
  `send-keys enter`。送达判据是对方 `agent_status` 转 `working`。
- 角色名（`pm`/`tl`/`dev`）是唯一寻址物，**不要用 pane_id**——它跨 workspace 会重复。

## 输出写法（STE 精简版）

> 完整规范见 `skills/ste/SKILL.md`（ASD-STE100 中文改写）。下面是每次输出必须遵守的
> 最小集合——遵守这 8 条即合格，拿不准时再查原文。违反它们比多写两句的 token 代价更高：
> 含糊的汇报会换来人类的追问，而追问是最贵的 token。

- **结论先行。** 回复第一句写结果（带状态词），过程证据写后面。
- **状态词只用 10 个**：已完成、部分完成、未开始、进行中、已验证、未验证、
  失败、跳过、阻塞、未确认。不得用「已修复」「搞定」代替。
- **每个结论写验证。** 已验证必须写检查方法；没跑过的检查写「未验证 + 原因」，
  不得写成通过。
- **一句一事。** 一句只写一个动作或事实；情态词只用「必须」「不得」「可以」。
- **不写过程叙述。** 不写「我先……然后……」。只写结果和读者需要的证据。
- **引用可定位。** 代码写 `路径:行号`，提交写提交号，命令写完整命令。
- **请用户决定时列编号选项。** 推荐项放第一并标「（推荐）」，每个选项写动作和后果。
- **不确定标「未确认」。** 不得用「可能」「大概」「应该是」掩盖；同时写出确认方法。
- **日志/报错/命令输出原样引用**，一字不改。

## 时间戳

**每条输出都带时刻。** 判断「它是刚做完还是卡了半天」需要的是**时刻**，不是时长——
时长只说「多久」，说不了「什么时候开始的」。

### xteam 自己的输出

`status` / `say` / `watch` / `stamp` 都带时间戳。`status` 额外给每个角色两列：

```
role  状态      持续     起始于         最后活动        义务 / 在哪
pm    idle      18m20s   10-01 16:06:11  10-01 16:06:11  none
dev   idle      4m12s    10-01 16:20:19  10-01 16:20:19  consume checkout
```

- **起始于** —— 当前这个状态从什么时候开始
- **最后活动** —— 最后一次真正在动是什么时候（`working` 或 `state_change_seq` 变化）

从未活动过显示 `—`，不是 1970 年。

### 角色自己的输出

三个角色每轮结束都执行 `xteam stamp <role> "<摘要>"`。时间戳由脚本生成，
**不要手写**——手写的可靠性等于「写的人对自己何时看过表」的记忆。

三个 pane 的时刻落在 `.xteam/watch/timeline.log` 同一条线上，可以直接排出
「谁在什么时候把活交给了谁」：

```
2026-10-01 16:20:19 | dev | delivery 2 已提交，84/84 + 42/42
2026-10-01 16:21:02 | tl | review 通过，diff 已看，自跑验证一致
2026-10-01 16:22:40 | pm | gate PASS，队列推进到下一项
```

### WAITING 免打扰

需要人类拍板而停下时：

```bash
xteam stamp pm "等你拍板结算口径" --waiting --wait-for "结算单含未结清账单，账单切片未建"
```

巡检读到 `WAITING` 且龄 ≤ 30 分钟 → **免打扰，但仍记 `SUPPRESSED` 事件**。

免打扰不等于免责：角色仍然欠着协议义务（`gate` / `next-slice`），人类回来时 `status`
照样显示它欠活。「在等人类」是合法状态不是故障——但必须在时间线上留痕，否则别人
只看到它不动，分不清「在等你」和「卡死了」。

## 巡检

```bash
xteam watch start|stop|status|once
```

盯全部 3 个 pane（不是只盯一个）。判据：`agent_status` + `state_change_seq` 双条件，
**不看文件 mtime**（agent 长时间读代码不落盘，mtime 会误报停滞）。

- 非活动 ≥ 600s 且 `owes != none` 且未声明 `WAITING` → 门铃提醒
- 非活动 ≥ 1800s 仍无反应 → 升级门铃（最多两级，不重复轰炸）
- pane 从 `agent list` 消失 → 记 `MISSING` 事件（可能崩了或被关了）
- 声明 `WAITING` 且龄 ≤ 30 分钟 → 记 `SUPPRESSED`，不催

计时状态持久在 `.xteam/watch/idle.state`，否则每次进程被杀重拉都把时长清零，
10 分钟告警永远打不出来。

**巡检有两层进程**：`supervise`（守护）+ `run`（干活）。

- `run` 会被各种意外干掉（工具任务回收时的进程组 SIGKILL、手滑的 pkill、机器休眠）。
  C-33 的教训是「静默退出 = 没有机制」——巡检挂了没人知道，整条防停摆链悄悄失效。
  所以 `supervise` 盯着 `run`，死了 3 秒后拉起，全程记 `RUN-SPAWNED` / `RUN-EXITED`。
- pidfile 在**项目自己的** `.xteam/watch/` 下，不放 `/tmp`。放全局会串：两个项目同时跑
  巡检会互相覆盖 pid，一个项目的 `watch stop` 杀掉另一个的进程。
- `stop` **先杀 supervise 再杀 run**。顺序反了的话 supervise 会在 3 秒内把刚杀掉的
  run 复活——`down` 之后对着已关闭的 workspace 复活巡检。

**每轮都落一行 `SAMPLE`**，哪怕什么都没发生：

```
2026-10-01T16:25:00Z SAMPLE pm=idle/18m20s@10-01 16:06:11/owes=none  tl=working/0s@…  dev=idle/4m@…/owes=consume
```

理由：只有「出事才写日志」的话，「巡检还在跑」和「巡检已死」在日志里长得一模一样。
巡检进程用 `start_new_session` 脱离当前进程组——工具任务回收时对进程组 SIGKILL 会连坐
（apartment 的看门狗实测活 12min / 7min 无痕死亡，两任都这么死的）。

## 为什么不用 kander

kander 的 1574 行协议（12 个文件、7 个模块全开）在全机所有项目里**从未建过一块板**，
而每个 session 都被强制加载。真正在三个 pane 之间跑通的是上面这套：8 个文件、
1 个巡检脚本、0 个命令契约。

从 apartment 项目实测继承的教训，构成本协议的设计依据：

| 教训 | 协议里的对应 |
|---|---|
| 门铃返回码 ≠ 送达 | `xteam say` 补 enter，判据是状态转换 |
| 文件 mtime 不是存活信号 | 巡检用 `agent_status` + `state_change_seq` |
| 停滞/未消费用 mtime 猜，误报 6 次 | 判据是 `verdict.round > consumed.round` |
| 只看一处证据下全称否定，导致 4 次连续误判 | verdict 的 finding 必须带行号 + 可复现证据 |
| 编排者越界自己改代码，责任不清 | TL 不写代码、PM 不写代码、DEV 不判 PASS |
| 静默退出 = 没有机制 | 巡检异常在 `events.log` 留大声原因 |
