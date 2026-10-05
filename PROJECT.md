# xteam 项目报告

> 接手这个项目的人先读这一份，再读 [README.md](README.md)（怎么用）。
> 这里回答四个问题：**要什么、怎么想的、做到哪、什么情况下不能用**。

---

## 1. 需求

### 1.1 要解决的问题

在真实项目里让三个 agent（pm / tl / dev）协作开发，而**协作状态可审计、可中断、可恢复**。

不是「让 AI 写代码」，而是解决协作里的三件具体事：

| 问题 | 传统做法的失败 | xteam 的做法 |
|---|---|---|
| 谁欠着活 | 看终端猜，或者问 PM | **文件即状态机**，`xteam status` 直接算出每个角色的义务 |
| 卡住了没人知道 | agent 静默退出，等人偶然发现 | **巡检**主动判定停滞、升级人类、响通知 |
| 关掉就丢上下文 | 重开会话从零解释 | 状态全在 `.xteam/`，`restore` 随时接回 |

### 1.2 三个角色的分工

```
你（人类）──需求、拍板──▶ pm ──spec──▶ tl ──request──▶ dev
                            ▲                        │
                            └──── verdict ────────────┘
```

- **pm**：对接人类 → grill 出 spec → **门禁判 PASS/FAIL** → 巡检。
  **第一职责是让事情继续下去**，不是当记录员。
- **tl**：评估 spec（**必须回，不能沉默**）→ 拆解 → 派活 → 持续 review → 打回。
  **不写代码**（否则分不清哪些是 dev 做的，评审链失效）。
- **dev**：实现 → 自测 → 上报 → 停下等审核 → 主动要下一件。

**为什么 gate 在 pm 而不是 tl**：pm 写 spec 但不写代码也不做微分解，审 dev 的交付没有
自审偏差；tl 天天和 dev 交互，由它判终局最容易「自己派自己判」。

### 1.3 明确不做的事

| 不做 | 原因 |
|---|---|
| 不用数据库 | 状态必须能被 agent 用 `cat` 读懂、能被人 `git diff` 看 |
| 不加文件锁 | 会引入「持有者进程死了 → 文件永远锁死」，比写坏更难恢复 |
| 不让 agent 互相裁决 | 三个 reviewer 意味着意见冲突，裁决权只会更集中 |
| 不装第四个常驻角色 | 常驻角色必然读过同一份 spec，会和 pm/tl 犯一样的错 |

---

## 2. 原理

### 2.1 状态即文件，不是看板

`.xteam/tasks/<slug>/` 下每个文件代表一级推进，谁写哪个文件是固定的：

```
spec.md → request.md → delivered.json → ready.json → verdict.json
 → consumed.json → closed.md
   PM        TL          DEV           TL          PM            DEV
```

无看板、无卡、无分支、无 worktree。**工件存在性 + round 计数就是状态。**

### 2.2 义务由协议算，不由时长猜

这是整套东西的核心。判据是**「它有没有未消费的交付」**，不是「idle 了多久」：

| 状态 | 义务 | 含义 |
|---|---|---|
| idle + `owes:none` | — | **真做完了**，合法终态，不报警 |
| idle + `owes:consume` | consume | **卡住了**，该催 |
| working | — | 正常，不打扰 |
| blocked | — | 停在审批 UI，**不代答破坏性操作** |

靠超时报警做不到：做得快的角色会被反复骚扰，真卡死的和认真干活的在超时面前没区别。

### 2.3 交接工件必须带轮次

`delivered.round`、`ready.delivery`、`verdict.round`、`verdict.delivery`。
**因为工件一旦 FAIL 就一直躺在目录里**，没有轮次就分不清「上一轮的 ready」和
「这轮该出的 ready」，FAIL 之后链路直接死锁——三个人都不欠活。

### 2.4 决策权归状态机，不归 agent 自觉

agent 的职责是**写工件**，而「谁欠什么」由 `debts()` 算出。agent 不写任何东西时，
系统照样知道该催谁。

---

## 3. 实现情况

### 3.1 已完成并验证

| 能力 | 状态 |
|---|---|
| 三 pane 编排 + 文件协议状态机 | ✅ 端到端验证 |
| 巡检（停滞判定 / 升级人类 / 代答选择题 / 权限放行） | ✅ |
| 中断恢复（`restore`）、会话标记 | ✅ |
| 多项目治理（项目注册表、共享契约层） | ⚠️ 已实现，**未在真实多项目跑过** |
| 跨项目切片排序（`depends_on` + 拓扑序） | ⚠️ 同上 |
| API 契约（`contracts new/list/check/log`） | ⚠️ 同上 |
| 第三方冷读评审（`xteam qa`） | ✅ 白名单放行 + 不可逆操作永不自动 |
| 中途换 agent（`swap`，两阶段替换） | ✅ |
| workspace 复用（`up` 复用 / `down` 只关 pane） | ✅ |
| 三层门禁（静态 → 单测 → 端到端） | ✅ 473 断言 |

### 3.2 工程约束

- **纯文本、无构建步骤**。运行只需 python3 ≥ 3.9 + herdr + 至少一个 agent CLI。
- **三层门禁**由快到慢分层，失败报出是哪一层——因为坏代码上跑出来的结果本身是
  误导的（status 崩在半路，看着像协议有问题，真因只是方法名写错）。
- **`tests/lint_names.py`** 抓「读了未定义的变量」和「调用不存在的方法」。
  这类错误编译期查不出、只在跑到那条路径才炸。

### 3.3 默认配置

| 角色 | agent | 说明 |
|---|---|---|
| pm / tl / reviewer | **omp** | 实测认 `--model` |
| dev | **devin** | `swe-2-max` |

**不用 opencode 的原因**：它的交互 TUI 不认 `--model`（实测 v2.0.20），只能吃
`opencode.json` 里那份**指定不了也看不见**的模型。

---

## 4. 运行边界

### 4.1 硬限制

| 限制 | 说明 |
|---|---|
| **必须 git** | 文件协议没有版本控制 = 没有后悔药。已发生过误写事故 |
| **必须 herdr 在运行** | `herdr workspace list` 不报错才算起来 |
| **需要至少一个 agent CLI** | devin / opencode / omp / pi / cursor / qodercli |
| **opencode 不能指定模型** | 见上；换 omp 或让 opencode 用自己的配置 |
| **openrouter 只提示不拦** | 名字里看不出走的哪条线路；确实要用加 `--allow-openrouter` |

### 4.2 已知的未修问题

| 问题 | 影响 | 缓解 |
|---|---|---|
| **`swap` 曾非事务** | ✅ 已修为两阶段（新 agent 活了才关旧的） | — |
| **共享 JSON 非原子写** | ✅ 已修为 tmp + `os.replace`；损坏不再静默伪装成「没有状态」 | — |
| **`--probe` 对未登录的 CLI** | 有些 CLI 会把 Error 写进 stdout 且退出码 0 | 已校验内容，但仍需人工确认 |
| **`mcode` / `qoderclicn`** | 不在 herdr 的 kind 列表，无法编排 | — |

### 4.3 什么时候不该用

- **一次性的小任务** —— 起三个 pane 的开销大于收益
- **没有版本控制的项目** —— 见上
- **需求本身没定** —— pm 的价值是 grill 出可判定的 spec；需求还是「做个 XX 系统」时，
  先想清楚再上

---

## 5. 踩过的坑（**这一节是 git 历史不能替代、但代码里也看不出来的东西**）

接手时最容易重犯的几个，按「会不会再犯」排序：

1. **改名时漏掉散落各处的路径字面量**。`.pm` → `.xteam` 时漏了一处 f-string，
   recap 因此全写进黑洞，agent 绕过去改成「双写」，token 成本翻倍。
   → 现在工作目录名收敛成 `XTEAM_DIRNAME` 常量，并有守卫测试。

2. **`set -e` 下 `[ "$X" = "1" ] && ...` 短路返回 1 会直接杀掉脚本**。
   → 写 shell 时所有 `[ ... ] && ...` 都要考虑 `|| true`。

3. **macOS 自带 bsdtar，不支持 `tar --transform`**（GNU tar 才有）。

4. **argparse 里子解析器的同名参数会覆盖全局选项**。
   `xteam --project X up .` 里的 X 曾被静默忽略，状态全写到当前目录。

5. **告警字段不能塞字符串**。`alerted` 是 0/1/2 的阶梯，塞进 `"recap"` 后
   `alerted < 1` 抛 TypeError，而那行在 working 分支里 —— **整个看门狗死掉**。

6. **冷却要涵盖所有触发原因**。两个分支各记各的账（一个写切片名、一个写空串），
   判据和记账格式对不上，于是每 60 秒问一次，跑一夜 696 次。

7. **Python 变量是函数局部的**。静态检查按文件追踪「变量→类型」会把测试 A 的
   类型套到测试 B 上。

8. **不要 `git checkout --` 改到一半的文件**。今天为此丢了两次未提交的改动。

---

## 6. 改动后的自检清单

```bash
python3 tests/test_protocol.py   # 协议单测，约 10s
bash tests/smoke.sh              # 三层门禁，约 180s，第 3 层要 herdr + agent
bash pack.sh --worktree          # 发布包（不含 .git，自证可用）
```

改了协议语义或角色章程，**同时**更新本文件第 2、3 节——它们是接手的人唯一会读的地方。

## 6.1 发版

发版已交给 GitHub Actions，本机不跑发布脚本：

```bash
bash pack.sh --bump=patch     # 升版本 + 自动提交 + 打 tag xteam-v0.1.6
git push && git push --tags   # tag 一到，CI 接走剩下全部
```

CI（`.github/workflows/release.yml`）会：核对 tag 与 `VERSION` 一致 → 打包并自证可用
→ 建 Release（正文只取 CHANGELOG 当前版本那一段）→ 更新 `ffqa/homebrew-tap` 的 Formula。

Formula 模板在 `.github/formula/xteam.rb.template`。**要改安装逻辑就改那里**，
不要改本地 `brew-publish.sh` —— 它已 gitignore，发版不再经过它。

首次配置需要给仓库加两个 secret（发 Tap 用，否则 Release 仍会建、只是不更新 tap）：

| secret | 内容 |
|---|---|
| `TAP_SSH_KEY` | `ffqa/homebrew-tap` 的部署私钥（只对这个库有写权限） |
| `TAP_KNOWN_HOSTS` | `ssh-keyscan github.com` 的输出 |

## 7. 回复风格

**一句话能说完的，不要说两句。**

要证据就给数字（"500 切片：>30s → 1.33s"），不铺陈；不要把一个结论拆成
"问题/决定/检查/下一步"四段，除非确实有四件事要说。表格只在真的逐项对比时用。

这条对 pm/tl/dev 三个 agent 同样成立——门铃和 say 里的话也是回复。
