"""xteam 共享库：herdr 访问、寻址、门铃、状态机、idle 判定、时间戳、模型成本约束。

设计约束全部来自**本机实测**（包括踩过的坑），不是照抄文档：

1. 不记 pane_id。`wD:p2` 这类句柄跨 workspace 会重复，`terminal_title` 随干活一直变。
   稳定可寻址的是 agent name —— 我们启动时用 `agent rename` 固定成 `role-<workspace>`。

2. `agent prompt` 返回码 != 已提交；而且**对 blocked 的 agent 直接被拒**
   （`agent_blocked: requires interactive input`）。给停在选项 UI 上的 agent 提交选择
   只能用 `pane send-text` + `pane send-keys enter`，且必须核对 agent 状态真的变了
   才算送达 —— 否则日志会写「代答成功」而实际一条都没送出去。

3. 停滞判据不能只看时长，更不能只看文件 mtime。agent 可以长时间读代码不落盘。
   真判据是「有没有未消费的交付」—— 义务由协议状态机算出来，不靠猜。

4. 门铃 vs 信封：短消息指路，完整内容走文件。注入 pane 的文本会进对方输入缓冲区，
   长文本硬塞既触发长度告警又挤掉对方上下文。

5. agent name 在 herdr 里**全局唯一**，所以必须带 workspace 做命名空间，否则同时只能
   跑一个 xteam 项目。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------- 常量

# 工作目录名。**整个项目只此一处定义。**
# 之前 `root / ".xteam"` 散落 10 处（lib 9 + CLI 19），`.pm` → `.xteam` 改名时
# 漏掉了 _maybe_request_recap 里手拼的 f-string —— recap 全写进黑洞，
# latest_recap() 永远读不到，agent 们为绕过去改成「双写」，token 成本翻倍。
# 字面量散落各处 = 下次改名必然再漏一处，所以收敛成常量。
XTEAM_DIRNAME = ".xteam"

# context 偏高触发的 recap 冷却（秒）—— 没有它，巡检每 60 秒问一遍。
RECAP_CONTEXT_COOLDOWN = 1800

# say 送达复核窗口（秒）：第 1 轮非 working 且尾巴无排队占位符时，隔这么久
# 再查第 2 轮。mid-turn 的目标把门铃文本排进 TUI 队列、回合结束才提交，
# 只查一轮必然把「已排队待提交」误报成未送达。不许为 0——为 0 等于没给第二次机会。
SAY_RECHECK_S = 1

IDLE_ALERT_SECS = 600        # 非活动满多久开始门铃
IDLE_ESCALATE_SECS = 1800    # 门铃后仍无反应，再升级一次
WAITING_GRACE_SECS = 1800    # 角色声明「在等人类」后多久内免打扰
STUCK_WORKING_SECS = 900      # 声称 working 但 seq 停滞多久算假 working
HUMAN_ESCALATE_REPEAT_SECS = 900  # 升级给人类后，多久再重提醒一次
SAMPLE_PERIOD = 60           # 巡检采样间隔（秒）

# 阶段 → (起点文件, 终点文件)。`xteam stats` 用文件 mtime 还原每片的分段耗时。
# **只有 mtime 可用** —— 协议里没有写时间戳的字段，所以这是粗粒度近似：
# 同一次写入的两个边界会被记成同一时刻（时长为 0），不是精确的相位测量。
PHASES: tuple[tuple[str, str, str], ...] = (
    ("tl_decomp", "spec.md", "request.md"),
    ("dev_impl", "request.md", "delivered.json"),
    ("tl_review", "delivered.json", "ready.json"),
    ("pm_gate", "ready.json", "verdict.json"),
    ("close", "verdict.json", "closed.md"),
)

# FAIL 修复优先于开新片：手里有没消费的 verdict（别的片）时，dev 不许开新活
# ——否则「边修上一片、边写下一片」会把两处推理搅在一起，还不容易看出来。
# 实测 FAIL 率 0/24（本仓），这个开关基本不触发；抖动大了就把它关掉（改 False）。
PREEMPT_ON_FAIL: bool = True


# ---------------------------------------------------------------- agent 能力表

# herdr 支持的 kind（`herdr agent start --help` 的 possible values）。
# 运行时还会用 Herdr.kinds_supported() 复核，这个表只是离线兜底。
HERDR_KINDS: set[str] = {
    "pi", "claude", "codex", "gemini", "cursor", "devin", "agy", "cline", "omp",
    "mastracode", "opencode", "copilot", "kimi", "kiro", "droid", "amp", "grok",
    "hermes", "kilo", "qodercli", "qwen", "maki", "muse",
}

# 口语别名 → herdr kind。只收**确实是同一个程序**的别名。
# 曾经把 `qoderclicn` 折成 `qodercli` 是错的 —— 它们是两个独立程序，只是用法看起来
# 一样。折错的后果是静默用错 agent，比报错更糟。
KIND_ALIASES: dict[str, str] = {
    "cursor-agent": "cursor",
    "open-code": "opencode",
}

# kind 的 TUI 是否认 `--model` —— **分三态，不把推断当实测**。
#
# VERIFIED：本机真跑过 `herdr agent start -- --model X` 并确认起来了。
#   devin    --model swe-2-max             argv 回显 ['devin','--model','swe-2-max'] 5.1s
#   cursor   --model gpt-5                 ✓
#   omp      --model gpt-5                 ✓
#   pi       --model anthropic/claude-opus-4-5  ✓
#   qodercli --model claude-sonnet-4-5     ✓
# ASSUMED：kander 的 agent 定义里有 `--model {model}`，机制应当一致，但**我没在本机验证过**。
#   换机器/换版本时如果某个 CLI 不认，表现为「启动超时」——真正的验证交给
#   `xteam doctor --probe`，而不是让用户拿超时去猜。
VERIFIED_TUI_MODEL: set[str] = {"devin", "cursor", "omp", "pi", "qodercli"}
ASSUMED_TUI_MODEL: set[str] = {
    "claude", "codex", "gemini", "copilot", "kimi", "grok", "cline",
    "droid", "amp", "hermes", "kilo", "qwen", "agy", "kiro", "muse",
    "maki", "mastracode",
}
TUI_MODEL_OK: set[str] = VERIFIED_TUI_MODEL | ASSUMED_TUI_MODEL

# opencode v2.0.20 的 TUI 顶层**没有** --model（只有 `opencode run` / `opencode mini`
# 有）。硬传的后果：它打印帮助后退出，herdr 等不到交互态，报
# `timed out waiting for agent startup` —— 这个报错长得像「模型名不存在」，会把人带偏。
# 试过并**都不通**的三条替代路径：OPENCODE_MODEL 环境变量、OPENCODE_CONFIG 环境变量、
# 项目级 opencode.json 的 model 字段（配置确实被读到，但 TUI 用持久化的
# 「上次所选模型」覆盖它）。所以脚本层面无法指定，只能提前拒绝。
# 补充实测（重要）：运行**中**的 opencode TUI 确实有 `/model` 斜杠命令，能弹出
# 模型选择器；但它是**交互式列表**，`/model <名字>` 带参直接提交不生效（实测仍是
# 原模型）。所以「启动时用 --model 指定」是唯一可靠的脚本化路径；运行中换模型
# 只能由人在 pane 里选。`opencode run --model X "hi"` 是一次性执行，与常驻 pane 无关。
NO_TUI_MODEL: set[str] = {"opencode"}

DEFAULT_MODEL_HINT: dict[str, str] = {
    "devin": "swe-2-max",
    "cursor": "gpt-5",
    "omp": "gpt-5",
    "pi": "anthropic/claude-opus-4-5",
    "qodercli": "claude-sonnet-4-5",
}


def probe_store_path() -> Path:
    """probe 实测结果的持久位置（用户级，跨项目跨进程）。

    之前 --probe 只改内存里的 VERIFIED_TUI_MODEL，进程一退就没了，
    却提示「下次 agents 命令会显示 ✓」—— 说了没做到。
    """
    return Path.home() / ".config" / "xteam" / "verified-agents.json"


def load_verified_agents() -> set[str]:
    """读持久化的实测结果，叠加到内置实测集上。读不到就用内置的，不报错。"""
    out = set(VERIFIED_TUI_MODEL)
    try:
        data = _read_json(probe_store_path())
        extra = data.get("verified") or []
        if isinstance(extra, list):
            out |= {str(x) for x in extra}
    except Exception:      # noqa: BLE001
        pass
    return out


def save_verified_agent(kind: str) -> None:
    """把一次成功的 probe 结果落盘，供后续 `agents` / `up` 读取。"""
    path = probe_store_path()
    data = _read_json(path)
    verified = data.get("verified") if isinstance(data.get("verified"), list) else []
    if kind not in verified:
        verified.append(kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, {"verified": sorted(verified),
                                "updated": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2)


def agent_slug(label: str) -> str:
    """workspace label → herdr agent name 可用的片段。

    herdr agent name 全局唯一且只认 `^[a-z][a-z0-9_-]{0,31}$`（首字母小写、
    最长 32 字符）—— 大写 workspace（如 `Sites`）原样拼成 `pm-Sites`
    会被直接拒绝（invalid_agent_name）。所以所有 agent/tab/pane 命名
    必须走 agent_name()，不许手拼 `f"{role}-{label}"`。
    """
    slug = re.sub(r"[^a-z0-9_-]+", "-", label.strip().lower()).strip("-_")
    return slug or "ws"


def agent_name(role: str, label: str) -> str:
    """角色 + workspace label → 合法的 herdr agent name（含 32 字符截断）。

    正式名 `role-slug` 与 swap 临时名 `swap-role-slug` 都走这里，前缀
    永远是小写 role，所以「首字母小写」天然满足；超长时截断 slug 保上限。
    """
    name = f"{role}-{agent_slug(label)}"
    return name[:32].rstrip("-_") if len(name) > 32 else name


def normalize_kind(kind: str) -> str:
    """把口语别名折成 herdr kind。未知的原样返回，好让上层报错时能原样显示。"""
    return KIND_ALIASES.get(kind.strip().lower(), kind.strip().lower())


def suggest_kinds(kind: str) -> str:
    """给拼错的 kind 一个最接近的猜测（别名表 + 简单子串匹配）。"""
    low = kind.lower()
    for alias, target in KIND_ALIASES.items():
        if alias in low or low in alias:
            return f"{kind} → {target}"
    hits = [k for k in HERDR_KINDS if k in low or low in k]
    return ", ".join(hits[:3])


# ---------------------------------------------------------------- 模型成本约束

# 用户 2026-10-01 明确要求：**未经允许不要用 openrouter 下的模型**（其他 provider 已
# 付费，走 openrouter 产生额外开销）。
#
# 关键：**不能只按模型名判断**。真实踩过 —— 给 omp 传 `--model opus`，它模糊匹配到
# `anthropic/claude-opus-*`，而这些在 omp 的目录里全部挂在 openrouter provider 下
# （559 个模型），名字里根本没有 "openrouter" 字样。
#
# omp 目录结构反直觉（实测 660 个模型）：
#   · 直连 provider（deepseek/openai/openai-codex/opencode-go）用**裸名**
#   · openrouter 用 **`provider/model` 前缀**镜像同一批
# 所以判据是「这个名字能不能在直连 provider 里原样找到」。
BLOCKED_PROVIDERS: frozenset[str] = frozenset({"openrouter", "openrouter-ai"})


def _omp_catalog() -> dict[str, str]:
    """omp 的模型目录：模型名 → provider。"""
    try:
        proc = subprocess.run(["omp", "models"], capture_output=True, text=True,
                              timeout=30)
    except (OSError, subprocess.SubprocessError):
        return {}
    catalog: dict[str, str] = {}
    provider = ""
    for line in proc.stdout.splitlines():
        head = re.match(r"^([a-z0-9_.-]+) \(\d+\)\s*$", line.strip())
        if head:
            provider = head.group(1)
            continue
        if not provider or "│" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("│").split("│")]
        if len(cells) >= 2 and re.match(r"^[~a-z0-9]", cells[0]):
            catalog.setdefault(cells[0].lstrip("~"), provider)
    return catalog


def model_cost_risk(kind: str, model: str, catalog: dict[str, str] | None = None) -> str:
    """模型是否落在禁用 provider 上。返回风险说明；空串表示没发现问题。

    拿不到 omp 目录时**不放行**（失败即关）：换个没装 omp 的机器就悄悄放行，
    openrouter 拦截等于形同虚设，而用户可能毫不知情地产生费用。
    """
    if not model:
        return ""
    low = model.lower()

    if "/" in model:
        provider = low.split("/", 1)[0].lstrip("~")
        if provider in BLOCKED_PROVIDERS:
            return f"{model} → provider {provider}（禁用）"
        if kind == "omp":
            return (f"{model} 带 provider 前缀 —— 在 omp 里这种写法正是 openrouter "
                    f"镜像模型的命名方式。直连线路用裸名，例如 gpt-5、"
                    f"deepseek-v4-pro。")

    if kind == "omp":
        # catalog 可注入：单测传入固定目录，就不必依赖本机实时模型表
        # （那张表会随账号/版本变，曾让同一份测试在不同机器上结论相反）
        if catalog is None:
            catalog = _omp_catalog()
        if not catalog:
            return (f"读不到 omp 的模型目录（omp 未安装，或 `omp models` 执行失败）"
                    f"—— 无法确认 {model} 走哪条线路。为避免误用 openrouter 线路"
                    f"（额外付费），这里不放行。请装 omp，或换用其他 agent。")
        direct = {n for n, p in catalog.items() if p not in BLOCKED_PROVIDERS}
        if low in direct:
            return ""                      # 直连 provider 里原样存在 → 已付费线路
        blocked_hits = [n for n, p in catalog.items()
                        if low in n.lower() and p in BLOCKED_PROVIDERS]
        if blocked_hits:
            sample = "、".join(sorted(blocked_hits)[:2])
            return (f"{model} 在 omp 的直连 provider 里不存在，但会模糊匹配到 "
                    f"openrouter 的 {len(blocked_hits)} 个模型（如 {sample}）"
                    f"—— 那是额外付费线路")
        return (f"{model} 不在 omp 的模型目录里，模糊匹配结果不可预测。"
                f"用 `omp models` 确认，或写一个直连 provider 里的裸名。")

    if "openrouter" in low:
        return f"{model} 名字里含 openrouter"
    return ""



def tui_model_ok(kind: str) -> bool:

    """这个 kind 的 TUI 认不认 `--model`。**判定与展示的唯一来源。**

    以前这里是 `kind in TUI_MODEL_OK`（纯静态），而展示读的是
    `load_verified_agents()`（静态 + 本机 probe 结果）。两套来源的后果是：
    `doctor --probe` 把一个新 agent 验证通过、界面上也显示 ✓，但
    `up --set-model` 仍然拒绝——**系统学到了却不用，等于没学**。
    现在两边都走这里，probe 的结果真的生效。

    实测记录的优先级高于静态推断：静态表是旧机器上的观测，用户刚在本机
    跑出来的 probe 更新鲜。
    """
    k = normalize_kind(kind)
    if k in load_verified_agents():
        return True
    return k in TUI_MODEL_OK


# ---------------------------------------------------------------- 身份注入

# 把角色身份**放进系统提示词**的 kind 表：kind → 追加系统提示词的 CLI flag。
#
# 存在的理由：`/new`、`/clear`、上下文压缩都清对话，**不清系统提示词**。
# 身份（你是哪个角色、章程是什么、上下文没了先干什么）放这里，
# 「会话一重置就失忆重来」这个前提就不成立了 —— 那正是长跑里最贵的故障：
# 人一 /new，agent 退回「你好，请问要做什么」，整条协作链要重新解释一遍。
#
# **实测是唯一入选标准**（2026-10-07）：
#   · omp 18.4.9：`herdr agent start … -- --append-system-prompt <file>` 起的 TUI，
#     `/new`（换会话，会话文件路径随之变化）与 `/clear`（Context reset — N
#     messages dropped; session continues）都清不掉这份身份；
#   · pi：同一套 TUI（omp 是它的发行版），同样认这个 flag、同样扛过 `/new`。
# 换 kind 进来之前同样要实测：起一个 pane 投这个 flag，/new 之后再问它
# 「你是谁、该干什么」，答得出来才算。`xteam doctor --probe` 只管 --model，
# 这条不在它的射程内。
SYSTEM_PROMPT_ARG: dict[str, str] = {
    "omp": "--append-system-prompt",
    "pi": "--append-system-prompt",
}

# 哪些 kind 有「只读子代理」可用（`task`/subagent 一类）。口径同
# SYSTEM_PROMPT_ARG：**只在表里才承认**。没实测过的不写 ✓ —— 让 agent 以为自己
# 能扇出、结果没有，比明说「未实测」更糟：它会编出一份没有证据的结论。
SUBAGENT_CAPABLE: set[str] = {"omp"}

# kind → 输入框提示符。`xteam say` 靠它判断「框里有没有东西」：
# 框里有草稿时投递 = herdr 的编码回车把「人打的一半 + 门铃文本」一起提交。
# **只在实测过的 kind 上写**（口径同 SYSTEM_PROMPT_ARG）：认不出来就退回旧行为
# （不预检、不补键）——绝不对着不认识的面板瞎按回车。
INPUT_BOX_MARK: dict[str, str] = {
    "omp": "╰─",     # 实测 omp 18.x：空框是 `╰─`，草稿/粘贴块是 `╰─ <内容>`
}

# omp 会把「大块粘贴」收成挂在输入框里的**粘贴块**（`╰─ txt #N`）而不是行内文本，
# 而 herdr 那次编码回车不提交它 —— 要再补一次 enter 才发得出去。实测边界
# （2026-10-07）：90 行还是行内提交，100 行变成粘贴块。留余量：**≥90 行就复核**。
# 短消息不复核（复核要轮询 3 秒），所以门铃的常规路径一点没变慢。
PASTE_CHIP_LINES = 90


def subagent_capability(kinds) -> str:
    """把 kind 列表渲染成一行「谁有只读子代理」：`omp ✓ / agy ?（未实测）`。"""
    return " / ".join(f"{k} ✓" if k in SUBAGENT_CAPABLE else f"{k} ?（未实测）"
                      for k in sorted(kinds))


# 「停在选择题上」的识别标记。实测（omp 18.x）：底部提示行是
# `Space toggle · Enter next · Up/Down move · Tab/Left/Right · Esc cancel`，
# 选项行是 `| [ ] 应用管理 |`。**只用来报告，不用来自动作答** —— 替 agent
# 选是另一条路（send_choice，带白名单与不可逆拦截）。
CHOICE_MARKERS: tuple[str, ...] = (
    "space toggle", "enter next", "esc cancel", "other (type your own)",
)


def detect_choice_prompt(tail: str) -> bool:
    """pane 尾部是不是停在「选择题/多选框」上（等一个答案）。"""
    low = tail.lower()
    return any(m in low for m in CHOICE_MARKERS)


def _ansi(text: str, code: str, on: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if on else text


def render_board(state: dict, color: bool = False) -> str:
    """把看板状态渲染成一屏文本。**纯函数**：采集在 bin/xteam（要 herdr）。

    看板只回答三件事：谁在干什么、**链路为什么不动**、最近发生了什么。
    它**不新增任何状态** —— 事实仍然只在 `.xteam/` 与 pane 现状里，看板只是
    把同一份事实渲染成一眼能看完的一屏（`status` 是快照，这个是常驻视图）。
    """
    out = [_ansi(str(state.get("title") or "xteam board"), "1", color), ""]
    roles = state.get("roles") or []
    out.append(_ansi("── 角色 ─────────────────────────────────────────────", "36", color))
    if roles:
        for r in roles:
            owes = r.get("owes") or "none"
            where = f" @ {r['where']}" if r.get("where") else ""
            box = {"draft": "   ✍ 输入框有草稿（投不进去）",
                   "unknown": ""}.get(str(r.get("box") or ""), "")
            held = str(r.get("held") or "").ljust(8)
            out.append(f"  {str(r.get('role','')):<4} {str(r.get('status','')):<8} "
                       f"{held} {owes}{where}{box}")
    else:
        out.append("  （没有在跑的角色）")

    alerts = state.get("alerts") or []
    if alerts:
        out.append("")
        out.append(_ansi("── 链路（为什么不动）──────────────────────────────", "36", color))
        for a in alerts:
            for line in str(a).splitlines():
                out.append("  " + _ansi(line, "33", color))

    slices = state.get("slices") or []
    out.append("")
    out.append(_ansi("── 切片 ─────────────────────────────────────────────", "36", color))
    if slices:
        for s in slices:
            out.append(f"  {str(s.get('name','')):<28} [{s.get('stage','')}]"
                       f"  {s.get('note','')}")
    else:
        out.append("  （没有未闭合切片）")

    events = state.get("events") or []
    if events:
        out.append("")
        out.append(_ansi("── 最近事件 ─────────────────────────────────────────", "36", color))
        for e in events:
            out.append("  " + str(e))

    panes_live = []
    for r in roles:
        tail = r.get("tail")
        if tail:
            panes_live.append((r.get("role", ""), tail))
    if panes_live:
        out.append("")
        out.append(_ansi("── 实时活动 (Pane Preview · 0 Token) ──────────────────", "36", color))
        for r_name, lines in panes_live:
            prefix = _ansi(f"  [{r_name:<4}]", "32", color)
            for idx, line in enumerate(lines):
                bullet = "└─" if idx == len(lines) - 1 else "├─"
                out.append(f"{prefix} {bullet} {line[:95]}")
    return "\n".join(out)

# 原地开新会话的斜杠命令：kind → 命令。
# 实测 omp / pi：`/new` 开新会话（omp 的 herdr `agent_session` 路径随之变化），
# 而 `/clear` 只是「丢消息、会话继续」—— 重开要的是前者的效果。
RESET_COMMAND: dict[str, str] = {"omp": "/new", "pi": "/new"}

# 「新会话真的开了」的回执：omp 打 `[ok] New session started`，pi 打
# `✓ New session started`。herdr 的 agent_session 只对 omp 这类 kind 提供，
# 对 pi 没有 —— 所以做重开时这条回执是**兜底的生效判据**（数出现次数，
# 不是「有没有」：TUI 的滚动区里可能留着上一次的）。
RESET_EVIDENCE = "New session started"

# context 占用超过这里就建议原地重开（`xteam reopen`）。为什么给人建议而不是
# 自动重开：重开丢掉的正是对话里**还没落文件**的推理，值不值得丢由人判断；
# 但提醒不能省 —— 上下文一满，agent 就开始「压缩之后凭印象干活」。
REOPEN_ADVISE_PCT = 75
REOPEN_ADVISE_COOLDOWN = 1800      # 同一角色两次提醒之间的最短间隔（秒）


def identity_path(root: Path, role: str) -> Path:
    """角色身份文件的落盘位置（.xteam/identity/<role>.md，随 .xteam 一起 gitignore）。"""
    return root / XTEAM_DIRNAME / "identity" / f"{role}.md"


IDENTITY_TEMPLATE = """\
你是「{label}」项目的 {role}（{cn}）—— xteam 三 pane 协作（pm / tl / dev）的现任成员。
项目根目录：{root}
你在一个 pane 里运行（环境变量 XTEAM_ROLE={role}）。

## 会话被重置之后（/new、/clear、上下文压缩、被换人重开）
身份不会丢 —— 它就在这条系统提示词里；重置清掉的是对话，不是它。
但**现状必须重新读**：你是进行中的项目里的 {role}，不是今天才接手。
新会话的第一条消息处理之前：
  1. 跑 `xteam whoami` —— 你欠什么义务、哪些切片未闭合、你上次的 recap、时间线尾部
  2. 还不够就读 `.xteam/PROTOCOL.md`（协议全文）与 `.xteam/memory/*-recap.md`
  3. **不要问「现在该做什么」** —— 答案在 whoami 的输出里
你会真的丢掉的只有对话细节。要紧的结论一律先落文件（工件 / recap / stamp），
别只留在对话里 —— 对话是易失的，.xteam/ 不是。

## 角色章程（全文，长期有效）
{charter}
"""


def write_identity(root: Path, role: str, roles_dir: Path, label: str = "") -> Path:
    """生成角色的「系统提示词版」身份文件，返回路径。

    up / swap / reopen 都往这里写一份，再把路径挂进 agent 的启动参数
    （SYSTEM_PROMPT_ARG），让身份常驻系统提示词。与 roles/<role>.md 的关系：
    章程全文一字不动地附在后面，前面那段引导**只有这份文件才有** ——
    告诉一个刚被清空上下文的 agent「你是谁、先跑 whoami、别问该做什么」。

    roles_dir 由调用方给（和 stale_rules 一样）：资源目录的解析只有一个来源
    （bin/xteam 的 `_find_root()`），这里不自己再推一遍。
    """
    charter_path = roles_dir / f"{role}.md"
    charter = charter_path.read_text(encoding="utf-8") if charter_path.exists() else ""
    cn = ROLES.get(role, ("", ""))[1]           # 未知角色给空中文名，不炸
    text = IDENTITY_TEMPLATE.format(label=label or root.name, role=role,
                                    cn=cn, root=root, charter=charter)
    path = identity_path(root, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def context_hint(role: str, kind: str) -> str:
    """门铃前缀：给「身份不在系统提示词里」的 kind 补一句身份与自检提示。

    支持的 kind 返回空串 —— 它们的身份常驻系统提示词，每条门铃再贴一遍只是
    噪音。不支持的 kind（身份只能投在对话里）必须贴：人一 /new，那不是
    「忘了一点」，是**全部忘光**，连自己是谁都不知道。
    """
    if kind in SYSTEM_PROMPT_ARG:
        return ""
    return (f"【xteam】你是 {role}。若上下文是新的（/new、/clear、压缩之后）："
            f"先跑 `xteam whoami` 读现状，不要从头问。\n\n")


def reset_command(kind: str) -> str:
    """原地开新会话的命令；这个 kind 没有就返回空串（调用方转 `xteam swap`）。"""
    return RESET_COMMAND.get(kind, "")


# ---------------------------------------------------------------- 角色规格


@dataclass(frozen=True)
class RoleSpec:
    """一个角色的启动规格：用什么 agent、什么模型、身份文件在哪。"""

    role: str
    kind: str          # herdr agent kind
    cn: str            # 中文名
    model: str = ""    # 模型名，写法由 kind 决定
    identity: str = "" # 身份文件路径（走系统提示词注入的 kind 才有）

    def supports_tui_model(self) -> bool:
        # 走 tui_model_ok 而不是静态表 —— 否则 probe 验证过的 agent 会
        #「显示支持但实际拒绝」，详见 tui_model_ok 的说明。
        return tui_model_ok(self.kind)

    def is_verified(self) -> bool:
        """这个 kind 的 --model 能力是实测过的吗（而非推断）。"""
        return self.kind in load_verified_agents()

    def agent_args(self) -> list[str]:
        """传给 `herdr agent start -- <args>` 的参数。"""
        args: list[str] = []
        if self.model and self.supports_tui_model():
            args += ["--model", self.model]
        # 身份文件走系统提示词（kind 支持时）：/new、/clear、压缩都清不掉它。
        # 不支持的 kind 维持原样 —— 它们的身份由 up/swap/reopen 投进对话，
        # 门铃再补一句 context_hint 兜底。
        flag = SYSTEM_PROMPT_ARG.get(self.kind, "")
        if self.identity and flag:
            args += [flag, self.identity]
        return args

    def preflight(self, allow_openrouter: bool = False) -> None:
        """启动前把「起不来 / 会多花钱」讲清楚，别让它白等 120s 超时。"""
        # openrouter 只**提示**不拦（用户 2026-10-02：「可以类似使用 omp 来选择，
        # 不用强行限死」）。但要留痕 —— 万一账单上冒出意外，至少日志里有据可查。
        risk = model_cost_risk(self.kind, self.model)
        if risk:
            note = f"{self.role}: 注意，{risk}（这是额外付费线路，非已订阅的 provider）"
            suffix = ("已用 --allow-openrouter 显式授权" if allow_openrouter
                      else "如需免打扰可加 --allow-openrouter")
            print(f"\033[33m[warn]\033[0m {note} —— {suffix}")
        if self.kind not in HERDR_KINDS:
            raise RuntimeError(
                f"{self.role}: herdr 不认识 agent kind {self.kind!r}。\n"
                f"  herdr 支持：{', '.join(sorted(HERDR_KINDS))}\n"
                + (f"  你是不是想写：{suggest_kinds(self.kind)}？\n"
                   if suggest_kinds(self.kind) else "")
                + "  注意 mcode / qoderclicn 是各自独立的程序，不在 herdr kind 列表里。"
            )
        if self.model and not self.supports_tui_model():
            alts = "、".join(sorted(VERIFIED_TUI_MODEL))
            raise RuntimeError(
                f"{self.role}: {self.kind} 的交互 TUI 不支持 --model"
                f"（本机 opencode v2.0.20 实测），无法脚本化指定模型。\n"
                f"  两个办法：\n"
                f"    1. 换成实测支持的 agent："
                f"xteam up --set-agent {self.role}=devin"
                f" --set-model {self.role}=swe-2-max\n"
                f"       （实测可用的：{alts}）\n"
                f"    2. 让 {self.kind} 用它自己的配置：改 opencode.json 的 model 字段，"
                f"或在 TUI 里手动选一次（它会记住并覆盖配置）。\n"
                f"  当前 model={self.model}"
            )

    def preflight_env(self, env: "Herdr", supported: set[str] | None = None) -> None:
        """换机器时的环境体检，在建任何东西之前跑。

        三层检查：herdr 在不在 / 这台机器的 herdr 认不认这个 kind（运行时探测）/
        agent CLI 装了没。提前查是为了给可执行的错误，而不是让 herdr 报「启动超时」——
        后者会把人引到「模型名有问题」的错误方向。
        """
        if shutil.which(env.bin) is None:
            raise RuntimeError(
                "找不到 herdr —— 它是 xteam 的地基（管 pane / agent 状态 / 门铃）。\n"
                "  装好并启动它的 server 后再试：herdr"
            )
        if supported is not None and self.kind not in supported:
            raise RuntimeError(
                f"{self.role}: 本机 herdr 不支持 agent kind {self.kind!r}。\n"
                f"  本机支持：{', '.join(sorted(supported))}\n"
                f"  换别的 agent：--set-agent {self.role}=<kind>（`xteam agents` 看可选）"
            )
        if not env.agent_installed(self.kind):
            raise RuntimeError(
                f"{self.role}: 找不到 {self.kind} 的可执行文件（不在 PATH 上）。\n"
                f"  先装 {self.kind}，确认 `{self.kind} --version` 能跑，再执行 xteam up。\n"
                f"  或换别的 agent：--set-agent {self.role}=<kind>（`xteam agents` 看可选）"
            )

    def describe(self) -> str:
        if self.model and not self.supports_tui_model():
            return f"{self.kind}（不支持脚本指定模型，将用它自身配置）"
        base = f"{self.kind} {self.model}".strip()
        return base + ("" if self.is_verified() or not self.model else "（未验证）")

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.role}({self.describe()})"


# ---------------------------------------------------------------- herdr 访问


class Herdr:
    """herdr CLI 的薄封装。JSON 解析集中在这里，失败一律抛出。"""

    def __init__(self, binary: str = "herdr", scope: str = "") -> None:
        self.bin = shutil.which(binary) or binary
        self.scope = scope   # agent name 全局唯一，用 workspace 做命名空间

    def _run(self, *args: str, timeout: int = 30) -> dict:
        """跑一条 herdr 命令并解析 JSON。

        timeout 逐命令给：`agent start` 要等 agent 起来（90s），统一 30s 会在就绪前掐掉。
        容忍**空输出**：`pane send-text` / `send-keys` 成功时 stdout 是空的。
        """
        try:
            proc = subprocess.run([self.bin, *args], capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"herdr {' '.join(args)} 超过 {timeout}s 未返回") from exc
        if proc.returncode != 0:
            raise RuntimeError(
                f"herdr {' '.join(args)} rc={proc.returncode}: "
                f"{(proc.stderr or proc.stdout).strip()[:200]}")
        text = (proc.stdout or "").strip()
        if not text:
            return {}
        try:
            return json.loads(text).get("result", {})
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"herdr {' '.join(args)} 输出非 JSON: {text[:200]}") from exc

    def kinds_supported(self) -> set[str] | None:
        """本机 herdr 实际支持的 kind；读不到返回 None。"""
        try:
            proc = subprocess.run([self.bin, "agent", "start", "--help"],
                                  capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        m = re.search(r"\[possible values:\s*([^\]]+)\]",
                      (proc.stdout or "") + (proc.stderr or ""))
        return {v.strip() for v in m.group(1).split(",") if v.strip()} if m else None

    def agent_installed(self, kind: str) -> bool:
        return shutil.which(kind) is not None

    # -- 查询 --

    def workspaces(self) -> list[dict]:
        return self._run("workspace", "list").get("workspaces", [])

    def agents(self) -> list[dict]:
        return self._run("agent", "list").get("agents", [])

    def find_workspace(self, label: str) -> dict | None:
        for ws in self.workspaces():
            if ws.get("label") == label:
                return ws
        for ws in self.workspaces():
            if ws.get("workspace_id", "").lower().startswith(label.lower()):
                return ws
        return None

    def role_map(self, workspace_id: str) -> dict[str, dict]:
        """role -> agent 记录。只认 agent name（role-scope 形式），不认终端标题。"""
        out: dict[str, dict] = {}
        for agent in self.agents():
            if not agent.get("pane_id", "").startswith(f"{workspace_id}:"):
                continue
            role = str(agent.get("name") or "").split("-", 1)[0]
            if role in ("pm", "tl", "dev", "reviewer"):
                out[role] = agent
        return out

    def agent_status(self, pane_id: str) -> str:
        for agent in self.agents():
            if agent.get("pane_id") == pane_id:
                return str(agent.get("agent_status", "unknown"))
        return "absent"

    def agent_session(self, pane_id: str) -> str:
        """pane 当前会话的标识（herdr 只对部分 kind 提供，如 omp 的会话文件路径）。

        用途是**判会话是否被换过**：`/new` 会换（新会话文件），`/clear` 不会
        （丢消息、会话继续）—— 所以它只能证明「换了」，不能证明「没换」。
        读不到（kind 不提供、agent 不在）返回空串，调用方自己决定怎么兜底。
        """
        for agent in self.agents():
            if agent.get("pane_id") == pane_id:
                return str((agent.get("agent_session") or {}).get("value") or "")
        return ""

    def read_pane(self, pane_id: str, lines: int = 60,
                  source: str = "recent") -> str:
        """读 pane 输出。`source="visible"` 读当前屏（输入框在最底下那几行）。"""
        proc = subprocess.run([self.bin, "pane", "read", pane_id,
                               "--lines", str(lines), "--source", source],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"herdr pane read {pane_id} rc={proc.returncode}")
        return proc.stdout

    # -- 变更 --

    def create_workspace(self, cwd: str, label: str) -> dict:
        return self._run("workspace", "create", "--cwd", cwd, "--label", label)

    def create_tab(self, workspace_id: str, cwd: str, label: str,
                   env: dict[str, str] | None = None) -> dict:
        """建 tab。env 在 pane 启动前注入——`xteam stamp` 靠它自动推断角色。"""
        args = ["tab", "create", "--workspace", workspace_id, "--cwd", cwd,
                "--label", label, "--no-focus"]
        for key, value in (env or {}).items():
            args += ["--env", f"{key}={value}"]
        return self._run(*args)

    def panes_of_tab(self, workspace_id: str, tab_id: str) -> list[str]:
        panes = self._run("pane", "list", "--workspace", workspace_id).get("panes", [])
        return [p["pane_id"] for p in panes if p.get("tab_id") == tab_id]

    def start_agent(self, pane_id: str, tab_id: str, spec: RoleSpec,
                    name: str | None = None) -> None:
        """拉起 agent 并在三个寻址命名空间固定同一个名字。

        agent name 全局唯一 → 用 `role-<slug>` 做命名空间（slug 经 agent_name()
        合法化，大写/空格 workspace 也能起）。

        `name` 可覆盖，用于 swap 的**两阶段**：新 agent 先用临时名
        （如 `swap-tl-<slug>`）起，成功后再 rename 成正式名。这样同一角色的
        新旧 agent 不会同时占用正式名，也就不存在「起新的要先把旧的
        关掉」这个不可回滚的顺序。
        """
        name = name or agent_name(spec.role, self.scope)
        argv = ["agent", "start", name, "--kind", spec.kind,
                "--pane", pane_id, "--timeout", "90000"]
        extra = spec.agent_args()
        # **extra 必须真的拼进 argv。** 算对了却没传出去，等于每个 agent 都跑在
        # 默认模型上，而 set-model / omp 默认这套设计整个失效、且没有任何测试会红
        # （原来的断言全都只在单独调 agent_args()）。所以下面 test_protocol 里有
        # 一条真跑 start_agent 抓 argv 的断言守着这里。
        if extra:
            argv += ["--"] + extra
        try:
            self._run(*argv, timeout=120)   # agent start 要等就绪，外层留余量
        except RuntimeError as exc:
            # **只认真正的超时。** 原来是 `"timeout" in str(exc)` 子串匹配，
            # 但 argv 里本来就有 `--timeout 90000`（被 _run 回显进错误串），
            # 所以任何快速失败（如 invalid_agent_name rc=1）都被谎报成
            # 「90s 内没进入交互态」—— 本次 Sites 故障就是这么被误导的。
            # 真超时只有两种：subprocess 超时（"超过 …s 未返回"）和 herdr
            # 服务端的等待超时（timed out waiting）。
            msg = str(exc).lower()
            if "未返回" in msg or "timed out waiting" in msg:
                # **不要断言是模型参数的问题。** 实测 claude 不带 --model 也一样超时
                # （首次运行的 onboarding/登录屏，herdr 检测不到交互态）。把原因归到
                # 模型上会让人往错方向查。给出能区分两种原因的下一步。
                raise RuntimeError(
                    f"{spec.role} 启动超时：agent 在 90s 内没进入交互态。\n"
                    f"  当前 kind={spec.kind}，model={spec.model or '(默认)'}。\n"
                    f"  两种常见原因，请分别排除：\n"
                    f"    1. 该 CLI 的 TUI 不认 --model → 去掉 model 再试\n"
                    f"    2. CLI 卡在首次登录/onboarding 屏 → 先在终端手跑一次 "
                    f"`{spec.kind}` 完成初始化\n"
                    f"  `xteam doctor --probe {spec.kind}` 可复现并区分。"
                ) from exc
            raise
        self._run("agent", "rename", pane_id, name)
        self._run("pane", "rename", pane_id, name)
        self._run("tab", "rename", tab_id, name)

    def detect_by_cwd(self, cwd: Path) -> list[tuple[str, int]]:
        """哪些 workspace 的 pane 位于 cwd 之内（含 cwd 自身或其祖先）。

        返回 [(label, 匹配深度)]，**深度大的是最贴切的**。这是「我现在在哪个
        workspace」的可靠判据：herdr 的 `focused` 在非交互查询下恒为 False，
        靠不住；而 pane 的 cwd 是事实。
        """
        try:
            here = cwd.resolve()
        except OSError:
            return []
        out: list[tuple[str, int]] = []
        for ws in self.workspaces():
            ws_id = ws.get("workspace_id", "")
            best = -1
            try:
                for pane in self._run("pane", "list", "--workspace", ws_id).get(
                        "panes", []):
                    raw = pane.get("cwd") or ""
                    if not raw:
                        continue
                    try:
                        pc = Path(raw).resolve()
                    except OSError:
                        continue
                    if here == pc or pc in here.parents:
                        best = max(best, len(pc.parts))
            except (RuntimeError, KeyError):
                continue
            if best >= 0:
                out.append((str(ws.get("label") or ws_id), best))
        out.sort(key=lambda x: -x[1])
        return out

    def close_workspace(self, workspace_id: str) -> None:
        self._run("workspace", "close", workspace_id)

    def close_tab(self, tab_id: str) -> None:
        """关掉整个 tab（连同里面的 agent）。

        **换 agent 只能走这条路**：herdr 没有 `agent kill`/`agent delete`，
        单独杀不掉一个 agent。每个角色独占一个 tab，所以「换掉这个角色的
        agent」= 关掉它的 tab 再建一个。
        """
        self._run("tab", "close", tab_id)

    # -- 门铃 / 投递 --

    def agent_kind(self, pane_id: str) -> str:
        """这个 pane 跑的是哪个 kind（`agent list` 的 `agent` 字段）。查不到返回空串。"""
        for agent in self.agents():
            if agent.get("pane_id") == pane_id:
                return str(agent.get("agent") or "")
        return ""

    def box_state(self, pane_id: str, kind: str = "") -> str:
        """输入框里有没有东西：`empty` / `draft` / `unknown`。

        实测（omp 18.x，2026-10-07）：输入行是**最后一条以 `╰─` 开头的行** ——
        空框是 `╰─`；有人打了字、或挂了一个粘贴块（`╰─ txt #2`）就带内容。

        **聚焦的 pane 会在输入行右侧挂一句提示**（`Shift+Tab to change thinking
        effort`），用一大段空白和正文隔开。只看「第一个大空隙之前」的内容 ——
        否则空框会被判成草稿：0.2.6 就栽在这，装上去之后所有门铃都被静默跳过
        （watch 日志：`ALERT tl … → skipped-draft`），而巡检还倒过来说「催了两轮
        无响应」。判据错的方向比漏判更贵：它让整条链看起来是 agent 不干活。

        认不出的 kind / 读不到 / 找不到提示符 → `unknown`：调用方一律退回旧行为，
        **不预检也不补键**。
        """
        mark = INPUT_BOX_MARK.get(kind or self.agent_kind(pane_id))
        if not mark:
            return "unknown"
        try:
            tail = self.read_pane(pane_id, lines=40, source="visible")
        except Exception:                        # noqa: BLE001
            return "unknown"
        for line in reversed(tail.splitlines()):
            if line.startswith(mark):
                # 大空隙（≥3 空格）之后是右侧提示，不是人打的字。
                head = re.split(r"\s{3,}", line[len(mark):], maxsplit=1)[0]
                return "draft" if head.strip() else "empty"
        return "unknown"

    def _poll_box(self, pane_id: str, kind: str, want: str,
                  timeout_s: float) -> str:
        """轮询输入框直到变成 `want`（或超时），返回最后一次读到的状态。

        **必须轮询**：omp 渲染粘贴块比 `agent prompt` 返回晚 —— 实测返回只要
        0.31s，粘贴块 t+0.5s 才出现在输入行上，单次读会读到「还没渲染」的旧屏。
        """
        deadline = time.time() + timeout_s
        state = self.box_state(pane_id, kind)
        while state != want and time.time() < deadline:
            time.sleep(0.25)
            state = self.box_state(pane_id, kind)
        return state

    def _ensure_submitted(self, target: str, kind: str) -> bool:
        """长消息投完的复核：被收成粘贴块就补回车，直到输入框空。返回是否已提交。

        **只该在「投之前框是空的」之后调用** —— 那时框里的东西必然是刚投进去的，
        补回车不会连坐别人的字。最多补两次：第一次提交，第二次给渲染慢的余地。
        """
        for _ in range(2):
            if self._poll_box(target, kind, "draft", 3.0) != "draft":
                return True                      # 一直是空框 = 行内提交成功
            try:
                self._run("pane", "send-keys", target, "enter")
            except RuntimeError:
                return False
            if self._poll_box(target, kind, "empty", 2.0) == "empty":
                return True
        return False

    def _prompt(self, target: str, message: str) -> tuple[int, str]:
        """`agent prompt` 投一次。返回 (rc, stderr)。**测试用替身覆盖这一层。**"""
        proc = subprocess.run([self.bin, "agent", "prompt", target, message],
                              capture_output=True, text=True, timeout=30)
        return proc.returncode, proc.stderr.strip()

    def doorbell(self, target: str, message: str, wait_s: float = 0.0,
                 kind: str = "", wait_empty_s: float = 15.0) -> str:
        """给一个 agent 发消息（走 `agent prompt`）。

        对 blocked 的 agent 无效（会被拒）——那种情况用 send_choice。

        **两条实测判据（2026-10-07，omp 18.x）：**

        1. **框里有草稿就不投。** 人可能正在这个 pane 里打字；直接投，herdr 那次
           编码回车会把「人打的一半 + 门铃文本」一起提交（实测：草稿被连坐发出，
           agent 收到一句人还没写完的话）。所以先看框，非空就等它空；等不到就
           返回 `skipped-draft` **什么都不投**（宁可不叫醒，也不替人按回车）。
        2. **投完复核框里还留着没有。** omp 把长多行消息（`sync` 投的就是章程全文）
           收成粘贴块挂在输入框（`╰─ txt #N`），herdr 那次回车不提交它 —— 实测
           需要再补一次 enter。补键**只在「投之前框是空的」前提下**做：那时框里的
           东西必然是刚投进去的，不会连坐别人的字。
        """
        kind = kind or self.agent_kind(target)
        known = kind in INPUT_BOX_MARK
        box = self.box_state(target, kind)
        if known:
            # 可见屏偶尔读不到输入行（渲染竞态）：重试几次，别把「读不到」当「空框」。
            for _ in range(3):
                if box != "unknown":
                    break
                time.sleep(0.25)
                box = self.box_state(target, kind)
        if box == "draft":
            deadline = time.time() + wait_empty_s
            while time.time() < deadline and self.box_state(target, kind) == "draft":
                time.sleep(0.5)
            if self.box_state(target, kind) == "draft":
                return (f"skipped-draft: 输入框里有草稿，等了 {wait_empty_s:.0f}s "
                        f"没等到空 —— 没投（投了会把你打的一半一起发出去）")
        rc, err = self._prompt(target, message)
        if rc != 0:
            return f"prompt-failed: {err[:120]}"
        submitted = True
        if known and box == "empty" and message.count("\n") + 1 >= PASTE_CHIP_LINES:
            submitted = self._ensure_submitted(target, kind)
        if wait_s > 0:
            time.sleep(wait_s)
        tail = "" if submitted else ";submit=stuck"
        return f"status={self.agent_status(target)}{tail}"

    def send_choice(self, pane_id: str, text: str, settle_s: int = 5) -> tuple[bool, str]:
        """给**停在选项 UI 上**的 agent 提交一个选择。

        必须用 pane send-text + send-keys enter：agent blocked 时 agent prompt 被拒。
        返回 (是否确认送达, 说明)。提交后核对状态真的变了才算送达。
        """
        before = self.agent_status(pane_id)
        for cmd in (["pane", "send-text", pane_id, text],
                    ["pane", "send-keys", pane_id, "enter"]):
            try:
                self._run(*cmd)
            except RuntimeError as exc:
                return False, str(exc)[:120]
        if settle_s > 0:
            time.sleep(settle_s)
        after = self.agent_status(pane_id)
        if after == before and after == "blocked":
            return False, f"提交后仍停在 blocked（{before} → {after}）"
        return True, f"{before} → {after}"

    def send_initial_prompt(self, target: str, text: str, wait_s: int = 20,
                            kind: str = "") -> str:
        return self.doorbell(target, text, wait_s=wait_s, kind=kind)

    def wait_state(self, target: str, states: tuple[str, ...], timeout_ms: int) -> str:
        args = [self.bin, "agent", "wait", target, "--timeout", str(timeout_ms)]
        for state in states:
            args += ["--until", state]
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout_ms / 1000 + 15)
        return proc.stdout.strip() or ("timeout" if proc.returncode else "unknown")


# ---------------------------------------------------------------- 协议状态机

# 默认角色跑在 omp 上，**不是 opencode**。
# 理由（用户 2026-10-05 给的）：opencode 的交互 TUI 不支持 --model（实测 v2.0.20），
# 于是只能吃它自己的 opencode.json 里的模型 —— 那台机子上默认落到 deepseek，
# 既**指定不了**也**看不见**。omp 实测认 --model，模型是可写进 team.json 的事实。
# 想换 opencode 随时 `xteam up --set-agent pm=opencode`，只是模型不可控。
DEFAULT_ROLES: dict[str, tuple[str, str]] = {
    "pm": ("omp", "产品经理"),
    "tl": ("omp", "团队负责人"),
    "dev": ("devin", "开发"),
    "reviewer": ("omp", "独立评审"),
}
# devin 主要用于开发，默认走 swe-2-max（用户 2026-10-02 定的）。
# 只改启动参数，不进 RoleSpec.model —— 那样 status 会显示「默认模型」而不是实际模型。
DEFAULT_START_MODEL: dict[str, str] = {"devin": "swe-2-max"}
BOOT_ORDER = ["pm", "tl", "dev", "reviewer"]
# 向后兼容别名：CLI 用 ROLES 查中文名
ROLES = DEFAULT_ROLES


# 读到过的损坏文件。**记下来而不是只吞掉** —— 静默返回 {} 会把「状态坏了」
# 伪装成「没有状态」，而排查最费时间的恰恰是这种：看到一片空白，却不知道文件
# 是本来就空、还是坏了。同一路径只记一次，免得每轮巡检刷屏。
CORRUPT_JSON: set[str] = set()


def _note_corrupt(path: Path, why: str) -> None:
    key = str(path)
    if key in CORRUPT_JSON:
        return
    CORRUPT_JSON.add(key)
    sys.stderr.write(
        f"[xteam] ⚠ {path} 读不出来（{why}）—— 按空内容继续。"
        f"若本该有内容，多半是上一次写入被中断；该文件可安全重建。\n")


def json_health() -> list[str]:
    """本次进程里读到过的损坏 JSON 路径。"""
    return sorted(CORRUPT_JSON)


def _read_json(path: Path) -> dict:
    """读一个 JSON 工件，永远返回 dict。

    **必须做类型检查**：JSON 合法不等于顶层是对象。agent 写错一个字符就可能产出
    `[1,2]` 或 `null`，直接返回会让下游所有 .get() 抛 AttributeError，把 status
    和整个巡检带崩。这些文件是 agent 写的，畸形是迟早的事。

    **但不能静默。** 读不出来时除了返回 {}，还要往 stderr 记一笔 ——
    否则「文件坏了」和「文件本来就空」在输出里长得一模一样。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        _note_corrupt(path, type(exc).__name__)
        return {}
    if not raw.strip():
        return {}                      # 空文件是合法状态，不算损坏
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _note_corrupt(path, f"JSON 解析失败 line {exc.lineno} col {exc.colno}")
        return {}
    if not isinstance(data, dict):
        _note_corrupt(path, f"顶层是 {type(data).__name__} 而不是对象")
        return {}
    return data


def write_json(path: Path, data, indent: int = 2) -> bool:
    """原子写 JSON：先写临时文件，再 `os.replace` 覆盖。

    **为什么不用锁，用原子替换。** 这个项目的共享状态几乎都是单写者：
    `idle.state` 只有巡检写，`index.json` 巡检 + 一次性 init，
    `session.json` / `team.json` 是人工触发、一次一个命令。真正会发生的故障是
    **写到一半被中断**（进程被杀、机器休眠），留下一截 JSON —— 而
    `os.replace` 在同一文件系统内是原子的，读者要么看到旧的完整内容，要么看到
    新的完整内容，不存在中间态。

    加锁反而更糟：锁必须解决「持有者已经死了」，否则进程被 kill 之后那个文件
    就**永远锁死** —— 为防「文件写坏」引入「文件再也动不了」，不划算。

    跨文件系统时 `os.replace` 会失败（EXDEV），此时退回直写并如实报错。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, ensure_ascii=False, indent=indent)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        # 临时文件与目标不在同一文件系统等：宁可直写并让它失败，也不要静默丢数据
        try:
            path.write_text(body, encoding="utf-8")
            return True
        except OSError:
            return False


def _round(path: Path, *keys: str) -> int:
    """从 JSON 工件里取一个 round 值，按 keys 优先级找第一个非零的。"""
    data = _read_json(path)
    for key in keys:
        try:
            value = int(data.get(key, 0) or 0)
        except (TypeError, ValueError):
            continue
        if value:
            return value
    return 0


def _verdict_round(task: Path) -> int:
    return _round(task / "verdict.json", "round")


def _consumed_round(task: Path) -> int:
    # 规范键是 round；consumed_round 是历史写法，兼容读。
    return _round(task / "consumed.json", "round", "consumed_round")


def _delivery_round(task: Path) -> int:
    return _round(task / "delivered.json", "round", "delivery")


def _ready_delivery(task: Path) -> int:
    return _round(task / "ready.json", "delivery", "round")


def _verdict_delivery(task: Path) -> int:
    return _round(task / "verdict.json", "delivery")


def _verdict_passed(task: Path) -> bool:
    return str(_read_json(task / "verdict.json").get("verdict", "")).upper() == "PASS"


def _spec_round(task: Path) -> int:
    return _round(task / "spec.json", "round")


def _assessment_spec_round(task: Path) -> int:
    return _round(task / "assessment.json", "spec_round")


def _agreement_spec_round(task: Path) -> int:
    return _round(task / "agreement.json", "spec_round")


def _blocked_round(task: Path) -> int:
    return _round(task / "blocked.json", "round")


def _response_round(task: Path) -> int:
    return _round(task / "pm-response.json", "round")


def _is_closed(task: Path) -> bool:
    return (task / "closed.md").exists()


def _mtime(path: Path) -> float | None:
    """文件 mtime（秒）；不存在/不可读 → None。

    **None ≠ 0**：0 会被读成「那一刻发生了两件事」，None 才是「没有这一刻」。
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return None


class Protocol:
    """`.xteam/` 目录即状态机。**文件存在性 + round 计数**就是状态，无 status 字段。

    三套独立计数：spec（PM 改版）、delivery（dev 交付）、verdict（PM 判定）。
    交接工件记录自己针对哪一版/哪一次，「有没有更新的东西没人看」纯靠比较得出。

    阶段零 · 解阻塞（PM 优先，贯穿全程）
      blocked.json TL/DEV  →  pm-response.json PM
    阶段一 · 需求共识（PM ↔ TL，TL 不许沉默）
      spec.md+spec.json → assessment.json → agreement.json
    阶段二 · 执行
      request.md → delivered.json → ready.json → verdict.json → consumed.json → closed.md
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.dir = root / XTEAM_DIRNAME
        self.tasks = self.dir / "tasks"

    def ensure(self) -> None:
        self.tasks.mkdir(parents=True, exist_ok=True)
        queue = self.dir / "QUEUE.md"
        if not queue.exists():
            queue.write_text(
                "# 队列\n\n"
                "> 唯一待办真相。TL 追加，PM 改状态，dev 只读。\n"
                "> 状态：`todo` → `spec` → `doing` → `review` → `gate` → `done`\n\n"
                "| 序 | 切片 | 状态 | 备注 |\n|---|---|---|---|\n",
                encoding="utf-8")

    def all_tasks(self) -> list[Path]:
        """按名字排好的全部切片目录。**结果按 tasks 目录的 mtime 缓存。**

        以前每调一次就 `iterdir()` + 对每个条目 `is_dir()`。而 `index()` 里
        `debts()` → `deps_blocking()` → `all_tasks()` 是一条链，一轮巡检下来
        `all_tasks()` 被调 400 多次，200 个切片就是 8 万多次 `stat` —— 实测
        200 个切片要 2.6s，500 个切片跑不完。巡检每 60 秒一轮，切片一多就持续
        占 CPU，告警也跟着延迟。

        为什么 mtime 是可靠的键：
          · 新建 / 删除 / 重命名切片 → 父目录 mtime 变 → 缓存失效，正确重算
          · 改切片**内部**文件    → 父目录 mtime **不变**，但 all_tasks() 只
            关心「有哪些子目录、叫什么、按什么顺序」，那些没变 —— 复用是对的
        所以这里缓存的是**名字列表**，不是状态；状态仍每轮重算。
        """
        try:
            stamp = self.tasks.stat().st_mtime_ns
        except OSError:
            return []
        cache = getattr(self, "_tasks_cache", None)
        if cache is not None and cache[0] == stamp:
            return cache[1]
        if not self.tasks.is_dir():
            out: list[Path] = []
        else:
            out = sorted(p for p in self.tasks.iterdir() if p.is_dir())
        self._tasks_cache = (stamp, out)
        return out

    def open_tasks(self) -> list[Path]:
        return [t for t in self.all_tasks() if not _is_closed(t)]

    def index(self) -> dict:
        """全量状态的单文件快照。

        之前状态散在各 task 目录的 json 里，agent 要判断「现在整体怎么样」得挨个
        翻。这里一次算完写成 .xteam/index.json：PM/人类/巡检都读它，不用拼。
        纯派生（不新增事实），任何时候都能从 task 目录重算，因此不会与实际脱节。
        """
        tasks = []
        for t in self.all_tasks():
            debts = self.debts(t)
            tasks.append({
                "slug": t.name,
                "closed": _is_closed(t),
                "stage": self._stage(t),
                "owes": debts,                       # {role: obligation}
                "spec_round": _spec_round(t),
                "delivery": _delivery_round(t),
                "verdict": _read_json(t / "verdict.json").get("verdict", ""),
                "verdict_round": _verdict_round(t),
                "blocked_round": _blocked_round(t),
                "responded": _response_round(t),
            })
        return {
            "generated": now(),
            "counts": {
                "total": len(tasks),
                "open": sum(1 for t in tasks if not t["closed"]),
                "closed": sum(1 for t in tasks if t["closed"]),
            },
            "owes": self.overall_debts(),
            "queue": self.queue_items(),
            "tasks": tasks,
        }

    def phase_times(self) -> list[dict]:
        """每片的分段耗时（分钟）。纯派生，不新增事实。

        用文件 mtime 当时刻 —— 协议里没有写时间戳的字段，所以这是近似：
        - 某一端的文件不存在 → 那一段是 **None，不是 0**（0 会被读成「没花时间」）
        - mtime 回退（负时长）**原样保留** —— 夹成 0 会让并行度骗人
        """
        files = ("spec.md", "request.md", "delivered.json",
                 "ready.json", "verdict.json", "closed.md")
        rows = []
        for t in self.all_tasks():
            stamps = {name: _mtime(t / name) for name in files}
            minutes: dict[str, float | None] = {}
            for phase, start, end in PHASES:
                a, b = stamps[start], stamps[end]
                minutes[phase] = (round((b - a) / 60.0, 1)
                                  if a is not None and b is not None else None)
            spec, closed = stamps["spec.md"], stamps["closed.md"]
            rows.append({
                "slice": t.name,
                "closed": _is_closed(t),
                "verdict": (str(_read_json(t / "verdict.json").get("verdict", ""))
                            .upper() or None),
                "stamps": stamps,
                "minutes": minutes,
                "total": (round((closed - spec) / 60.0, 1)
                          if spec is not None and closed is not None else None),
            })
        return rows

    def throughput(self, rows: list[dict] | None = None) -> dict:
        """把 `phase_times()` 汇成「一行话」：中位 / 吞吐 / 并行度 / 冻结 / 重叠命中。

        `freeze_with_other_writer` 是**「重叠真的发生了」的唯一证据**：冻结窗口
        （delivered → verdict）里，另一片的 request 已落、delivered 还没落 —— 也就是
        有人在这段时间里正在写。F-16 全局冻结下它恒为 0。
        """
        rows = self.phase_times() if rows is None else rows

        def med(values: list) -> float | None:
            vals = [v for v in values if v is not None]
            return round(statistics.median(vals), 1) if vals else None

        sum_minutes = sum(r["total"] for r in rows if r["total"] is not None)
        specs = [r["stamps"]["spec.md"] for r in rows if r["stamps"]["spec.md"] is not None]
        wall = (max(specs) - min(specs)) / 60.0 if len(specs) > 1 else 0.0
        freeze, hits = 0.0, 0
        for r in rows:
            start, end = r["stamps"]["delivered.json"], r["stamps"]["verdict.json"]
            if start is None or end is None:
                continue
            freeze += (end - start) / 60.0
            for other in rows:
                if other is r:
                    continue
                req, dl = other["stamps"]["request.md"], other["stamps"]["delivered.json"]
                if req is not None and dl is not None and req < start < dl < end:
                    hits += 1
                    break
        return {
            "slices": len(rows),
            "closed": sum(1 for r in rows if r["closed"]),
            "sum_minutes": round(sum_minutes, 1),
            "wall_minutes": round(wall, 1),
            "parallelism": (round(sum_minutes / wall, 2) if wall > 0 else None),
            "median": {**{p: med([r["minutes"][p] for r in rows]) for p, _, _ in PHASES},
                       "total": med([r["total"] for r in rows])},
            "freeze_minutes": round(freeze, 1),
            "freeze_with_other_writer": hits,
        }

    def overall_debts(self) -> dict[str, list[str]]:
        """role -> ["<task>:<obligation>", ...]，跨全部 task 汇总。

        **队列有未开工项却没人欠账时，PM 欠 `start-next`。**
        没有这一条会出现一个既没人在干活、也没人负责的真空：项目里还没有切片
        目录（或全部闭合），`debts()` 什么都不产出，于是 `idle + owes:none` 被巡检
        判成「合法终态」—— 而人类看到的是「三个 pane 全闲着，队列里明明有活」。
        谁都不欠账，于是谁都不动，**人成了唯一的推进力**。
        """
        out: dict[str, list[str]] = {}
        ns_label = self._next_slice_label()
        for t in self._tasks_by_queue_order():
            for role, ob in self.debts(t).items():
                # next-slice 的标签指向「下一个未开工项」，不是产出它的那片
                # 已闭合切片——闭合片名出现在催办里就是误导（F-20 证据 B）。
                name = ns_label if ob == "next-slice" and ns_label else t.name
                out.setdefault(role, []).append(f"{name}:{ob}")
        if (not out.get("pm") and self._unstarted_items()
                and not self._unstarted_gate_conflict()):
            # 待判窗口里，只有「下一片与待判片触碰同一批文件」才不催 —— 那才是
            # 「开新片 = 往判中的树上写东西」（F-16）。触碰不相交时照催：队列
            # 先铺好下一片，dev 交付完就能接上，不用等 PM 现写 spec。
            out["pm"] = ["(队列):start-next"]
        # **队列全闭合但 REPORT.md 缺失 → PM 欠 report。** 切片 closed 只意味
        # 「这片做完了」，而人类要的是「整个项目做完后能部署、能测试、有人接」。
        # 没有这一条，PM 写完 closed.md 就判定「无欠账合法终态」，结项报告永远没人写。
        #
        # 判据是「**没有待办**」（pending == 0），不是「队列里一行都没有」
        # （total == 0）。原来写的是 `queue_counts() == (0, 0)`，于是只在
        # 「队列从没被用过」时才成立 —— 而 Protocol.ensure() 一上来就建出
        # QUEUE.md，TL 追加切片后 total 永远 > 0。结果：**真实项目里这条义务
        # 一次都不会触发**，结项报告永远没人催。PR 的测试没抓到，是因为它用的
        # Bench 根本不写 QUEUE.md（total 恰好为 0），正好落在唯一能过的那个分支上。
        # 「空项目不该催报告」由下面 `any(_is_closed(...))` 负责，与 total 无关。
        if (not out.get("pm") and self.queue_counts()[0] == 0
                and any(_is_closed(t) for t in self.all_tasks())
                and not (self.dir / "REPORT.md").exists()):
            out["pm"] = ["(项目):report"]
        return out

    @staticmethod
    def _stage(task: Path) -> str:
        """切片当前走到哪一级 —— 用工件存在性判定，不猜。"""
        if _is_closed(task):
            return "closed"
        if _blocked_round(task) > _response_round(task):
            return "blocked"                    # 有人卡住，等 PM/人类
        if (task / "ready.json").exists():
            return "gate"                       # 等 PM 判 PASS/FAIL
        if (task / "delivered.json").exists():
            return "review"                     # 等 TL 工作 review
        if (task / "request.md").exists():
            return "implement"                  # dev 在写
        if (task / "agreement.json").exists():
            return "decomposing"                # TL 该拆解了
        if (task / "assessment.json").exists():
            return "settling"                   # PM 该回应评估了
        if (task / "spec.json").exists():
            return "assessing"                  # TL 该评估 spec
        if (task / "spec.md").exists():
            return "spec-drafting"              # PM 还没标版本
        return "empty"

    def write_index(self) -> Path:
        """把快照落盘并返回路径。巡检每轮调它，restore 与 status 也读它。"""
        p = self.dir / "index.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        write_json(p, self.index(), indent=2)
        return p

    def project_docs(self) -> dict:
        """探测项目自己的文档位置。**只读不改** —— 产品契约以项目为准。

        项目已有 docs/adr/specs 就在 spec 里引用它们，不另起一套说法；
        没有（简化开发）才用 tasks/*/spec.md 承载。
        """
        found: dict[str, list[str]] = {}
        for key, rel in (("adr", "docs/adr"), ("specs", "docs/specs"),
                         ("design", "docs/design"), ("rfc", "docs/rfc"),
                         ("docs", "docs")):
            d = self.root / rel
            if not d.is_dir():
                continue
            files = sorted(p.name for p in d.glob("*.md"))[:50]
            if files:
                found[key] = files
        return {
            "has_project_docs": bool(found),
            "locations": found,
            "policy": ("项目文档是产品契约，xteam 只读不改；spec 需引用既有决策"
                       if found else
                       "项目无 docs/，xteam 用 tasks/*/spec.md 承载决策"),
        }

    def project_of(self, task: Path) -> str:
        """切片属于哪个项目（委托给注册表解析 task.json / front-matter）。"""
        return Registry(self.root).project_of_task(task)

    def by_project(self) -> dict[str, list[str]]:
        """project -> [未闭合切片名]。status / restore 按项目分组时用。

        用拓扑序而不是字母序：跨项目链路上（api → frontend → admin），
        字母序会把后继排在前面，看起来像可以开工。
        """
        reg = Registry(self.root)
        out: dict[str, list[str]] = {}
        for t in self.slice_order(self.open_tasks()):
            out.setdefault(self.project_of(t) or "(未声明)", []).append(t.name)
        return out

    def debts(self, task: Path) -> dict[str, str]:
        """单个 task 目录下每个角色当前欠的义务名（无则不含该角色）。纯函数。"""
        out: dict[str, str] = {}
        closed = _is_closed(task)

        # 跨切片依赖优先于一切：**前置没闭合的切片现在还不能动**。
        # 放在最前面是因为它决定「这条切片该不该存在在这一轮」，而不是
        # 「这一步该谁做」。等前置是合法状态，不是有人偷懒 —— 所以归 tl 记一笔，
        # 让 PM 知道下一步该去推哪个前置，而不是以为 TL 卡住了。
        #
        # **对所有阶段生效，不只是没拆解的时候。** 之前只在「还没有 request.md」
        # 时才拦，于是 TL 一旦先拆了解释，就整个绕过去了：dev 会照着一套还不存在
        # 的 API 去实现，跨项目排序形同虚设。门禁必须是状态机的地板，不是门框。
        if not closed:
            blocking = self.deps_blocking(task)
            if blocking:
                out["tl"] = "wait-dep"
                return out

        # 共享契约项目改了契约却没有对应文件 → PM 得先把契约定下来。
        # 不拦「写代码」，只拦「宣称改了什么却没留下约定」——后者正是跨项目
        # 最常见的扯皮来源：前端照着记忆里的接口实现。
        if not closed:
            missing = self.contracts_missing(task)
            if missing:
                out["pm"] = "contracts-missing"
                return out

        # 阶段零：解阻塞优先级最高 —— 一个没人拍板的问题挂着，下游整条链都是死的。
        if not closed and _blocked_round(task) > _response_round(task):
            out["pm"] = "unblock"

        # 阶段一：需求共识。只在还没产出 request.md 时进入；拆解完成后不回退，
        # 否则两个角色来回震荡。
        if (task / "spec.md").exists() and not closed \
                and not (task / "request.md").exists():
            # **只在多项目时强制。** 单项目下要求声明纯属多余摩擦。
            # 多项目下它是硬要求：没它，dev 可以在 A 项目里改代码说是做 B 的活，
            # review 时无从判断越界。
            reg = Registry(self.root)
            if len(reg.names()) > 1 and not self.project_of(task):
                out["pm"] = "name-project"
                return out
            s = _spec_round(task)
            if s == 0:
                out["pm"] = "version-spec"
            elif _assessment_spec_round(task) < s:
                out["tl"] = "assess"          # 硬规则：每版 spec 都必须回一句
            elif _agreement_spec_round(task) < s:
                out["pm"] = "settle"          # TL 已回，PM 必须回应
            else:
                out["tl"] = "decompose"

        # 阶段二：执行
        if (task / "request.md").exists() and not closed:
            delivered = (task / "delivered.json").exists()
            fail_pending = (
                (task / "verdict.json").exists()
                and not _verdict_passed(task)
                and _verdict_delivery(task) >= _delivery_round(task)
            )
            # **写树闸门：只拦「跟已经在飞的片触碰同一批文件」。**
            # F-16 的原判据是「树必须可归因」，所以冻住整棵树；钉了交付快照
            # （`xteam snap` → base/head）之后，「可归因」由提交承担，树可以继续
            # 往前跑。没写 touches.json = 未知 = 冲突（保守方向，退回原来的串行）。
            # 待判片自己到不了这——delivered 且无对准它的 FAIL 才算 pending。
            if (not delivered or fail_pending) \
                    and not self.implement_blockers(task) \
                    and not (PREEMPT_ON_FAIL and self._rework_slices(exclude=task)):
                out["dev"] = "implement"
            elif ((task / "verdict.json").exists()
                    and _verdict_round(task) != _consumed_round(task)):
                # 用 != 而非 >：verdict.round < consumed.round 是丢轮次的回退事故，
                # 不能读成「已完成」——那会让该切片永久卡死再无人催。
                out["dev"] = "consume"

        if (task / "delivered.json").exists() and not closed:
            if (not (task / "ready.json").exists()
                    or _ready_delivery(task) < _delivery_round(task)):
                out["tl"] = "chase"

        if (task / "ready.json").exists() and not closed:
            if (not (task / "verdict.json").exists()
                    or _verdict_delivery(task) < _ready_delivery(task)):
                out["pm"] = "gate"

        if closed:
            out["pm"] = "next-slice"
        # **队列里没有未开工项时 next-slice 让位。** 单个切片 closed 只意味「这片做完了」；
        # 队列有未开工项时，催 PM 去取下一项是对的（别停）。队列空、或队列里的项全已开工
        # （spec.md 已出、下游在跑）时再催就是空转/误催——前者由 overall_debts 的
        # 「队列空 + 有闭合切片 + 无 REPORT」接去催 report，后者是 PM 的合法等待。
        if closed and not self._unstarted_items():
            out.pop("pm", None)
        return out

    # -- 跨切片依赖 --

    def dep_map(self) -> dict[str, list[str]]:
        """切片名 → 它 depends_on 的切片名。只含真的声明了依赖的。"""
        reg = Registry(self.root)
        out: dict[str, list[str]] = {}
        for t in self.all_tasks():
            deps = reg.depends_on_task(t)
            if deps:
                out[t.name] = deps
        return out

    def _registry(self) -> "Registry":
        """本轮复用的注册表实例。缓存挂在实例上，所以必须复用。"""
        reg = getattr(self, "_reg", None)
        if reg is None:
            reg = Registry(self.root)
            self._reg = reg
        return reg

    def deps_blocking(self, task: Path) -> list[str]:
        """这个切片在等哪些前置。返回**未闭合**的前置名（已闭合的不算）。

        三种状态要分清，混在一起会把「写错了」当成「还没做完」：
          · 前置已闭合          → 不阻塞
          · 前置存在但未闭合    → 阻塞（正常排队）
          · **前置根本不存在**  → 阻塞，且是配置错误（见 missing_deps）
        """
        # 复用同一个 Registry 实例：mtime 缓存只在**实例内**生效，每个切片
        # 新建一个等于没缓存。
        reg = self._registry()
        by_name = {t.name: t for t in self.all_tasks()}
        out = []
        for name in reg.depends_on_task(task):
            dep = by_name.get(name)
            if dep is None or not _is_closed(dep):
                out.append(name)
        return out

    def ready_queue(self) -> list[dict]:
        """队列里**现在就能派**的项，按原顺序。

        和 `queue_items()` 的区别：这一份剔除了还在等 `depends_on` 前置的切片。
        「队首就是推荐项」这条规则必须建立在「推荐项真的能做」之上 ——
        否则 PM 会先抓一个还在等前置的切片，整条链看上去像卡住了，
        而实际上只是顺序不对。
        """
        ready: list[dict] = []
        for item in self.queue_items():
            task = self.tasks / item["slice"]
            if task.exists() and self.deps_blocking(task):
                continue
            ready.append(item)
        if ready and not any(i["recommended"] for i in ready):
            ready[0]["recommended"] = True
        return ready

    def missing_deps(self, task: Path) -> list[str]:
        """depends_on 里指向**不存在**的切片 —— 拼错名字了，不是排队。"""
        reg = Registry(self.root)
        names = {t.name for t in self.all_tasks()}
        return [d for d in reg.depends_on_task(task) if d not in names]

    def slice_order(self, tasks: list[Path] | None = None) -> list[Path]:
        """按依赖拓扑排序：前置一定排在后继前面。

        单级依赖（frontend 等 api）靠 `deps_blocking()` 就够判断能不能派，但**显示**
        需要更多层：A→B→C 三级依赖时，字母序会把 C 排在最前，人看着以为 C 可以
        开工。排序解决的是「看起来」和「实际上」不一致。

        深度优先、依赖优先输出；**有环时把环上的切片放到最后**（`dep_cycles()`
        会另行报警），不让排序本身抛错 —— 一个显示问题不该让 status 崩掉。
        """
        reg = Registry(self.root)
        pool = list(tasks if tasks is not None else self.all_tasks())
        by_name = {t.name: t for t in pool}
        deps = {t.name: [d for d in reg.depends_on_task(t) if d in by_name]
                for t in pool}
        out: list[Path] = []
        done: set[str] = set()
        active: set[str] = set()

        def visit(name: str) -> None:
            if name in done or name in active:
                return                       # active = 在环上，交给 dep_cycles 去报
            active.add(name)
            for d in deps.get(name, []):
                visit(d)
            active.discard(name)
            done.add(name)
            out.append(by_name[name])

        for name in sorted(deps):
            visit(name)
        # 环上的切片从没被 visit 完成，补到末尾并保持稳定顺序
        for t in pool:
            if t not in out:
                out.append(t)
        return out

    def dep_cycles(self) -> list[list[str]]:
        """依赖环。a 等 b、b 等 a 时不报环就会永远排不上队，还查不出原因。"""
        graph = self.dep_map()

        state: dict[str, int] = {}          # 0=在栈上 1=已完成
        cycles: list[list[str]] = []

        def walk(node: str, stack: list[str]) -> None:
            if state.get(node) == 1:
                return
            if state.get(node) == 0:
                if node in stack:
                    cycles.append(stack[stack.index(node):] + [node])
                return
            state[node] = 0
            for nxt in graph.get(node, []):
                walk(nxt, stack + [node])
            state[node] = 1

        for name in graph:
            walk(name, [])
        return cycles

    def contracts_missing(self, task: Path) -> list[str]:
        """切片声明要改的契约里，`.xteam/contracts/` 下还没有对应文件的。

        **只在共享契约项目上要求。** 普通项目没有跨项目契约这回事，
        硬要求只会变成填表作业。
        """
        reg = Registry(self.root)
        names = reg.contracts_of_task(task)
        if not names:
            return []
        if not reg.is_shared(self.project_of(task)):
            return []
        out = []
        for c in names:
            path = contract_path(self.root, c)
            # 越界/不合规的名字一律算「缺失」：宁可要求重新写一个合法契约，
            # 也不能让 task.json 里的 `../../README.md` 冒充成「契约已存在」。
            if path is None or not path.exists():
                out.append(c)
        return out

    def dispatchable(self) -> list[Path]:
        """现在就能派出去的切片：未闭合，且依赖已满足。

        队列的「推荐项」应该从这里取 —— 否则 PM/TL 会先抓到一个还在等前置的
        切片，整条链看起来像卡住了。
        """
        return [t for t in self.open_tasks() if not self.deps_blocking(t)]

    def obligations(self) -> dict[str, list[tuple[str, str]]]:
        """role -> [(task_slug, obligation_name), ...]。

        任务按 QUEUE 序遍历（`_tasks_by_queue_order`）——同一角色多片可做时
        `pending[0]` 取序号最小那片，不再被任务目录的字母序牵着走（F-20）；
        `next-slice` 条目的 slug 指向队列第一个未开工项（`_next_slice_label`），
        已闭合片名不进标签。"""
        found: dict[str, list[tuple[str, str]]] = {r: [] for r in DEFAULT_ROLES}
        ns_label = self._next_slice_label()
        for task in self._tasks_by_queue_order():
            for role, obligation in self.debts(task).items():
                slug = ns_label if obligation == "next-slice" and ns_label \
                    else task.name
                found[role].append((slug, obligation))
        return found

    # -- 队列 --

    def queue_items(self) -> list[dict]:
        """解析 QUEUE.md。队首（未完成项里序号最小）是**推荐项** ——
        角色遇到「下一件做什么」时默认取它，不该再问一遍人类。"""
        path = self.dir / "QUEUE.md"
        if not path.exists():
            return []
        items: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("|") or "---" in line:
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 3 or not cells[0].isdigit():
                continue
            note = cells[3] if len(cells) > 3 else ""
            items.append({"order": int(cells[0]), "slice": cells[1].strip("`* "),
                          "state": cells[2].strip("`* "), "note": note,
                          "recommended": "推荐" in note})
        pending = [i for i in items if i["state"] not in ("done", "已闭合")]
        if pending and not any(i["recommended"] for i in pending):
            pending[0]["recommended"] = True
        return items

    def queue_counts(self) -> tuple[int, int]:
        """队列计数（未完成, 总数）—— 唯一口径。

        未完成 = state 不在 ("done", "已闭合")；总数 = 解析器认得的行数。
        """
        items = self.queue_items()
        pending = [i for i in items if i["state"] not in ("done", "已闭合")]
        return len(pending), len(items)

    def _unstarted_items(self) -> list[dict]:
        """队列里未开工的项：state 未完成 且 `tasks/<slug>/spec.md` 不存在。

        spec.md 是 PM 的第一件产物——它存在 = 片子已被开工（spec 共识/拆解/实现
        交给下游在跑），此时 PM「不欠取下一项」是合法等待；只有还存在连 spec 都
        没写的队列项（或目录整个缺失）时，催 PM 取下一项才有意义。
        """
        return [i for i in self.queue_items()
                if i["state"] not in ("done", "已闭合")
                and not (self.tasks / i["slice"] / "spec.md").exists()]

    def touches(self, task: Path) -> list[str] | None:
        """TL 写的 `touches.json`：这一片会碰哪些路径（glob，相对项目根）。

        缺失 / 坏 JSON / 不是字符串列表 / 空 → **None = 未知**。
        「没声明」和「不碰任何文件」是两回事：未知一律按「跟谁都冲突」处理
        （保守方向是退回串行，漏判的代价是把两片写进同一份文件）。
        """
        data = _read_json(task / "touches.json")
        paths = data.get("paths")
        if not isinstance(paths, list):
            return None
        out = [str(p).strip().lstrip("./")
               for p in paths if isinstance(p, str) and p.strip()]
        return out or None

    def conflict_with(self, task: Path, others: list[str],
                      unknown_conflicts: bool = True) -> list[str]:
        """`task` 和其它切片是否触碰同一批文件。返回冲突的片名（空 = 可并行）。

        判据（双向，任一条命中即冲突）：
        - glob 双向匹配：`bin/*.py` 与 `bin/xteam_lib.py` 算相交（任一侧当模式）
        - **目录前缀**：`bin` 与 `bin/xteam` 算相交（声明一个目录 = 碰它下面所有文件）
        - 任一侧未知（没写 touches.json）→ `unknown_conflicts` 决定算不算冲突

        默认（True）把未知当冲突：多判一个只是退回串行，漏判一个就是把两片写进
        同一份文件。`unknown_conflicts=False` 只在「双方都明确声明过」时才算冲突
        —— 给旧片留一条不互相冻死的路（见 `implement_blockers`）。
        """
        mine = self.touches(task)
        if not mine:
            if not unknown_conflicts:
                return []
            return [n for n in others if n != task.name]

        def hit(a: str, b: str) -> bool:
            return (fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a)
                    or a.startswith(b + "/") or b.startswith(a + "/"))

        clash: list[str] = []
        for name in others:
            if name == task.name:
                continue
            theirs = self.touches(self.tasks / name)
            if not theirs:
                if unknown_conflicts:
                    clash.append(name)
                continue
            if any(hit(a, b) for a in mine for b in theirs):
                clash.append(name)
        return clash

    def implement_blockers(self, task: Path) -> list[str]:
        """拦着 `task` 开工（写产品文件）的片名。空 = 可以开工。

        两条规则，都往保守偏，但**保证总有人能动**（互相冻死比串行糟得多）：

        1. 与**待判片**触碰冲突 → 拦（F-16：gate 要判在一棵可归因的树上；判完就放）。
           未知一律算冲突 —— 旧片没有 `touches.json`，行为退回「交付即冻结」。
        2. 与**明确声明了同一批文件**的在飞片 → 只放队列序最前的那一片，其余等它
           （一次一个写者）。两边有一边没声明时不算冲突：那是升级前的常态，
           而且归因已经由交付快照（`head`）承担，不会再变成「分不清哪片是哪片」。
        """
        blocked = self.conflict_with(task, self.pending_gate_tasks())
        order = {t.name: i for i, t in enumerate(self._tasks_by_queue_order())}
        mine = order.get(task.name, -1)
        for name in self.conflict_with(task, self.inflight_slices(exclude=task),
                                       unknown_conflicts=False):
            if name not in blocked and order.get(name, -1) < mine:
                blocked.append(name)
        return blocked

    def inflight_slices(self, exclude: Path | None = None) -> list[str]:
        """「已经开工、还没闭合」的切片名，按队列序。

        开工判据 = `request.md` 在：TL 拆完才算真占了地方（只写了 spec 的还在
        PM 手上转，没人往产品文件里写东西）。
        """
        out: list[str] = []
        for t in self._tasks_by_queue_order():
            if _is_closed(t) or (exclude is not None and t.name == exclude.name):
                continue
            if (t / "request.md").exists():
                out.append(t.name)
        return out

    def _rework_slices(self, exclude: Path | None = None) -> list[str]:
        """别的片给你记着「要返工」的判定（FAIL、还没消费）—— 先修它，再开新片。

        **只拦 FAIL**：PASS 的 `consume` 只是「读一眼、记一笔」，拿它挡新活就把
        解锁重叠全挡没了。自己那片不算（是不是自己欠 `implement` 由 fail_pending 判）。
        """
        out: list[str] = []
        for t in self.all_tasks():
            if _is_closed(t) or (exclude is not None and t.name == exclude.name):
                continue
            if not (t / "verdict.json").exists() or _verdict_passed(t):
                continue
            if _verdict_round(t) != _consumed_round(t):
                out.append(t.name)
        return out

    def _unstarted_gate_conflict(self) -> list[str]:
        """队列里第一个未开工项 与「待判片」的冲突（空 = 不拦 start-next）。

        只在**待判窗口**上判：新片开工（写 spec）本身不写产品文件，真正的写树
        闸门在 `implement`（那里对所有在飞片判冲突）。这里保守一点，只是为了不
        让 dev 在待判窗口里拿到同一批文件的活。
        """
        items = sorted(self._unstarted_items(), key=lambda i: i["order"])
        gate = self.pending_gate_tasks()
        if not items or not gate:
            return []
        return self.conflict_with(self.tasks / items[0]["slice"], gate)

    def pending_gate_tasks(self) -> list[str]:
        """「已交付未判」的切片名 —— delivered.json 在、这次交付还没被 verdict 覆盖。

        窗口从 delivered.json 落盘开到 verdict.delivery 对准这次交付
        （ready.json 有无不算数——交付即冻结，TL review 期间同样在窗口内）。
        **它不再是全局闸门**：写树闸门是 `conflict_with()`（只拦触碰冲突），
        归因交给交付快照（`xteam snap` 钉的 base/head）。这里剩两个用途：
        status 显示「谁在待判」、`_unstarted_gate_conflict()` 的保守判据。
        FAIL 已对准当前交付的不算 pending——verdict 落地窗口就关了，fail_pending
        照常走。
        """
        pending = []
        for t in self.all_tasks():
            if _is_closed(t) or not (t / "delivered.json").exists():
                continue
            if (not (t / "verdict.json").exists()
                    or _verdict_delivery(t) < _delivery_round(t)):
                pending.append(t.name)
        return pending

    def undispatched_tasks(self, now_ts: float | None = None) -> list[str]:
        """request.md 落地却迟迟没人领的切片名 —— TL 拆完没门铃 dev 派发时
        的机器兜底（F-23）。

        判据只看文件事实：`request.md` 在 + `delivered.json` 不在 +
        `request.md` 的 mtime 早于 now - `IDLE_ALERT_SECS`（沿用巡检既有
        阈值 600s，不新增常量）。刚落地的（mtime 新）不点名——给 TL 留出
        门铃的时间窗，不误报；closed 片与已交付片天然不算「没人领」。
        """
        now_ts = time.time() if now_ts is None else now_ts
        out = []
        for t in self._tasks_by_queue_order():
            req = t / "request.md"
            if _is_closed(t) or (t / "delivered.json").exists() \
                    or not req.exists():
                continue
            try:
                if req.stat().st_mtime < now_ts - IDLE_ALERT_SECS:
                    out.append(t.name)
            except OSError:
                continue
        return out

    def recommended_task(self) -> dict | None:
        for item in self.queue_items():
            if item["recommended"]:
                return item
        return None

    def _queue_order(self) -> dict[str, int]:
        """QUEUE.md 的 切片名 → 序 映射（done 项也记序 —— 序是「谁先来」，
        不是「做没做完」）。QUEUE 缺失/解析失败 → 空映射，调用方退回现状
        （F-20/AC-5：解析坏了不许把 status 打死）。"""
        try:
            return {i["slice"]: i["order"] for i in self.queue_items()}
        except Exception:                            # noqa: BLE001
            return {}

    def _tasks_by_queue_order(self) -> list[Path]:
        """all_tasks() 按 QUEUE 序重排：有记序的按序号小在前，没记序的
        （bench/临时布景）保持目录序缀后。义务**种类**一行不动——只改同一
        角色多片可做时谁排第一（status/巡检取 pending[0]）。"""
        order = self._queue_order()
        return sorted(
            self.all_tasks(),
            key=lambda t: (t.name not in order,
                           order.get(t.name, 0), t.name))

    def _next_slice_label(self) -> str:
        """`next-slice` 的标签：队列里**序号最小**的未开工项名
        （`_unstarted_items` 的口径：state 未完成且无 spec.md）。
        没有未开工项 → ""（此时 next-slice 本就发不出——debts() 已让位）。
        已闭合切片的名字永不进标签。"""
        items = sorted(self._unstarted_items(), key=lambda i: i["order"])
        return items[0]["slice"] if items else ""


OBLIGATIONS: dict[str, str] = {
    "unblock": "有人报了阻塞（看 blocked.json：谁卡在哪、需要你定什么）—— "
              "PM 的第一职责是让事情继续下去。能自己定就定并写 pm-response.json；"
              "真需要人类拍板就 --waiting 说明在等什么。",
    "wait-dep": "这个切片声明了 depends_on，但前置切片还没闭合。"
                "**这不是卡住，是还没轮到它。** 去推前置切片，或把队列顺序调一下；"
                "别为了让它动起来就把依赖删掉——那正是跨项目返工的来源。"
                "（前置名拼错的话 `xteam status` 会单独报出来。）",
    "contracts-missing": "切片动了共享契约项目，却在 task.json 的 contracts 里声明了"
                        "还不存在的契约文件。**先把契约定下来再改代码** ——"
                        "跨项目最常见的失败是前端照着记忆里的接口实现。"
                        "用 `xteam contracts new \"POST /orders\"` 建，"
                        "写清请求/响应/错误码。",
    "name-project": "切片没声明它动哪个项目。多项目下这是硬要求——"
                    "在 .xteam/tasks/<切片>/task.json 写 {\"project\": \"<名字>\"}，"
                    "或在 spec.md 开头加一行 `project: <名字>`。"
                    "（可跑 `xteam projects` 看有哪些项目）",
    "version-spec": "spec.md 写了但没标版本号（spec.json 的 round）—— TL 无从判断"
                    "你的评估针对哪一版。补一个 spec.json。",
    "assess": "PM 给了新一版 spec，你还没评估。**必须回一句**：有异议写进 "
              "assessment.json 的 objections，没异议也写 verdict=agree。"
              "沉默会让 PM 无法判断该不该让 dev 开工。",
    "settle": "TL 已回复 spec 评估，你还没回应。有异议就接着讨论（改 spec 并把 "
              "spec.json 的 round +1 重发，或写明为什么不改），无异议就写 "
              "agreement.json 确认并引导 TL 拆解。",
    "decompose": "spec 已与 PM 达成一致，现在可以拆解了 —— 去写 request.md。",
    "implement": "request.md 已下但还没收到你的 delivered.json —— 开工，做完上报。",
    "chase": "dev 已交付但你还没做工作 review —— 去看 diff，该打回就打回。",
    "gate": "TL 已喊 ready，本切片等你出 verdict.json（PASS/FAIL）—— PM 门禁。",
    "consume": "verdict 已出但你还没取 —— 读 verdict.json，FAIL 就修，"
               "PASS 就问 TL 要下一项。",
    "start-next": "**队列里还有没做的项，而你现在不欠任何活 —— 这不是「做完了」，"
                  "是停住了。** 立刻打开 QUEUE.md 取下一项（或按 depends_on 挑一个"
                  "前置已就绪的），写 spec、派给 TL。不要问人类要不要开始 ——"
                  "「明显该做下一件」正是 PM 该自己判断的事，为它停下来等于把链条"
                  "的推进责任推给了人。**只有队列真的空了才问人类**，而且要一次问清"
                  "接下来几件，别一件一件地问。",
    "next-slice": "本切片已闭合 —— 别停。**先自己看 QUEUE.md / backlog 里有没有"
                  "下一项**，有就直接开工（写 spec → 派 TL）；遇选择题默认选推荐项"
                  "继续。只有那里也空了，才**一次性**问人类要接下来几件 ——"
                  "别问「可以开始下一件吗」，也别一件一件地问。",
    "report": "全部切片已闭合但 `.xteam/REPORT.md` 还没有 —— 写结项报告，写完才算"
              "真正交付。按 `templates/REPORT-TEMPLATE.md` 的节写（目标 / 进度与完成情况 / "
              "部署启动测试 / 默认账号信息 / 边界与疑问点），每节都要有可验证的内容，"
              "不确定的标「未确认」并写确认方法，不要编。",
}


def load_roles(root: Path) -> dict[str, RoleSpec]:
    """读默认角色 + 项目级覆盖（`.xteam/team.json`）。配置不存在用默认值。"""
    specs = {r: RoleSpec(r, k, cn) for r, (k, cn) in DEFAULT_ROLES.items()}
    path = root / XTEAM_DIRNAME / "team.json"
    if not path.exists():
        return specs
    # team.json 写坏必须报错，不能静默降级——配置写错却按默认跑，比报错难查得多。
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("顶层不是对象")
    except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f".xteam/team.json 解析失败：{exc}") from exc
    roles = data.get("roles", {})
    if not isinstance(roles, dict):
        raise RuntimeError(f".xteam/team.json 的 roles 必须是对象，"
                           f"实际是 {type(roles).__name__}")
    for role, override in roles.items():
        if role not in specs:
            raise RuntimeError(f".xteam/team.json 里有未知角色 {role!r}，"
                               f"可选：{', '.join(sorted(specs))}")
        if not isinstance(override, dict):
            # null / 字符串 / 数组都会让下面 .get() 抛 AttributeError，
            # 变成看不懂的 traceback 而不是可操作���配置错误
            raise RuntimeError(
                f".xteam/team.json 里 roles.{role} 必须是对象，"
                f"实际是 {type(override).__name__}")
        base = specs[role]
        specs[role] = RoleSpec(role, normalize_kind(override.get("kind", base.kind)),
                               base.cn, override.get("model", ""))
    return specs


def agent_catalog(installed: set[str] | None = None,
                  verified: set[str] | None = None) -> str:
    """给人看的 agent 能力清单，**明确区分实测与推断**。"""
    # 默认就用合并后的实测集（含本机 probe 结果），与 tui_model_ok 同源 ——
    # 展示说支持、判定说不支持，是最让人白折腾的一种不一致。
    proven = verified if verified is not None else load_verified_agents()
    lines = ["可编排的 agent（✓=本机实测过 TUI --model；?=依 kander 定义推断，未验证）：", ""]
    for kind in sorted(HERDR_KINDS):
        if kind in proven:
            mark = "✓ --model（实测）"
        elif kind in ASSUMED_TUI_MODEL:
            mark = "? --model（推断）"
        else:
            mark = "✗ 无 --model（实测 TUI 不认）"
        bits = [mark]
        if installed is not None:
            bits.append("已安装" if kind in installed else "未安装")
        if kind in DEFAULT_MODEL_HINT:
            bits.append(f"推荐 {DEFAULT_MODEL_HINT[kind]}")
        lines.append(f"  {kind:<12} " + "  ".join(bits))
    lines += [
        "",
        "不在 herdr kind 列表、无法编排：mcode、qoderclicn（各自独立的程序）",
        "",
        "验证某个 agent 认不认 --model：xteam doctor --probe <kind> --model <模型名>",
        "查可用模型：devin models / opencode models / omp models",
        "",
        "身份注入（/new、/clear 之后身份不丢）："
        + "、".join(sorted(SYSTEM_PROMPT_ARG))
        + " —— 起进程时带 " + "、".join(sorted(set(SYSTEM_PROMPT_ARG.values())))
        + "，实测过的才在表里。",
        "  别的 kind 想加进来必须自己实测一遍：起一个 pane 投这个 flag，"
        "/new 之后再问它「你是谁」，答得出才算。",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- context 感知

# pane 状态栏里 context 占用的四种格式（实测）：
#   opencode:  "392.8K (37%)"
#   devin:     "168k / 262k tokens (64%)"
#   omp:       "…> $1.18 >----------------------------51%---------------|-------1M-"
#              （进度条：百分号夹在横杠里，右边跟 `|` + 该模型的窗口上限；
#                实测 2026-10-07 omp 18.4.9。小数会出现：新会话显示 "-0.7%"）
#   pi:        "… $0.025 (sub) 2.6%/500k (auto)"      （占用 / 窗口上限）
# herdr 的 agent list **不暴露 token 字段**，只能从 pane 可见文本解析；
# 而 pane read 对 working 中的 agent 会拒绝——所以只在 idle 时采样，正好符合
# 「一个切片做完了才想 recap」的使用场景。
#
# omp / pi 这两条是补的：前两条（opencode/devin）都要求**带括号**，而它们不带，
# 于是 `_maybe_request_recap` 的 context 分支在默认 agent 上从来不触发 ——
# 一个只在别的 kind 上工作的阈值等于没有。
CONTEXT_PCT_PATTERNS = (
    re.compile(r"([0-9.]+[KMG]?)\s*\(\s*([0-9]+)%\s*\)"),
    re.compile(r"([0-9.]+[kKmM]?)\s*/\s*([0-9.]+[kKmM]?)\s*tokens\s*\(\s*([0-9]+)%\s*\)"),
    re.compile(r"([0-9]+(?:\.[0-9]+)?)%[-─—]*\|[-─—\s]*[0-9.]+[KMG]?"),
    re.compile(r"([0-9]+(?:\.[0-9]+)?)%/[0-9.]+[kKmM]?"),
)


def parse_context_pct(pane_text: str) -> int | None:
    """从 pane 文本里解析 context 占用百分比。解析不出返回 None（不猜）。"""
    for pat in CONTEXT_PCT_PATTERNS:
        m = pat.search(pane_text)
        if m:
            try:
                # float 先过一道：omp 的进度条会显示小数（"-0.7%"），
                # int("0.7") 会抛 ValueError，把「解析不了」变成崩。
                return int(float(m.group(m.lastindex)))
            except ValueError:
                continue
    return None


def recap_path(root: Path, role: str) -> Path:
    return root / XTEAM_DIRNAME / "memory" / f"{role}-recap.md"


# recap 的**读取**上限。实测有人写过 20 万字的 recap，交接 prompt 也跟着变成
# 20 万字 —— up/swap 时白烧 token，甚至直接撑爆上下文。别人的 recap 注入时有
# 限长，自己的却没有，这是漏的一环。
#
# 为什么只在**读取**端截：recap 是 agent 用自己的工具写的，xteam 拦不到写入；
# 请求文案里已经要求 15 行以内，但那是请求不是保证。读取端截断是唯一可靠的位置。
# 保留尾部：recap 的结论在最后（前面是过程），交接时新 agent 看的也是尾部。
RECAP_STORE_CHARS = 8000


def latest_recap(root: Path, role: str) -> str:
    """读该角色最近一次 recap 的正文；没有就返回空串。

    **读取时也限长** —— 文件可能是本次截断规则之前写的，也可能被手工改过，
    不能假设磁盘上的东西一定是短的。
    """
    p = recap_path(root, role)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")[-RECAP_STORE_CHARS:]
    except OSError:
        return ""


# ---------------------------------------------------------------- 模型查询


_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@+~-]{0,79}$")


def plausible_model(name: str) -> bool:
    """这个字符串长得像模型名吗？

    **不是洁癖，是防误导。** 实测 `claude models` 根本没有 models 子命令 ——
    它忽略参数直接进交互会话，stdout 里是一段欢迎语。解析器会把其中某一行
    当成模型名，于是 probe 报「启动超时」，而真实原因是模型名是一句话。
    那个报错会把人引向完全错误的方向（去查 agent 认不认这个模型）。

    真实模型名都是单个 token：`swe-2-max`、`anthropic/claude-opus-4-5`、
    `deepinfra/ByteDance/Seed-2.0-code`。含空格、过长、或含中英文标点的都不是。
    """
    return bool(_MODEL_NAME_RE.match(name.strip()))


def list_models(kind: str) -> list[dict]:
    """问 agent 自己要模型清单。返回 [{model, provider, blocked}]。

    每家的 `models` 输出格式都不同（实测）：
      · opencode `opencode models`  —— 纯列表，一行一个 `provider/model`
      · omp      `omp models`       —— 带 provider 分组的表格
      · devin    `devin models list` —— 按 family 分组、每行一个缩进的名字
    统一成同一形状，上层就不用管差异。

    `blocked` 标出走 openrouter 的那些 —— 名字里往往看不出前缀（`muse`、
    `deepseek-v4.1` 都有 openrouter 版和已付费版），所以必须在查询时就标出来，
    不能指望调用方自己认前缀。
    """
    # devin 的 models 需要子命令 `models list`（实测：不带子命令它打印用法但
    # **退出码仍是 0**，所以只查 rc 抓不住这个坑）
    argv = [kind, "models", "list"] if kind == "devin" else [kind, "models"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=45)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0 or "command not found" in (proc.stderr or ""):
        return []
    text = proc.stdout or ""
    # 这几个 CLI 在「没登录 / 没匹配」时**把错误写在 stdout 且退出码仍是 0**，
    # 所以必须看内容，否则会把一句报错当成「该 agent 有 0 个模型」——
    # 真实原因是环境没配好，提示会指向错误方向。
    first = next((l.strip() for l in text.splitlines() if l.strip()), "")
    if re.match(r"^(Error|Warning)\b", first):
        return []
    out: list[dict] = []

    if kind == "omp":
        provider = ""
        for line in text.splitlines():
            head = re.match(r"^([a-z0-9_.-]+) \((\d+)\)\s*$", line.strip())
            if head:
                provider = head.group(1)
                continue
            if not provider or "│" not in line:
                continue
            cells = [c.strip() for c in line.strip().strip("│").split("│")]
            if len(cells) >= 2 and re.match(r"^[~a-z0-9]", cells[0]) \
                    and cells[0] not in ("model", "models"):
                name = cells[0].lstrip("~")
                out.append({"model": name, "provider": provider,
                            "blocked": provider in BLOCKED_PROVIDERS})
        return out

    if kind == "devin":
        for line in text.splitlines():
            # 行形如 `  swe-2-max      SWE-2 Max  [262K context, Free]`
            # 要排除 family 标题行（无缩进）、aliases 行、空行。
            m = re.match(r"^\s+([a-z0-9][\w.-]*)\s{2,}\S", line, re.I)
            if m and not m.group(1).lower().startswith("alias"):
                out.append({"model": m.group(1), "provider": "devin",
                            "blocked": False})
        return out

    # opencode 及其它：一行一个 model
    for line in text.splitlines():
        name = line.strip()
        if "/" in name and not name.startswith(("/", ".")):
            provider = name.split("/", 1)[0].lower()
            out.append({"model": name, "provider": provider,
                        "blocked": provider in BLOCKED_PROVIDERS})
    return out


def search_models(kind: str, query: str, include_blocked: bool = True,
                  limit: int = 40) -> list[dict]:
    """搜模型。

    默认**返回全部**（含 openrouter），只在结果里标记 —— 用户要自己挑，
    替他藏起来反而不好选。`--only-paid` 才只看已付费的。
    openrouter 只是「多花钱」的提示，不是禁止。
    """
    q = query.strip().lower()
    hits = [m for m in list_models(kind)
            if (include_blocked or not m["blocked"])
            and (not q or q in m["model"].lower())]
    return hits[:limit]


# ---------------------------------------------------------------- 项目注册表
#
# 一个根目录下治理多个项目（典型：frontend / api / admin）。要点：
#
#   1. **api 之类的共享项目不是普通项目，是契约层。** 它被多个消费方依赖，
#      动它就要知道「谁受影响」——否则前端默默坏了，没人被提醒。
#   2. **项目构成会变。** 有时后端和 API 在同一个仓里（`backend[api]`），
#      所以注册表要能表达「谁消费谁」，不能写死项目名。
#   3. **每个切片必须声明它动哪个项目。** 否则 dev 可以在 api 里改代码、
#      说是给前端的活，review 时无从判断越界。
#
# 契约项目的 consumers 声明下游，改它时 PM 会被告知影响面。
PROJECT_KINDS = ("app", "shared-api")

# **这个仓库本身**是什么类型。init 时问清楚，避免以后把三种结构搞混：
#   single      单个应用仓库，xteam 就管它一个（最简单）
#   multi-app   多个彼此独立的应用（前台 / 后台 / 脚本…），没有共享契约
#   multi-api   多项目，且其中有**共享契约层**（api 同时服务多个消费方）
#                —— 动它要知道下游，这是与 multi-app 的关键差别
REPO_KINDS = ("single", "multi-app", "multi-api")


class Registry:
    """`.xteam/projects.json` —— 一次 xteam 治理哪些项目、谁依赖谁。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / XTEAM_DIRNAME / "projects.json"

    def load(self) -> dict:
        """读注册表。文件不存在时**自动发现**子目录里的 git 仓库（兜底，不是推荐路径）。

        **按 mtime 缓存。** `deps_blocking()` 每个切片都新建一个 Registry 并
        `load()` 一次，于是 index() 里注册表被读几百遍；更贵的是自动发现那条路
        —— 每个子目录还要 `git rev-parse` 一次。
        """
        try:
            stamp = self.path.stat().st_mtime_ns
        except OSError:
            stamp = None
        if stamp is None:
            # 文件不存在（走自动发现）。目录结构不变时结果也不变，用目录 mtime 当键。
            try:
                stamp = ("dir", self.root.stat().st_mtime_ns)
            except OSError:
                return self._autodiscover()
        cache = getattr(self, "_cache", None)
        if cache is not None and cache[0] == stamp:
            return cache[1]
        data = self._load_uncached()
        self._cache = (stamp, data)
        return data

    def _load_uncached(self) -> dict:
        """真正读盘。不缓存，测试与「刚改完立刻重算」的场景用。"""
        if self.path.exists():
            data = _read_json(self.path)
            if isinstance(data.get("projects"), list):
                return data
        return self._autodiscover()

    def repo_kind(self) -> str:
        """这个仓库是什么结构。声明过就用声明的，否则按项目数推断。"""
        declared = self.load().get("kind")
        if declared in REPO_KINDS:
            return declared
        n = len(self.names())
        if n <= 1:
            return "single"
        return "multi-api" if any(p.get("kind") == "shared-api"
                                   for p in self.load()["projects"]) else "multi-app"

    def is_multi(self) -> bool:
        return self.repo_kind() != "single"

    def describe_structure(self) -> str:
        """一句话讲清这个仓库的结构 —— init / status / restore 都会用。"""
        kind = self.repo_kind()
        names = self.names()
        if kind == "single":
            return "单应用仓库，xteam 只管这一个"
        shared = [p["name"] for p in self.load()["projects"]
                  if p.get("kind") == "shared-api"]
        head = f"多项目（{kind}）：{'、'.join(names) or '未声明'}"
        if shared:
            head += f"；共享契约层：{'、'.join(shared)}"
        return head

    def _autodiscover(self) -> dict:
        """没有注册表就按「子目录里有 .git」推断，并把结果落盘供人修订。

        猜出来的 type 一律是 `app`（最保守）；标成 `shared-api` 需要人明确写，
        因为那会让 xteam 主动提示下游影响，猜错会变成噪音。
        """
        found = []
        try:
            for child in sorted(self.root.iterdir()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                if (child / ".git").exists() or (child / ".hg").exists():
                    found.append({"name": child.name, "kind": "app",
                                  "consumers": []})
        except OSError:
            pass
        return {"projects": found, "_auto": True,
                "_note": "自动发现，未经确认；需要共享契约请手工标 kind=shared-api"}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.path, data, indent=2)

    def names(self) -> list[str]:
        return [p["name"] for p in self.load().get("projects", [])]

    def find(self, name: str) -> dict | None:
        """取某个项目的注册信息。命名避开 dict.get 以免读代码时误解。"""
        for p in self.load().get("projects", []):
            if p.get("name") == name:
                return p
        return None

    def consumers_of(self, name: str) -> list[str]:
        """谁依赖这个项目。跨项目切片完成后据此提醒 PM 通知下游。"""
        return list((self.find(name) or {}).get("consumers", []) or [])

    def is_shared(self, name: str) -> bool:
        return (self.find(name) or {}).get("kind") == "shared-api"

    def resolve_dir(self, name: str) -> Path | None:
        """项目在磁盘上的位置。只允许根目录的直接子目录（防越界）。

        **单应用项目的位置就是根目录自己。** `init --repo-kind single` 记的项目名
        是根目录名，而根目录不是自己的子目录 —— 于是 `projects` 会把一个刚 init
        完的正常项目显示成「✗ 目录不存在」，白费用户对状态命令的信任（实测确认）。
        所以：先按子目录找，找不到再看它是不是根目录自己的名字。
        """
        if not name or "/" in name or name == "..":
            return None
        if name == ".":
            return self.root          # 单应用显式指向根目录
        candidate = (self.root / name).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError:
            return None
        if candidate.is_dir():
            return candidate
        # 单应用：项目名就是根目录名，位置即根目录
        if name == self.root.name:
            return self.root
        return None

    def project_of_task(self, task: Path) -> str:
        """切片属于哪个项目：优先 task.json 的 project，其次 spec.md 的 front-matter。"""
        meta = _read_json(task / "task.json")
        if meta.get("project"):
            return str(meta["project"])
        head = ""
        try:
            with (task / "spec.md").open(encoding="utf-8") as fh:
                for _ in range(12):          # 只看开头，front-matter 不会太长
                    line = fh.readline()
                    if not line:
                        break
                    head += line
                    if head.count("---") >= 2:
                        break
        except OSError:
            pass
        m = re.search(r"^project:\s*(\S+)", head, re.M)
        return m.group(1).strip() if m else ""

    def depends_on_task(self, task: Path) -> list[str]:
        """切片声明的前置切片（`task.json` 的 depends_on）。

        跨项目需求的正确拆法是**按项目切成多个切片**再排序，而不是一个切片
        跨两个仓改代码（那样 review 时无从判断越界）：

            api-side       {"project": "api"}
            frontend-side  {"project": "frontend", "depends_on": ["api-side"]}
        """
        raw = _read_json(task / "task.json").get("depends_on")
        if not isinstance(raw, list):
            return []
        return [str(x).strip() for x in raw if str(x).strip()]

    def contracts_of_task(self, task: Path) -> list[str]:
        """切片声明它改动的契约文件（`task.json` 的 contracts）。"""
        raw = _read_json(task / "task.json").get("contracts")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        return [str(x).strip() for x in raw if str(x).strip()]

    def downstream_impact(self, projects: list[str]) -> list[str]:
        """给定项目集合，返回受影响的下游消费方（去重、去自身）。"""
        out: list[str] = []
        for name in projects:
            for consumer in self.consumers_of(name):
                if consumer not in projects and consumer not in out:
                    out.append(consumer)
        return out


# ---------------------------------------------------------------- 项目 wiki


def wiki_dir(root: Path) -> Path:
    return root / XTEAM_DIRNAME / "wiki"


def wiki_files(root: Path) -> list[Path]:
    d = wiki_dir(root)
    if not d.is_dir():
        return []
    return sorted(d.glob("*.md"))


def wiki_append(root: Path, kind: str, title: str, body: str, by: str = "") -> Path:
    """往 wiki 追加一条（决策 / 教训 / 约定）。

    追加而不是覆盖：这些是**累积资产**，一次切片的一条教训要跟之前的一起留着。
    单文件按月分（decisions-2026-10.md），避免单个文件长到没法读。
    """
    if kind not in ("decisions", "lessons", "conventions"):
        raise ValueError(f"kind 只能是 decisions/lessons/conventions，收到 {kind!r}")
    d = wiki_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{kind}-{time.strftime('%Y-%m', time.localtime())}.md"
    if not path.exists():
        path.write_text(f"# {kind}（{time.strftime('%Y-%m')}）\n\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## {now()} {title}\n\n")
        if by:
            fh.write(f"（记录者：{by}）\n\n")
        fh.write(body.rstrip() + "\n")
    return path


def wiki_search(root: Path, query: str, limit: int = 20) -> list[dict]:
    """在 wiki 里检索。返回 [{file, line_no, text, context}]，按命中数排序。"""
    q = query.strip().lower()

    if not q:
        return []
    hits: list[dict] = []
    for path in wiki_files(root):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        section = ""
        for i, line in enumerate(lines, 1):
            if line.startswith("## "):
                section = line[3:].strip()
            if q in line.lower():
                lo, hi = max(0, i - 2), min(len(lines), i + 2)
                hits.append({
                    "file": path.name, "line": i, "section": section,
                    "text": line.strip()[:200],
                    "context": "\n".join(lines[lo:hi]).strip()[:600],
                })
    hits.sort(key=lambda h: (h["section"], h["line"]))
    return hits[:limit]


# ---------------------------------------------------------------- 章程变更检测
#
# **为什么需要。** `up` 时章程一次性投进 pane，之后你改了 roles/pm.md，正在跑的
# pane 拿到的还是旧的；而 PROTOCOL.md 是「源比副本新就自动覆盖」的。两者规则
# 不一致，于是「我改了章程怎么没生效」要靠人自己想起来跑 sync。
#
# 修法不是让章程也自动覆盖（agent 得**重读并当场承认**，覆盖文件不算数），
# 而是**把变更 detectable 出来**：记指纹，`status` 主动提醒，只在真的变了时重投。
def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def rules_fingerprint(roles_dir: Path, protocol: Path,
                      skills_dir: Path | None = None) -> dict[str, str]:
    """当前章程、协议与输出规范的指纹。缺文件记空串（= 那一刻的状态）。

    **skills/ 必须一起算。** STE 输出规范（skills/ste/）是 0.1.6 起新增的，
    而 PROTOCOL.md 里只放了 8 条精简版、正文写「完整规范见 skills/ste/SKILL.md」。
    也就是说**真正的输出契约住在 skills/ 里**。指纹不覆盖它，就出现这种情况：
    某次发版只改了 skills/ste/ → stale 为空 → sync 说「没变化，不打扰」→
    正在跑的 agent 继续按旧规范输出，而它在 status 里看不出任何异常。
    """
    out = {}
    for path in sorted(roles_dir.glob("*.md")) if roles_dir.exists() else []:
        out[path.stem] = _sha256(path)
    out["PROTOCOL"] = _sha256(protocol)
    # 用 rglob：规范文件在 skills/<name>/ 下面一层，不只平铺在 skills/ 里。
    if skills_dir is not None and skills_dir.exists():
        for path in sorted(skills_dir.rglob("*.md")):
            rel = path.relative_to(skills_dir).as_posix()
            out[f"skills/{rel}"] = _sha256(path)
    return out


def rules_state_path(root: Path) -> Path:
    return root / XTEAM_DIRNAME / "rules.json"


def load_rules_state(root: Path) -> dict:
    """读回「投进 pane 的那一版」。只有 fingerprint 那部分，没有记录时返回空。"""
    data = _read_json(rules_state_path(root))
    fp = data.get("fingerprint")
    return fp if isinstance(fp, dict) else {}


def injected_meta(root: Path) -> dict:
    """投进去那次的元信息：xteam 版本、CLI 面（有哪些子命令）。

    指纹只能回答「文件变没变」，答不了「**这是哪个版本投的**」。而后者才是人能
    直接用的答案：面板里 0.1.5、协议是 0.1.8 写的，一眼就看出该 sync 了。
    """
    data = _read_json(rules_state_path(root))
    return {k: v for k, v in data.items() if k != "fingerprint"}


def save_rules_state(root: Path, fingerprint: dict[str, str],
                     version: str = "", cli: list[str] | None = None) -> None:
    """记下「投进 pane 的是哪一版」。下次好比对是不是过期。

    `version` / `cli` 是给人看的：指纹是不透明哈希，回答不了「该更新了没有」。
    """
    path = rules_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"fingerprint": fingerprint, "injected_at": now()}
    if version:
        payload["xteam_version"] = version
    if cli is not None:
        payload["cli"] = cli
    write_json(path, payload, indent=2)


def stale_rules(root: Path, roles_dir: Path, protocol: Path,
                skills_dir: Path | None = None) -> list[str]:
    """哪些规则改了但还没投给正在跑的 pane。返回名字列表。

    **没有记录时返回空** —— 首次 up 之前不该报「过期」，那是噪声。
    """
    seen = load_rules_state(root)
    if not seen:
        return []
    now_fp = rules_fingerprint(roles_dir, protocol, skills_dir)
    return [k for k, v in now_fp.items() if seen.get(k) != v]


# 指纹里的键分三类，sync 对它们的处理**必须**不一样 ——
# 起因是一个真 bug：只改 PROTOCOL.md 时，stale = {"PROTOCOL"}，
# 而重投循环是 `if stale and role not in stale: continue`，三个角色都不在
# stale 里 → 一个都不重投 → sent=0 → save_rules_state 不执行 →
# 指纹永不更新 → status 永久报警，而 sync 修不好它。
#
#   · 角色名（pm/tl/dev）  章程全文变了 → 重投全文（agent 需重新通读）
#   · PROTOCOL             协议变了     → 所有人都受影响 → 都要通知
#   · skills/…             输出规范变了 → 都要通知（agent 按需自读那个文件）
def is_protocol_drift(key: str) -> bool:
    return key == "PROTOCOL" or key.startswith("skills/")


# ------------------------------------------------------- 上游版本检查
#
# **为什么不每次都查。** xteam 的 status / 门狗都要求「快」，而一次 HTTPS 往返
# 在正常网络下 200–800ms、离线时更久。把网络放进 status 的关键路径，等于让一个
# 用来做判断的命令变成一个会卡住的东西。所以：
#   · 结果**缓存**到 .xteam/update-check.json，status 只读缓存（不联网）
#   · 只有门狗（本来就在后台跑）才按 24h 冷却去刷一次
#   · 任何失败都只记进缓存的 error 字段，**绝不影响 xteam 本身**
#
# 为什么值得查：xteam 不是常驻进程，用户没有任何时刻会收到「上游有新版本」。
# 而「该不该更新」这个问题，只有对照上游版本才答得出来。
UPSTREAM_REPO = "ffqa/xpier-xteam"
UPDATE_CHECK_TTL = 24 * 3600


def update_check_path(root: Path) -> Path:
    return root / XTEAM_DIRNAME / "update-check.json"


def _parse_version(v: str) -> tuple:
    """'0.1.10' → (0,1,10)；也认 'xteam-v0.1.10' / 'v0.1.10'。

    **不能只用 lstrip("v")**：tag 是 `xteam-v0.1.8`，首字符是 `x`，
    lstrip 对它完全无效 → 版本号被当成非数字 → 恒等于 0 → 于是
    「上游有新版」永远显示不出来（而且不报错，最难发现的那种）。
    所以这里**从头剥掉所有非数字、非点的前缀**，再按点分段。
    """
    s = str(v).strip()
    i = 0
    while i < len(s) and not (s[i].isdigit() or s[i] == "."):
        i += 1
    s = s[i:]
    out = []
    for part in s.split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def newer_than(mine: str, latest: str) -> bool:
    """latest 是否比 mine 新。任一为空/解析不了 → False（不乱报）。"""
    if not mine or not latest:
        return False
    try:
        return _parse_version(latest) > _parse_version(mine)
    except (TypeError, ValueError):
        return False


def fetch_latest_release(repo: str = UPSTREAM_REPO,
                         timeout: float = 4.0) -> tuple[str, str]:
    """查上游最新 release。返回 (版本号, 错误说明)，成功时错误为空串。

    刻意用 urllib 而不是 `gh`：gh 不一定装在用户机器上，而这是 xteam 唯一的
    外部依赖面 —— 不该为一个提示信息引入一个 CLI 依赖。
    """
    import urllib.error
    import urllib.request
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json",
                      "User-Agent": "xteam-update-check"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = str(data.get("tag_name") or "")
        return (tag, "")          # 保留原样，交给 _parse_version 剥前缀
    except urllib.error.HTTPError as exc:
        return ("", f"HTTP {exc.code}")
    except Exception as exc:                     # 离线 / 超时 / DNS / JSON 坏
        return ("", type(exc).__name__)


def load_update_cache(root: Path) -> dict:
    data = _read_json(update_check_path(root))
    return data if isinstance(data, dict) else {}


def refresh_update_cache(root: Path, ttl: int = UPDATE_CHECK_TTL) -> dict:
    """按冷却刷新缓存。返回缓存内容（无论刷没刷）。"""
    cache = load_update_cache(root)
    if os.environ.get("XTEAM_NO_UPDATE_CHECK"):
        return cache
    checked = float(cache.get("checked_epoch") or 0)
    if checked and (time.time() - checked) < ttl:
        return cache
    latest, err = fetch_latest_release()
    cache = {
        "checked_at": now(),
        "checked_epoch": time.time(),
        "latest": latest,
        "error": err,
    }
    path = update_check_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, cache, indent=2)
    return cache


def display_version(v: str) -> str:
    """把 tag / 版本号统一显示成 '0.1.8'。认不出数字就原样返回（别显示空）。"""
    s = str(v or "").strip()
    if not s:
        return ""
    parsed = _parse_version(s)
    if not any(parsed):
        return s
    return ".".join(str(x) for x in parsed)


def update_hint(root: Path, mine: str) -> str:
    """「该不该更新」的一句话答案。缓存里没结果就返回空串。"""
    cache = load_update_cache(root)
    latest = str(cache.get("latest") or "")
    if not latest:
        return ""
    if not newer_than(mine, latest):
        return ""
    return (f"上游有新版本 {display_version(latest)}（本机 {display_version(mine) or '?'}）"
            + _how_to_update())


def _how_to_update() -> str:
    """按安装方式给不同的更新指令 —— 说错比不说更糟。

    判断依据是**安装目录在不在 Homebrew 的路径形状里**：brew 装的会落在
    `.../Cellar/xteam/<版本>/...`（而版本目录名每次升级都变，正好也说明
    「现在这版是 brew 什么时候装的」）。认不出就只说「去你的安装方式更新」，
    不硬猜命令。
    """
    here = Path(__file__).resolve().parent.parent
    h = str(here)
    if "Cellar" in h or "/opt/homebrew/" in h or "/usr/local/" in h:
        return " —— 更新：brew upgrade ffqa/tap/xteam"
    return " —— 更新：在 xteam 源码目录里 git pull"


def cli_drift(root: Path, current_cli: list[str]) -> list[str]:
    """CLI 面（子命令集合）相对上次投进去的增量。

    **为什么单列一类**：新增子命令/新 flag 属于「工具能力」变化，章程一个字没动。
    这类变更**不该**重投章程 —— agent 靠跑 `xteam <cmd> --help` 自然就能拿到，
    而章程全文重投会把真正的变更淹掉。只告知「多了哪些命令」即可。
    """
    prev = injected_meta(root).get("cli")
    if not isinstance(prev, list) or not prev:
        return []
    old = {str(c) for c in prev}
    return sorted(c for c in current_cli if c not in old)


# ---------------------------------------------------------------- API 契约
#
# **为什么需要它。** 跨项目最常见的失败不是代码写错，而是「前端照着记忆里的
# 接口实现」—— api 那边改了字段名，前端这边直到联调才发现。所以动共享契约层
# （shared-api）时，改了什么必须留下可查的约定。
#
# 约定：**一个契约一个文件**，文件名就是 `METHOD /path`，但路径里的 `/` 写成
# `_`（文件名不能直接带斜杠）：`POST /orders/{id}` → `POST _orders_{id}.md`。
CONTRACT_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


def contracts_dir(root: Path) -> Path:
    return root / XTEAM_DIRNAME / "contracts"


def contract_filename(method: str, path: str) -> str:
    """把 `POST /orders/{id}` 编码成 `POST _orders_{id}.md`，且**可逆**。

    **转义字符本身。** 早先只用 `/`→`_`，于是 `/user_profiles` 和
    `/user/profiles` 都变成 `GET _user_profiles.md` —— 两个不同接口互相覆盖，
    而 `contracts list` 还会把前者显示成后者，审查结果直接不可信（实测确认）。
    现在编码前先把 `_` 转义成 `__`，于是两个路径各得各的文件名。

    向后兼容：旧文件不含 `__`，解码规则一致（单个 `_` → `/`），仍然读得对。
    """
    m = method.strip().upper()
    p = path.strip() or "/"
    if not p.startswith("/"):
        p = "/" + p
    return f"{m} {p.replace('_', '__').replace('/', '_')}.md"


def parse_contract_name(name: str) -> tuple[str, str]:
    """`POST _orders.md` → ("POST", "/orders")。文件名不合规返回 ("", "")。

    解码规则：`__` → `_`，单个 `_` → `/`。必须是**从左往右一次扫描**——
    先把所有 `__` 换成占位符再统一替换 `/`，那样 `__` 会被二次处理。
    """
    stem = name[:-3] if name.endswith(".md") else name
    if " " not in stem:
        return "", ""
    method, _, path = stem.partition(" ")
    method = method.strip().upper()
    if method not in CONTRACT_METHODS or not path:
        return "", ""
    if not path.startswith("_"):
        return "", ""
    out: list[str] = []
    i = 0
    while i < len(path):
        ch = path[i]
        if ch == "_":
            if i + 1 < len(path) and path[i + 1] == "_":
                out.append("_")          # 转义的下划线
                i += 2
                continue
            out.append("/")              # 路径分隔符
        else:
            out.append(ch)
        i += 1
    return method, "".join(out)


def contract_real_path(path: Path) -> tuple[str, str]:
    """读契约文件里记的**真实** method/path，以 front-matter 为准。

    文件名在路径含下划线时可能有歧义：`GET _a_b_c.md` 既可能是 `/a/b/c`，
    也可能是旧文件名（当年 `/a_b/c` 就叫这个）。新建的文件会把真实路径写进
    front-matter，这里读它；老文件没有，就退回按文件名解码。
    """
    m, p = parse_contract_name(path.name)
    try:
        text = path.read_text(encoding="utf-8")[:400]
    except OSError:
        return m, p
    if not text.startswith("<!-- xteam-contract"):
        return m, p
    fm = text.split("-->", 1)[0]
    mm = re.search(r"^method:\s*(\S+)", fm, re.M)
    pm = re.search(r"^path:\s*(\S+)", fm, re.M)
    if not (mm and pm):
        return m, p
    method = mm.group(1).strip().upper()
    if method not in CONTRACT_METHODS:
        return m, p
    return method, pm.group(1).strip()


def contract_files(root: Path) -> list[Path]:
    d = contracts_dir(root)
    return sorted(d.glob("*.md")) if d.exists() else []


def contract_path(root: Path, name: str) -> Path | None:
    """把契约名解析成 `contracts/` 内的真实路径。越界或不合规返回 None。

    `name` 来自两处，都是**不可信输入**：命令行参数，以及 task.json（agent 写的）。
    直接 `contracts_dir(root) / name` 的话，`../../anything.md` 会写到契约目录之外
    —— 而 `contracts_missing()` 也接受同样的越界引用，于是 task.json 里写个
    `../../README.md` 就能让门禁以为「契约已存在」而放行。

    所以这里同时做两件事：格式必须符合 `METHOD _path.md`，且解析后的路径必须
    仍在 `contracts/` 内（用 resolve + 相对路径判定，挡掉符号链接和 `..`）。
    """
    if not parse_contract_name(name)[0]:
        return None
    base = contracts_dir(root).resolve()
    try:
        candidate = (base / name).resolve()
    except OSError:
        return None
    if candidate.parent != base:
        return None                  # 越界（含 ../ 与指向别处的符号链接）
    return candidate


def contract_log_change(root: Path, name: str, who: str, what: str) -> str:
    """往契约的变更记录里追一行，并落到共享时间线。

    改契约不留痕等于没改 —— 下游无从判断自己手里的实现还能不能用。

    **写不进就明确失败，绝不「报了成功其实没写」。** 之前找不到「## 变更记录」
    小节时会跳过写入却照样写时间线并返回成功，于是时间线说改了、契约文件里
    没有 —— 审计信息自相矛盾，比直接报错更糟。
    """
    path = contract_path(root, name)
    if path is None or not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
        if "## 变更记录" not in text:
            return ""            # 没有小节 → 什么都没写 → 不能报成功
        head, _, tail = text.partition("## 变更记录")
        path.write_text(head + "## 变更记录"
                        + tail.rstrip("\n") + "\n"
                        + f"| {now()} | {who} | {what.replace('|', '/')} | |\n",
                        encoding="utf-8")
    except OSError:
        return ""
    return log_timeline(root, "contracts", f"{name}：{what}")


CONTRACT_TEMPLATE = """<!-- xteam-contract
method: {method}
path: {path}
-->

# {method} {path}

> 由 xteam 维护。改这个文件 = 改接口约定，**必须**在时间线留痕并通知下游消费方。
>
> 上面那段注释里记着这个接口的**真实路径**。文件名在路径含下划线时可能有歧义
> （`/a_b/c` 与 `/a/b/c` 早先会共用一个文件名），所以以这里为准。

## 用途

<!-- 一句话：这条接口解决什么问题。 -->

## 请求

<!-- 方法、路径、query/body 字段、类型、必填性 -->

## 响应

<!-- 成功时的结构；字段名和类型必须写死，不要写「类似订单对象」这种 -->

## 错误码

<!-- 逐条：HTTP 状态 + 业务码 + 触发条件 + 客户端该怎么办 -->

## 变更记录

| 时间 | 谁 | 改了什么 | 谁受影响 |
|---|---|---|---|
"""


# ---------------------------------------------------------------- idle 判定


class IdleTracker:
    """按 agent_status + state_change_seq 累计非活动时长，跨进程重启持久。

    两个信号都算活动：`working`；seq 变化（状态在 done/idle 跳但动过）。
    计时必须持久，否则每次被杀重拉都清零，10min 告警永远打不出来（C-33）。
    """

    def __init__(self, state_file: Path) -> None:
        self.path = state_file
        self.state: dict[str, dict] = {}
        if state_file.exists():
            try:
                self.state = _read_json(state_file)
            except Exception:  # noqa: BLE001
                self.state = {}

    def observe(self, role: str, status: str, seq) -> int:
        """采样一个角色，返回它当前**已停滞**的秒数（0 = 刚动过）。

        区分三种「没在动」：
          1. 非 working 且 seq 不变 → 真空闲（可能做完，也可能卡住，靠 owes 区分）
          2. **working 但 seq 长期不变** → 假 working。agent 声称在干活，实际
             没有任何状态推进。实测踩过：PM 卡了 9 分钟，status 一直报 working，
             因为原判据只在 `status != working` 时才计时，这种卡死永远不告警。
          3. blocked → 停在 UI，交给选择题代答逻辑
        """
        now = int(time.time())
        rec = self.state.get(role, {})
        seq_changed = str(seq) != str(rec.get("seq"))

        if status == "working":
            if seq_changed or rec.get("working_since") is None:
                rec = dict(rec, since=now, seq=seq, alerted=0,
                           missing_since=0, last_active_at=now, working_since=now)
            rec["status"] = status
            self.state[role] = rec
            self.flush()
            return 0 if seq_changed else now - int(rec.get("working_since", now))

        rec = dict(rec, since=(now if seq_changed or rec.get("since") is None
                               else rec["since"]),
                   seq=seq, status=status, working_since=0)
        if seq_changed:
            rec["last_active_at"] = now
            rec["alerted"] = 0 if rec.get("alerted") != "answered" else "answered"
        self.state[role] = rec
        self.flush()
        return now - int(rec["since"])

    def nag_gate(self, key: str, ttl: int, stamp: int | None = None) -> bool:
        """冷却门：距上次为 True 是否已过 ttl。过了就记下时刻并返回 True。

        **为什么不用 observe() 凑。** observe 是给「角色」用的，靠 seq 变化判断
        状态是否推进；拿它记提醒时刻就得伪造 seq。而它只在 seq 变化时重置
        `since`，于是「先 clean 后 stale」这条路径上，第一次提醒会被压到
        下一个 ttl 之后 —— 恰好是最该立刻提醒的那一次被吞掉。
        这里显式记「上次提醒时刻」，语义只有它自己用。
        """
        now = int(stamp if stamp is not None else time.time())
        rec = self.state.get(key)
        last = int((rec or {}).get("nagged_at") or 0)
        if last and (now - last) < ttl:
            return False
        self.state[key] = {"nagged_at": now, "ttl": ttl}
        self.flush()
        return True

    def clear_nag(self, key: str) -> None:
        """提醒条件消失时清掉冷却，让下次真的发生时立刻能喊。"""
        if key in self.state:
            del self.state[key]
            self.flush()

    def is_fake_working(self, role: str) -> bool:
        """声称 working 但 seq 停滞够久 —— 判为假 working。

        读自己的 working_since，不接受调用方传时长：那个数可能是上一轮采样的，
        序列恢复后继续用它会误报（实测踩过这个坑）。
        """
        rec = self.state.get(role, {})
        if rec.get("status") != "working":
            return False
        since = int(rec.get("working_since", 0) or 0)
        if not since:
            return False
        return int(time.time()) - since >= STUCK_WORKING_SECS

    def mark_missing(self, role: str) -> int:
        now = int(time.time())
        rec = self.state.setdefault(role, {"seq": None, "alerted": 0})
        if not rec.get("missing_since"):
            rec["missing_since"] = now
        self.flush()
        return now - int(rec["missing_since"])

    def clear_missing(self, role: str) -> None:
        rec = self.state.get(role)
        if rec:
            rec["missing_since"] = 0
            self.flush()

    def alerted(self, role: str):
        """0/1/2 是告警阶梯；'suppressed'/'answered' 是字符串哨兵（必须先判类型）。"""
        v = self.state.get(role, {}).get("alerted", 0)
        return v if isinstance(v, str) else int(v or 0)

    def set_alerted(self, role: str, level) -> None:
        self.state.setdefault(role, {})["alerted"] = level
        self.flush()

    def alert_level(self, role: str) -> int:
        """告警阶梯的**数值**部分；字符串哨兵（suppressed/answered）算「已处理过」。

        **不要直接对 `alerted()` 做数值比较。** 它按设计就是 int-or-string 混合
        （见 test_alerted_suppressed_is_not_an_int），而调用点写着
        `tracker.alerted(role) < 1` —— 角色一旦被标成 "suppressed" 或 "answered"，
        下一轮巡检走到 working 分支就抛
        `TypeError: '<' not supported between instances of 'str' and 'int'`，
        **整个看门狗死掉**。实测两个哨兵值都会触发。

        用它取代所有 `< 1` / `== 0`：`== 0` 只在真是 0 时成立，语义正确；
        `< 1` 则是错的（字符串哨兵表示「这轮不打扰」，也该跳过告警）。
        """
        v = self.alerted(role)
        return v if isinstance(v, int) else 99

    def is_settled(self, role: str) -> bool:
        """这一轮是否已经「不必再催」（字符串哨兵，或阶梯已到顶）。"""
        return not isinstance(self.alerted(role), int)

    def last_active_at(self, role: str) -> int:
        return int(self.state.get(role, {}).get("last_active_at", 0) or 0)

    def state_since(self, role: str) -> int:
        return int(self.state.get(role, {}).get("since", 0) or 0)

    def current_status(self, role: str) -> str:
        return str(self.state.get(role, {}).get("status", "unknown"))

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        write_json(tmp, self.state, indent=1)
        tmp.replace(self.path)

    def recap_asked_for(self, role: str) -> str:
        """已经向这个角色要过哪一轮的 recap（切片名）。空串=还没要过。

        **必须独立于 `alerted`。** `alerted` 是 0/1/2 的告警阶梯，往里塞字符串
        会污染它：`alerted == 0` 不成立 → 再也不门铃催活；
        `isinstance(level, int)` 不成立 → 升级分支被跳过；
        `alerted < 1` 直接抛 `TypeError: '<' not supported between 'str' and 'int'`，
        而那行在 `status == "working"` 分支里 —— agent 从 idle 转 working 之后，
        下一轮巡检就崩，整个看门狗死掉。实测确认，不是理论风险。
        """
        return str(self.state.get(role, {}).get("recap_asked", ""))

    def recap_asked_all(self, role: str) -> set[str]:
        """已经向这个角色要过 recap 的**所有**切片名。

        `recap_asked` 单槽位只能记一个切片：问完 s1 再问 s2，s1 就被冲掉，
        下一轮判据以为 s1 没问过 → s1→s2→s1→s2 震荡，第三个切片永远轮不到。
        判据必须读集合；单槽位只留作「最近一次问的对象」兼容读法（既有
        用例与 context 空串语义都靠它），不再参与判据。
        """
        v = self.state.get(role, {}).get("recap_asked_all") or []
        return {str(x) for x in v}

    def set_recap_asked(self, role: str, task: str) -> None:
        rec = self.state.setdefault(role, {})
        rec["recap_asked"] = task            # 单槽位仍写「最近一次」（兼容读法）
        if task:                           # context 触发的空串不进集合
            asked = rec.setdefault("recap_asked_all", [])
            if task not in asked:
                asked.append(task)
        rec["recap_asked_at"] = int(time.time())
        self.flush()

    def recap_asked_ago(self, role: str) -> int:
        """距上次要 recap 过了多少秒。没有记录返回很大值（= 可以问）。"""
        v = self.state.get(role, {}).get("recap_asked_at")
        try:
            return int(time.time()) - int(v) if v else 10 ** 9
        except (TypeError, ValueError):
            return 10 ** 9

    def rotated_for(self, role: str) -> str:
        """最近一次为哪个切片执行了会话轮换。空串=还没轮换过。"""
        return str(self.state.get(role, {}).get("rotated_for", ""))

    def rotated_all(self, role: str) -> set[str]:
        """已经为该角色执行过会话轮换的所有切片名集合。"""
        v = self.state.get(role, {}).get("rotated_all") or []
        return {str(x) for x in v}

    def set_rotated(self, role: str, task: str) -> None:
        """记录已为该角色在该切片闭合后完成会话轮换。"""
        rec = self.state.setdefault(role, {})
        rec["rotated_for"] = task
        if task:
            all_rot = rec.setdefault("rotated_all", [])
            if task not in all_rot:
                all_rot.append(task)
        rec["rotated_at"] = int(time.time())
        self.flush()

    def rotated_ago(self, role: str) -> int:
        """距上次执行会话轮换过了多少秒。"""
        v = self.state.get(role, {}).get("rotated_at")
        try:
            return int(time.time()) - int(v) if v else 10 ** 9
        except (TypeError, ValueError):
            return 10 ** 9


def roles_touched_task(task: Path) -> set[str]:
    """这个切片里，哪些角色真的产出过东西。

    用来决定「该不该要这个角色的 recap」。原先一律要 —— 任何切片闭合都让三个
    角色各写一份结构化长文档：既费 token，又让没参与的人凭空编一段。

    判据是工件本身（谁写的它就是谁干的），不靠猜：
      · verdict.json / spec.md   → pm
      · request.md / ready.json → tl
      · delivered.json          → dev
    """
    out: set[str] = set()
    if (task / "verdict.json").exists() or (task / "spec.md").exists():
        out.add("pm")
    if (task / "request.md").exists() or (task / "ready.json").exists():
        out.add("tl")
    if (task / "delivered.json").exists():
        out.add("dev")
    return out


# ---------------------------------------------------------------- 时间戳 / 日志


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def fmt_epoch(epoch: int) -> str:
    if not epoch:
        return "—"                     # 从未发生，不是 1970 年
    return time.strftime("%m-%d %H:%M:%S", time.localtime(epoch))


def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def log_event(log_file: Path, message: str) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(f"{now_utc()} {message}\n")


def log_timeline(root: Path, role: str, summary: str, waiting: bool = False,
                 wait_for: str = "") -> str:
    path = root / XTEAM_DIRNAME / "watch" / "timeline.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [now(), role, summary.replace("\n", " ").replace("|", "/")[:300]]
    if waiting:
        parts.append("WAITING" + (f":{wait_for[:80]}" if wait_for else ""))
    line = " | ".join(parts)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return line


# ---------------------------------------------------------------- 第三方评审
#
# 为什么是「外部一次性调用」而不是再加一个常驻角色：
#   · PM 和 TL 已经用不同 agent/model，但**读的是同一份 spec.md**。若错在 spec
#     本身（漏了边界、判据不可判定），两个角色会一起错 —— 再加一个读同一份
#     spec 的常驻 reviewer，同样会错，而且引入「谁裁决」的新问题。
#   · 外部进程是**冷读**：没参与过前面的讨论，只看 spec + diff。它唯一能补上
#     的就是「需求本身有没有问题」这一格。
#   · 按需调用、零常驻成本，不给状态机增加义务分支。
#
# 实测：`qodercli -p -w <dir> "<prompt>"` 24s 返回，且真能读到仓库里的文件。
ONESHOT_VERIFIED: set[str] = {"qodercli"}

# **只允许预注册的 agent。**
# 以前 `.xteam/qa.json` 的 `agent` 字段是自由文本，经 normalize_kind（未知值
# 原样返回）后直接进 subprocess.run([agent, ...])，而 cwd 就是项目目录。
# 于是配置里写 `agent: "/usr/bin/curl"` 或 `"./evil.sh"` 就能在巡检
# （when=always 时自动触发）里执行任意本地程序，还能读到整个项目树。
#
# 折中说明：**没有做只读沙箱**（POSIX 上没有可靠的跨平台只读 bind mount，
# 而 macOS 上 sandbox-exec 已废弃）。改为「白名单 + 显式告知」——只放行我们
# 亲手验证过支持一次性调用的那几个，其余一律拒，并说明该怎么加。
QA_ALLOWED: set[str] = {"qodercli"}


class QaAgentRejected(ValueError):
    """qa.json 里的 agent 不在白名单。"""

QA_WHEN = ("manual", "always", "risky")

QA_DEFAULTS: dict = {
    "agent": "qodercli",   # 已实测支持「一次性调用 + 指定 cwd」
    "model": "",           # 空 = 用它自己的默认
    "when": "manual",      # manual=只手动 / always=每个门禁前 / risky=高风险切片
    "risky_projects": [],  # when=risky 时，这些项目也算高风险（shared-api 自动算）
    "prompt": "",          # 追加给外部 agent 的额外要求
    "timeout": 900,
}


class QaConfig:
    """`.xteam/qa.json` —— 第三方评审用什么 agent、什么时机跑。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / XTEAM_DIRNAME / "qa.json"

    def load(self) -> dict:
        raw = _read_json(self.path)
        cfg = dict(QA_DEFAULTS)
        for key in QA_DEFAULTS:
            if key in raw:
                cfg[key] = raw[key]
        if cfg["when"] not in QA_WHEN:
            cfg["when"] = QA_DEFAULTS["when"]
        # 白名单校验放在 load()，于是**每条执行路径**（手动 xteam qa run、
        # 巡检 when=always/risky 自动触发）都自动受约束 —— 不依赖调用方自觉。
        agent = str(cfg["agent"]).strip()
        if not QA_ALLOWED:
            raise QaAgentRejected(
                "qa.json 没有可用的 agent（白名单为空）。"
                f"实测支持一次性调用的：{'、'.join(sorted(QA_ALLOWED)) or '（无）'}")
        if agent not in QA_ALLOWED:
            raise QaAgentRejected(
                f"拒绝执行 {agent!r}：不在第三方评审的白名单里。"
                f"允许的：{'、'.join(sorted(QA_ALLOWED))}。"
                "确认它支持 `CLI -p -w <dir> <prompt>` 后，"
                "加到 xteam_lib.py 的 QA_ALLOWED 再用。")
        cfg["agent"] = agent
        cfg["risky_projects"] = [str(x) for x in (cfg["risky_projects"] or [])]
        return cfg

    def save(self, cfg: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = {k: cfg[k] for k in QA_DEFAULTS if k in cfg}
        body["_updated"] = now()
        write_json(self.path, body, indent=2)

    def configured(self) -> bool:
        return self.path.exists()


def qa_should_run(root: Path, task: Path) -> tuple[bool, str]:
    """这个切片门禁前要不要自动跑第三方评审。返回 (要不要, 理由)。

    默认 manual —— 每轮都跑会拖慢门禁，而多数切片 PM 和 TL 已经审够了。
    """
    cfg = QaConfig(root).load()
    when = cfg["when"]
    if when == "manual":
        return False, "when=manual（默认，只手动跑）"
    if when == "always":
        return True, "when=always"
    reg = Registry(root)
    projects = [reg.project_of_task(task)] if reg.names() else []
    if any(reg.is_shared(p) for p in projects if p):
        return True, f"共享契约项目（{projects[0]}），下游会受影响"
    listed = [p for p in cfg["risky_projects"] if p in (projects or [])]
    if listed:
        return True, f"标记为高风险项目（{listed[0]}）"
    return False, "when=risky，但这个切片不算高风险"


def review_path(task: Path) -> Path:
    return task / "external-review.md"


def build_review_prompt(root: Path, task: Path, extra: str = "") -> str:
    """给外部 agent 的提问。

    **不把 diff 贴进 prompt**：外部进程的 cwd 就在项目根，它自己 `git diff`
    就能看到。贴进来既可能超长，又会错过未提交/未跟踪的改动。
    """
    rel = task.relative_to(root) if str(task).startswith(str(root)) else task
    parts = [
        f"你是第三方评审，**独立于**这个项目的 pm / tl / dev 三方。",
        f"项目根目录就是你的当前目录。切片材料在 `{rel}/`。",
        "",
        "请对照两件事给出结论：",
        "",
        "1. **需求层**：读 "
        f"`{rel}/spec.md`，这个需求本身有没有遗漏、歧义、"
        "或者不可判定的验收标准？",
        "2. **实现层**：用 `git status` / `git diff` 看这次改动，"
        "它是否真的满足 spec 里的验收标准？",
        "",
        "要求：",
        "- 每条问题给出 `文件:行号` 和**为什么是问题**。",
        "- 只报你能验证的缺陷，不要泛泛的风格建议，不要猜测你没看到的代码。",
        "- 确实没问题就直接说没问题，不要为了凑数编问题。",
        "",
        "用中文回答。",
    ]
    if extra.strip():
        parts += ["", "额外要求：", extra.strip()]
    return "\n".join(parts)


def run_external_review(root: Path, task: Path, cfg: dict,
                       extra_prompt: str = "") -> tuple[bool, str]:
    """调一次外部 agent 做第三方评审，结果落进切片目录。

    返回 (是否成功, 说明)。**失败不抛异常** —— 门禁不该因为一个外部工具
    不可用就停摆，记下原因继续走。
    """
    agent = cfg["agent"]
    # 第二道闸：即使有人绕过 QaConfig.load（直接构造 cfg 调用本函数），
    # 执行前再查一次白名单。执行任意本地程序不是能靠调用方自觉的事。
    if agent not in QA_ALLOWED:
        return False, (f"拒绝执行 {agent!r}：不在第三方评审白名单里"
                       f"（允许：{'、'.join(sorted(QA_ALLOWED))}）")
    argv = [agent, "-p", "-w", str(root)]
    if cfg["model"]:
        argv += ["-m", cfg["model"]]
    argv.append(build_review_prompt(root, task, extra_prompt))
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=int(cfg["timeout"]))
    except FileNotFoundError:
        return False, f"{agent} 不在 PATH"
    except subprocess.TimeoutExpired:
        return False, f"{agent} 超过 {cfg['timeout']}s 没返回"
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        return False, f"{agent} 退出码 {proc.returncode}：{(proc.stderr or out)[:200]}"
    path = review_path(task)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"<!-- 第三方评审 · {agent} {cfg['model'] or '(默认模型)'} · {now()} -->\n\n"
        f"## 问题\n\n{out}\n",
        encoding="utf-8")
    return True, f"已写入 {path}"


def review_digest(task: Path, limit: int = 800) -> str:
    """把评审结论压成能塞进门铃的摘要。"""
    p = review_path(task)
    if not p.exists():
        return ""
    body = p.read_text(encoding="utf-8")
    body = body.replace("<!--", "").split("-->", 1)[-1]
    body = body.replace("## 问题", "").strip()
    return body[:limit] + ("…" if len(body) > limit else "")


def die(message: str, code: int = 1) -> None:
    print(f"xteam: {message}", file=sys.stderr)
    sys.exit(code)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, ValueError):
        return False
