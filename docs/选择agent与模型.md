# 选择 agent 与 model

← 返回 [README](../README.md)

## 指定每个角色用哪个 agent 和 model

### 换 agent（已验证可用）

```bash
xteam up . --set-agent pm=claude --set-agent tl=codex --set-agent dev=devin
```

`--set-agent` 可重复，取值是 herdr 支持的 agent kind。混用不同 CLI 没问题。

### 中途换（不用重启整套）

```bash
xteam swap tl devin --model swe-2-max
```

`--set-agent` 是**开跑前**定；`swap` 是**跑到一半**换，而且不用动其他两个角色。
新配置会写进 `.xteam/team.json`，下次 `up` 直接沿用。

**为什么换 agent 必然要重建 pane，运行中却能精确指定模型。** 运行中的 TUI
换不了自己的 agent（herdr 也没有 `agent kill`，只能关掉整个 tab 重建）。
但既然 pane 本来就要重建，model 就能在**启动时精确指定**——正好绕开运行中用
方向键盲选模型的风险：那个列表没有稳定编号，实测盲按 5 下会落到
`Free Models Router OpenRouter`（真金白银）。所以换 agent 比换 model 干净。

**代价：旧 agent 的对话上下文随 pane 一起消失。** 所以 swap 会先让旧 agent
写一份 recap，再把「它自己的 recap + 别人的 recap 尾巴 + 协议现状（谁欠什么
义务）+ 未闭合切片」注入新 agent。不想要交接加 `--no-recap`，但新 agent 会更瞎。

**安全阀**：旧 agent 还在 `working` 时直接拒绝——此刻关 pane 等于把没做完的活
连同上下文一起扔掉。确实要强换才加 `--force`。


### 指定 model（**取决于 agent 是否支持**）

```bash
xteam up . --set-agent dev=devin --set-model dev=devin-large
```

这点必须先说清楚，否则会白等两分钟才报错：

| agent | TUI 认 `--model`？ | 实测 |
|---|---|---|
| devin / claude / codex | 认 | `herdr agent start -- --model devin-large` 6.8s 起来，argv 回显正确 |
| **opencode v2.0.20** | 启动时不认 | TUI 顶层没有 `--model`（只有 `opencode run` / `opencode mini` 有） |

opencode 传 `--model` 的后果：它打印帮助然后退出，herdr 一直等不到交互态，报
`timed out waiting for agent startup`——**这个报错长得像「模型名不存在」，会把人带偏**。

所以 xteam 对 opencode **提前拒绝**并给替代方案，不让你干等超时：

```
$ xteam up . --set-model pm=anthropic/claude-opus-4-5
pm: opencode 的交互 TUI 启动时不支持 --model（本机 v2.0.20 实测），无法脚本化指定。
  两个办法：
    1. 换成支持 --model 的 agent：
       xteam up --set-agent pm=devin --set-model pm=<模型名>
    2. 让 opencode 用它自己的配置：改 opencode.json 的 model 字段，
       或在 TUI 里手动选一次（它会记住并覆盖配置）。
```

opencode 侧试过并**都不通**的三条替代路径（都实测过）：`OPENCODE_MODEL` 环境变量、
`OPENCODE_CONFIG` 环境变量、项目级 `opencode.json` 的 `model` 字段。最后一条配置
**确实被读到**（`opencode debug config` 能看到解析成 `opencode-go/glm-5.3`），但 TUI 会用
持久化的「上次所选模型」覆盖它，界面仍显示 `DeepSeek Chat`。

模型名先查再填：`devin models` / `opencode models` / `omp models`。

### 运行途中能不能换模型

| 方式 | 结论（实测） |
|---|---|
| 启动时 `--set-model` | ✅ **唯一可靠的脚本化路径** |
| `opencode run --model X "hi"` | ❌ 一次性执行完就退出，与常驻 pane 无关 |
| 运行中发 `/model` | ⚠️ 会弹模型选择器；`/model <名字>` 带参**不生效**，但**方向键 + enter 能换成功**（实测） |

**但 xteam 不自动做这件事。** 方向键导航是"盲选"：列表项没有稳定编号，高亮起点随
当前模型和滚动位置变——实测盲按 5 下就落到了 `Free Models Router OpenRouter`，
正是用户明确要求禁用的线路。让程序盲选模型，代价是真金白银。

所以：**模型只在 `up` 时定**。要换就确定性操作：

```bash
xteam down            # 改 .xteam/team.json 的 model
xteam up . --watch    # 用新模型重起
```

### xteam 会周期性发什么指令

**会发的**（每 60s 巡检一轮，有义务才动）：
- 欠活 → 门铃催办（两轮后升级到人类）
- **假 working**（声称在干活但 seq 不动）→ 告警
- **blocked 停在选项上** → 按推荐项自动代答
- 切片收尾 / context > 60% → 要求写 recap

**不会发的**：周期性催办。没义务就安静——`owes: none` + `idle` 是合法终态，
反复催只会变成噪音，还会降低信号的效力。

### openrouter：只提示，不拦

openrouter 是**额外付费**线路（其它 provider 你已订阅）。xteam 启动前会**提示**
「这个模型会走 openrouter」，但**不拦**——想用哪个模型是你的决定。

坑在于**光看模型名判断不出来**。真实踩过：给 omp 传 `--model opus`，它模糊匹配到
`anthropic/claude-opus-*`，而这些在 omp 目录里全部挂在 openrouter provider 下（559 个
模型）——名字里根本没有 "openrouter" 字样。

omp 目录结构有个反直觉的地方（实测 671 个模型）：

| 写法 | 落到哪 | 例子 |
|---|---|---|
| 裸名 | **直连 provider（你已付费）** | `gpt-5`、`deepseek-v4-pro` |
| `provider/model` 前缀 | **openrouter 镜像（额外付费）** | `openai/gpt-5`、`anthropic/claude-opus-4.5` |

所以判据是「这个名字能不能在直连 provider 里原样找到」：`gpt-5` 直连；`opus` 在任何
直连 provider 里都没有，**会模糊匹配到 openrouter**。这种情况 xteam **提示但放行**——
用哪个模型是你的决定，拦死等于替你选。`--allow-openrouter` 只是免掉这条提示。

### 查这台机器上真正可用的模型

```bash
xteam models opencode              # 全列，openrouter 标 🚫
xteam models opencode muse         # 搜
xteam models devin                 # devin 的 721 个
xteam models opencode --only-paid  # 只看非 openrouter
```

实测各家的 `models` 输出格式完全不同（opencode 纯列表 / omp 带 provider 分组表格 /
devin 按 family 分组），xteam 统一解析成同一形状。**注意有些 CLI 在没登录或无匹配时
把错误写进 stdout 且退出码仍是 0**，所以还要看内容——否则会把一句报错当成「该 agent
有 0 个模型」，提示方向全错。

### 固定下来（之后每次 up 自动沿用）

`up` 会把这次实际用的非默认项写进项目的 `.xteam/team.json`：

```json
{
  "roles": {
    "pm":  {"kind": "claude", "model": "opus"},
    "dev": {"kind": "devin",  "model": "devin-large"}
  },
  "_updated": "2026-10-01 16:45:12"
}
```

下次直接 `xteam up .` 即可。想手改就编辑它——随项目走，换机器也在。
配置写错（坏 JSON、未知角色名、给 opencode 指定 model）会**立刻报错**，不静默降级。

### 默认值

| 角色 | 默认 agent |
|---|---|
| pm | omp |
| tl | omp |
| dev | devin |
| reviewer | opencode（默认**不启动**，`--reviewer` 才拉起） |
