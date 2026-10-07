#!/usr/bin/env python3
"""xteam 协议状态机与 idle 判定的验证。

这些用例断言的是**协议正确性**，不是「代码跑得起来」：每条断言都对应一个真实会
发生的协作故障（漏派活、误判停滞、把 round 回退当成已消费、门铃打给了没欠活的人）。

跑法：python3 tests/test_protocol.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import shutil
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _find_cli() -> Path:
    """找到 CLI 脚本。**两种布局都要认**：
      · 仓库/解包目录： bin/xteam + bin/xteam_lib.py（同级）
      · install.sh 装出来： lib/xteam + lib/xteam_lib.py
    只认仓库布局的话，装完的那份 `tests/` 跑不起来 —— 而打包验证正是靠
    「装到临时前缀再跑一遍」发现这个的。
    """
    for d in (_ROOT / "bin", _ROOT / "lib", _ROOT):
        if (d / "xteam").exists() and (d / "xteam_lib.py").exists():
            return d / "xteam"
    die_msg = (f"找不到 CLI：{_ROOT} 下既没有 bin/xteam 也没有 lib/xteam。\n"
               f"  目录内容：{sorted(p.name for p in _ROOT.iterdir())[:20]}")
    raise SystemExit(die_msg)


def repo_doc(rel: str) -> Path:
    """取仓库里的文件（章程/模板）。测试要断言它们的措辞。"""
    return _ROOT / rel


_CLI_DIR = _find_cli().parent
sys.path.insert(0, str(_CLI_DIR))
sys.path.insert(0, str(_ROOT))
# CLI 脚本没有 .py 后缀，用 SourceFileLoader 直接按路径加载，
# 这样测试能直接验证「选择题解析」这段真实代码而不是复制一份。
from importlib.machinery import SourceFileLoader
_cli = SourceFileLoader("xteam_cli", str(_find_cli())).load_module()
_parse_options = _cli._parse_options

from xteam_lib import (  # noqa: E402
    IDLE_ALERT_SECS,
    RECAP_CONTEXT_COOLDOWN,
    DEFAULT_MODEL_HINT,
    HERDR_KINDS,
    Herdr,
    IdleTracker,
    recap_path,
    json_health,
    roles_touched_task,
    STUCK_WORKING_SECS,
    Protocol,
    Registry,
    RoleSpec,
    plausible_model,
    rules_fingerprint,
    save_rules_state,
    stale_rules,
    is_protocol_drift,
    cli_drift,
    injected_meta,
    newer_than,
    _parse_version,
    parse_contract_name,
    contracts_dir,
    contract_path,
    contract_real_path,
    contract_log_change,
    contract_filename,
    CONTRACT_TEMPLATE,
    agent_catalog,
    save_verified_agent,
    tui_model_ok,
    QaConfig,
    QaAgentRejected,
    QA_ALLOWED,
    qa_should_run,
    review_digest,
    review_path,
    fmt_duration,
    fmt_epoch,
    list_models,
    load_roles,
    normalize_kind,
    model_cost_risk,
    now,
    wiki_append,
    wiki_files,
    wiki_search,
    search_models,
    suggest_kinds,
    subagent_capability,
    PASTE_CHIP_LINES,
    render_board,
    detect_choice_prompt,
)

FAILURES: list[str] = []

# 状态机的推进顺序。写文件 = 推进一级。阶段一（共识）在 request 之前。
STAGE_FILES: dict[str, tuple[str, object]] = {
    "spec":      ("spec.md", "# 需求"),
    "specv":     ("spec.json", {"round": 1}),
    "assess":    ("assessment.json", {"spec_round": 1, "verdict": "agree", "objections": []}),
    "agree":     ("agreement.json", {"spec_round": 1, "note": "达成一致"}),
    "request":   ("request.md", "# 拆解"),
    "delivered": ("delivered.json", {"round": 1, "head": "bbb", "verification": []}),
    "ready":     ("ready.json", {"delivery": 1}),
    "verdict":   ("verdict.json", {"round": 1, "delivery": 1, "verdict": "PASS"}),
    "consumed":  ("consumed.json", {"round": 1, "note": "已取"}),
    "closed":    ("closed.md", "本切片闭合"),
}
STAGE_ORDER = list(STAGE_FILES)


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}\n      got:  {got!r}\n      want: {want!r}")
        FAILURES.append(name)


class Bench:
    """一个临时项目 + 一个 task 目录，可按阶段推进。"""

    def __init__(self, slug: str = "a") -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.slug = slug
        self.proto = Protocol(self.tmp)
        self.proto.ensure()
        self.task = self.tmp / ".xteam" / "tasks" / slug
        self.task.mkdir(parents=True, exist_ok=True)
        self.stage = -1

    def advance_to(self, stage: str) -> "Bench":
        """把所有 ≤ stage 的文件都写出来（幂等）。"""
        target = STAGE_ORDER.index(stage)
        while self.stage < target:
            self.stage += 1
            name, content = STAGE_FILES[STAGE_ORDER[self.stage]]
            path = self.task / name
            path.write_text(
                content if isinstance(content, str) else json.dumps(content),
                encoding="utf-8",
            )
        return self

    def write(self, name: str, content) -> None:
        path = self.task / name
        path.write_text(
            content if isinstance(content, str) else json.dumps(content),
            encoding="utf-8",
        )

    def drop(self, name: str) -> None:
        (self.task / name).unlink(missing_ok=True)

    def owes(self, role: str) -> list[str]:
        return [obligation for _, obligation in self.proto.obligations()[role]]

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- 用例


def test_chain_walks_one_role_at_a_time() -> None:
    print("\n[1] 状态机逐级推进：每一级只欠下一个角色的活")
    b = Bench()
    try:
        check("空目录：全员不欠", {r: b.owes(r) for r in ("pm", "tl", "dev")},
              {"pm": [], "tl": [], "dev": []})

        b.advance_to("spec")
        check("有 spec 但没版本号 → PM 欠 version-spec", b.owes("pm"), ["version-spec"])

        b.advance_to("specv")
        check("spec v1 就绪 → TL 欠 assess（不许直接拆）", b.owes("tl"), ["assess"])
        check("spec v1 就绪 → dev 不欠", b.owes("dev"), [])

        b.advance_to("assess")
        check("TL 已回评估 → PM 欠 settle（必须回应）", b.owes("pm"), ["settle"])

        b.advance_to("agree")
        check("达成一致 → TL 欠 decompose", b.owes("tl"), ["decompose"])

        b.advance_to("request")
        check("有 request → TL 不欠", b.owes("tl"), [])
        check("有 request → dev 欠实现", b.owes("dev"), ["implement"])

        b.advance_to("delivered")
        check("已交付 → dev 不欠", b.owes("dev"), [])
        check("已交付 → TL 欠 review", b.owes("tl"), ["chase"])

        b.advance_to("ready")
        check("TL ready → PM 欠 gate", b.owes("pm"), ["gate"])
        check("TL ready → TL 不欠", b.owes("tl"), [])

        b.advance_to("verdict")
        check("出 verdict → dev 欠消费", b.owes("dev"), ["consume"])
        check("出 verdict → PM 不欠", b.owes("pm"), [])

        b.advance_to("consumed")
        check("已消费 → 全员不欠（idle 是合法终态）",
              {r: b.owes(r) for r in ("pm", "tl", "dev")},
              {"pm": [], "tl": [], "dev": []})
    finally:
        b.cleanup()


def test_unconsumed_verdict_keeps_dev_busy() -> None:
    print("\n[2] verdict 未消费 → dev 仍被判欠活（防停摆的核心判据）")
    b = Bench()
    try:
        b.advance_to("ready")
        b.write("verdict.json", {"round": 3, "delivery": 1, "verdict": "PASS"})
        check("consumed 缺失 → 欠 consume", b.owes("dev"), ["consume"])

        b.write("consumed.json", {"round": 2})
        check("verdict r3 > consumed r2 → 仍欠 consume", b.owes("dev"), ["consume"])

        b.write("consumed.json", {"round": 3})
        check("round 对齐 → 已消费", b.owes("dev"), [])
    finally:
        b.cleanup()


def test_round_regression_is_conservative() -> None:
    print("\n[3] round 回退判为「未消费」而非「已完成」")
    # apartment 实测：phase0-foundation 的 verdict 从 r7 回退到 r6，consumed 仍是 7。
    # 朴素判据 `consumed >= verdict` 会读成已完成，该 topic 于是永久卡死再无人催。
    b = Bench()
    try:
        b.advance_to("ready")
        b.write("verdict.json", {"round": 6, "delivery": 1, "verdict": "PASS"})
        b.write("consumed.json", {"round": 7})
        check("verdict r6 < consumed r7 → 保守判未消费", b.owes("dev"), ["consume"])
    finally:
        b.cleanup()


def test_legacy_consumed_key_is_readable() -> None:
    print("\n[4] 兼容历史 consumed_round 键（只认 round 曾把已消费误报成未消费）")
    b = Bench()
    try:
        b.advance_to("ready")
        b.write("verdict.json", {"round": 3, "delivery": 1, "verdict": "PASS"})
        b.write("consumed.json", {"consumed_round": 3})
        check("consumed_round=3 对齐 verdict=3 → 已消费", b.owes("dev"), [])
    finally:
        b.cleanup()


def test_closed_frees_everyone_and_prompts_next_slice() -> None:
    print("\n[5] closed 之后：TL/dev 放空，PM 收到「去派下一项」")
    b = Bench()
    try:
        # 队列还有活：closed 后 PM 该去取下一项（别停），不是写结项报告。
        (b.proto.dir / "QUEUE.md").write_text(
            "| # | 切片 | 状态 | 备注 |\n|---|---|---|---|\n"
            "| 1 | a | done | |\n| 2 | b | todo | |\n", encoding="utf-8")
        b.advance_to("closed")
        check("closed → TL 不欠", b.owes("tl"), [])
        check("closed → dev 不欠", b.owes("dev"), [])
        check("closed → PM 欠 next-slice（别停）", b.owes("pm"), ["next-slice"])
        check("closed task 不再算未闭合", [t.name for t in b.proto.open_tasks()], [])
    finally:
        b.cleanup()


def test_all_closed_without_report_pm_owes_report() -> None:
    print("\n[5b] 全部闭合但 REPORT.md 缺失 → PM 欠 report；写完就不欠")
    b = Bench()
    try:
        b.advance_to("closed")
        # Bench 不写 QUEUE.md → queue_counts()==(0,0)，且有一个闭合切片，
        # 正好命中 report 条件（空项目无切片时不命中，见下）。
        check("全闭合无 REPORT → PM 欠 report",
              b.proto.overall_debts().get("pm"), ["(项目):report"])
        (b.proto.dir / "REPORT.md").write_text("# 结项报告\n", encoding="utf-8")
        check("REPORT 写完 → PM 不再欠",
              b.proto.overall_debts().get("pm"), None)
    finally:
        b.cleanup()
    # 空项目（无切片、无队列）：要的是派活，不是写报告
    b2 = Bench("empty-probe")
    try:
        b2.task.rmdir()  # 删掉 Bench 自带的空 task 目录
        check("空项目 → PM 不欠 report",
              b2.proto.overall_debts().get("pm"), None)
    finally:
        b2.cleanup()

    # **队列从「有活」变「空」时，PM 的义务要跟着换。** 这是本次改动的核心：
    # 队列有活时催 next-slice（别停）是对的；队列空了还催 next-slice 就是空转，
    # 真正的收尾是结项报告。只测两个端点会漏掉「切换」本身 ——
    # 而切换处最容易出「两条义务同时挂着」或「一条都不挂」的 bug。
    b3 = Bench("handoff")
    try:
        qpath = b3.proto.dir / "QUEUE.md"
        qpath.write_text(
            "| # | 切片 | 状态 | 备注 |\n|---|---|---|---|\n"
            "| 1 | a | todo | |\n", encoding="utf-8")
        b3.advance_to("closed")
        check("队列有活 + 闭合 → PM 欠 next-slice（F-20：标签指首个未开工项，非闭合片名）",
              b3.proto.overall_debts().get("pm"), ["a:next-slice"])
        check("此时不欠 report（还没到结项）",
              "report" in str(b3.proto.overall_debts().get("pm")), False)
        # 把队列标完 → 义务应当让位给 report，且 next-slice 消失（不是两条并存）
        qpath.write_text(
            "| # | 切片 | 状态 | 备注 |\n|---|---|---|---|\n"
            "| 1 | a | done | |\n", encoding="utf-8")
        d = b3.proto.overall_debts().get("pm")
        check("队列跑完（仍有行、全部 done）→ PM 欠 report", d, ["(项目):report"])
        check("next-slice 已让位（不与 report 并存）",
              "next-slice" in str(d), False)
        check("REPORT 写完 → 什么都不欠（真实项目路径）",
              (proto_dir_reports := b3.proto.dir).joinpath("REPORT.md").exists(), False)
        (proto_dir_reports / "REPORT.md").write_text("# 结项报告\n", encoding="utf-8")
        check("写完 REPORT 后 PM 清空", b3.proto.overall_debts().get("pm"), None)
    finally:
        b3.cleanup()

    # **回归：报告义务必须按「有没有待办」判，而不是「队列里有没有行」。**
    # 原判据是 queue_counts() == (0, 0)，而 Protocol.ensure() 一上来就建出
    # QUEUE.md、TL 追加切片后 total 永远 > 0 —— 于是真实项目里这条义务
    # 一次都不会触发，结项报告永远没人催。而当时的测试用的是 Bench
    # （不写 QUEUE.md，total 恰好为 0），正好落在唯一能通过的那个分支上，
    # 于是「测试全绿 + 功能是死的」同时成立。
    # 这条断言就是照着真实布局写的：先 ensure() 建骨架，再用 TL 的写法追加行。
    b4 = Bench("realistic")
    try:
        q4 = b4.proto.dir / "QUEUE.md"
        q4.write_text(
            "| 序 | 切片 | 状态 | 备注 |\n|---|---|---|---|\n"
            "| 1 | a | done | |\n", encoding="utf-8")
        b4.advance_to("closed")
        check("队列有行但全部 done → 仍欠 report（真实项目路径）",
              b4.proto.overall_debts().get("pm"), ["(项目):report"])
    finally:
        b4.cleanup()


def test_fail_round_reopens_the_loop() -> None:
    print("\n[6] FAIL 打回后链路能转回来：dev 重做 → TL 重审 → PM 重判")
    b = Bench()
    try:
        b.advance_to("ready")
        b.write("verdict.json", {"round": 1, "delivery": 1, "verdict": "FAIL"})
        check("FAIL 判在本次交付上 → dev 欠重做（不是 consume）",
              b.owes("dev"), ["implement"])

        # dev 修完重交：交付号 +1。旧 ready 仍指向 delivery 1，于是 TL 立刻重新欠 review
        # ——这正是不带轮次时会死锁的地方。
        b.write("delivered.json", {"round": 2, "head": "ccc", "verification": []})
        check("重交后 → TL 欠 chase（旧 ready 只覆盖 delivery 1）",
              b.owes("tl"), ["chase"])
        check("重交后 → PM 不欠 gate（已判过 delivery 1，要等 TL 放行 delivery 2）",
              b.owes("pm"), [])

        b.write("ready.json", {"delivery": 2})
        check("TL 放行 delivery 2 → PM 才欠 gate", b.owes("pm"), ["gate"])

        b.write("verdict.json", {"round": 2, "delivery": 2, "verdict": "PASS"})
        check("二轮 PASS → dev 欠 consume", b.owes("dev"), ["consume"])

        b.write("consumed.json", {"round": 2})
        check("二轮已消费 → 全员不欠",
              {r: b.owes(r) for r in ("pm", "tl", "dev")},
              {"pm": [], "tl": [], "dev": []})
    finally:
        b.cleanup()


def test_multiple_tasks_accumulate_debts() -> None:
    print("\n[7] 并行切片：多个 task 的义务累加，不互相覆盖")
    b = Bench("a")
    try:
        b.advance_to("specv")                      # a 有版本号但 TL 未评估
        second = b.proto.tasks / "b"
        second.mkdir(parents=True, exist_ok=True)
        (second / "request.md").write_text("# b 拆解", encoding="utf-8")
        debts = b.proto.obligations()
        check("a 欠 TL 评估", [o for _, o in debts["tl"]], ["assess"])
        check("b 欠 dev 实现", [o for _, o in debts["dev"]], ["implement"])
        check("a 未拆解时不额外欠 dev（一个 task 一个义务）", len(debts["dev"]), 1)
    finally:
        b.cleanup()


def test_agreement_round_is_mandatory() -> None:
    print("\n[20] 需求共识：TL 不回评估就开不了工，PM 不回应就拆不了")
    b = Bench()
    try:
        b.advance_to("spec")
        check("spec 无版本号 → PM 欠 version-spec", b.owes("pm"), ["version-spec"])
        check("此时 TL 不欠（还没到评估）", b.owes("tl"), [])

        b.advance_to("specv")
        check("spec v1 → TL 欠 assess", b.owes("tl"), ["assess"])
        check("TL 没回之前 PM 不欠（球在 TL 手上）", b.owes("pm"), [])

        b.advance_to("assess")
        check("TL 回了 → PM 欠 settle", b.owes("pm"), ["settle"])
        check("PM 没回应之前 TL 不欠拆解", b.owes("tl"), [])

        b.advance_to("agree")
        check("达成一致 → TL 欠 decompose", b.owes("tl"), ["decompose"])
    finally:
        b.cleanup()


def test_tl_objection_reopens_the_discussion() -> None:
    print("\n[21] TL 提异议：PM 改 spec 后要重新评估，不能直接开拆")
    b = Bench()
    try:
        b.advance_to("specv")
        b.write("assessment.json",
                {"spec_round": 1, "verdict": "object", "objections": ["缺一条边界"]})
        check("TL 提异议 → PM 欠 settle", b.owes("pm"), ["settle"])
        check("有异议时 TL 不欠拆解", b.owes("tl"), [])

        # PM 改了 spec：spec.json round+1，旧评估立刻失效
        b.write("spec.json", {"round": 2})
        check("spec 升到 v2 → TL 又欠 assess", b.owes("tl"), ["assess"])
        check("PM 已回应过 → 不再欠 settle", b.owes("pm"), [])

        b.write("assessment.json",
                {"spec_round": 2, "verdict": "agree", "objections": []})
        check("v2 达成评估 → PM 又欠 settle 确认", b.owes("pm"), ["settle"])

        b.write("agreement.json", {"spec_round": 2, "note": "一致"})
        check("确认后 → TL 欠 decompose", b.owes("tl"), ["decompose"])
    finally:
        b.cleanup()


def test_blocked_forces_pm_to_act() -> None:
    print("\n[22] 阻塞优先：TL/dev 报阻塞后 PM 必须先解开，下游才谈别的")
    b = Bench()
    try:
        b.advance_to("request")
        check("正常执行中 PM 不欠 unblock", b.owes("pm"), [])

        b.write("blocked.json",
                {"round": 1, "role": "tl", "where": "拆解时", "question": "范围收窄还是先建账单？"})
        check("TL 报阻塞 → PM 欠 unblock（最高优先级）", b.owes("pm"), ["unblock"])
        check("阻塞时 PM 不再欠别的事（被 unblock 压过）",
              [o for o in b.owes("pm") if o != "unblock"], [])

        b.write("pm-response.json", {"round": 1, "decision": "收窄范围，账单留下一期"})
        check("PM 回应后 → 不再欠 unblock", b.owes("pm"), [])

        # dev 又报一轮
        b.write("blocked.json",
                {"round": 2, "role": "dev", "where": "实现时", "question": "验收第3条怎么判？"})
        check("再次阻塞 → PM 又欠 unblock", b.owes("pm"), ["unblock"])
    finally:
        b.cleanup()


def test_parse_options_prefers_recommended() -> None:
    print("\n[23] 选择题解析：识别选项并优先选推荐项")
    tail = """
  下一批做哪一项？

  1. 退租/退房切片（推荐，承接项已就绪）
  2. cloud-card-issuance SPEC
  3. 先补 Round 2 剩下的三项

  回复编号，或直接说别的。
"""
    opts = _parse_options(tail)
    check("解析出 3 个选项", len(opts), 3)
    check("识别出推荐项", [o["recommended"] for o in opts], [True, False, False])
    check("剥掉括号后是干净的选项文字", opts[0]["text"], "退租/退房切片")


def test_parse_options_falls_back_to_first() -> None:
    print("\n[24] 没有推荐标记时选第一项；识别不出就不乱按")
    opts = _parse_options("1. 方案 A\n2. 方案 B\n3. 方案 C\n")
    check("无推荐标记时都判 False", any(o["recommended"] for o in opts), False)
    check("退回选第一项", opts[0]["text"], "方案 A")
    # 看不懂的界面绝不能瞎按
    check("非选择题返回空（不代答）", _parse_options("Build · DeepSeek\n错误：超时"), [])
    check("空文本返回空", _parse_options(""), [])
    check("识别 TUI 高亮写法", [o["text"] for o in _parse_options("> 1. 继续\n  2. 取消\n")],
          ["继续", "取消"])


def test_queue_recommends_head_when_unmarked() -> None:
    print("\n[25] 队列推荐项：没人标推荐时队首即推荐（「下一件做什么」有答案了）")
    tmp = Path(tempfile.mkdtemp())
    try:
        p = Protocol(tmp)
        p.ensure()
        p.dir.joinpath("QUEUE.md").write_text(
            "# 队列\n\n"
            "| 序 | 切片 | 状态 | 备注 |\n|---|---|---|---|\n"
            "| 1 | checkout | `done` | 已闭合 |\n"
            "| 2 | cloud-card | `todo` | 被 D-13 阻塞 |\n"
            "| 3 | round2 | `todo` | 三项收尾 |\n",
            encoding="utf-8")
        check("解析出 3 项", len(p.queue_items()), 3)
        check("done 的不算待办，推荐下一项", p.recommended_task()["slice"], "cloud-card")

        p.dir.joinpath("QUEUE.md").write_text(
            "# 队列\n\n"
            "| 序 | 切片 | 状态 | 备注 |\n|---|---|---|---|\n"
            "| 1 | a | `todo` | 普通项 |\n"
            "| 2 | b | `todo` | **推荐** 承接项就绪 |\n",
            encoding="utf-8")
        check("显式标「推荐」时以它为准", p.recommended_task()["slice"], "b")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_idle_requires_both_signals() -> None:
    print("\n[8] idle 双条件：working 或 seq 变化都算活动")
    tmp = Path(tempfile.mkdtemp())
    try:
        state = tmp / "idle.state"
        tr = IdleTracker(state)
        tr.observe("dev", "working", 100)
        check("刚采样 → 0s", tr.observe("dev", "working", 100), 0)
        check("seq 变化（即使状态是 idle）→ 重新计时",
              tr.observe("dev", "idle", 101), 0)
        check("状态与 seq 均未变 → 累计非活动时长 >= 0",
              tr.observe("dev", "idle", 101) >= 0, True)

        reloaded = IdleTracker(state)
        check("计时状态跨进程持久（否则 10min 告警永远打不出来）",
              reloaded.observe("dev", "idle", 101) >= 0, True)
        check("新角色首次采样不炸", reloaded.observe("tl", "done", 5), 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_missing_pane_detected_and_cleared() -> None:
    print("\n[9] pane 消失要能检出，恢复后计时清零")
    tmp = Path(tempfile.mkdtemp())
    try:
        tr = IdleTracker(tmp / "idle.state")
        check("首次标 missing → 0s", tr.mark_missing("tl"), 0)
        check("持续 missing → 累计", tr.mark_missing("tl") >= 0, True)
        tr.clear_missing("tl")
        check("恢复后归零", tr.mark_missing("tl"), 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_alert_escalation_ladder() -> None:
    print("\n[10] 告警阶梯：0 → 1 → 2，不重复轰炸")
    tmp = Path(tempfile.mkdtemp())
    try:
        tr = IdleTracker(tmp / "idle.state")
        check("初始未告警", tr.alerted("dev"), 0)
        tr.set_alerted("dev", 1)
        check("一级后读回 1", tr.alerted("dev"), 1)
        tr.set_alerted("dev", 2)
        check("升级后读回 2", tr.alerted("dev"), 2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ensure_is_idempotent() -> None:
    print("\n[11] ensure() 建骨架且不覆盖已有文件")
    tmp = Path(tempfile.mkdtemp())
    try:
        p = Protocol(tmp)
        p.ensure()
        check("QUEUE.md 建出", (tmp / ".xteam" / "QUEUE.md").exists(), True)
        check("tasks/ 建出", (tmp / ".xteam" / "tasks").is_dir(), True)
        (tmp / ".xteam" / "QUEUE.md").write_text("TL 手写的队列", encoding="utf-8")
        p.ensure()
        check("重复 ensure 不覆盖手写队列",
              (tmp / ".xteam" / "QUEUE.md").read_text(encoding="utf-8"), "TL 手写的队列")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_tracker_records_last_active_and_state_since() -> None:
    print("\n[12] 时间戳：状态表要能回答「它从几点开始闲的」「最后动是什么时候」")
    tmp = Path(tempfile.mkdtemp())
    try:
        tr = IdleTracker(tmp / "idle.state")
        tr.observe("dev", "working", 10)
        first_active = tr.last_active_at("dev")
        check("working 采样后记下 last_active_at", first_active > 0, True)
        check("last_active_at 不是未来", first_active <= int(time.time()) + 1, True)

        # 之后一直 idle：状态起点不再变，last_active_at 保持在上次活动那一刻
        tr.observe("dev", "idle", 10)
        tr.observe("dev", "idle", 10)
        check("持续 idle 时 last_active_at 不被刷新",
              tr.last_active_at("dev"), first_active)
        check("state_since 已记录", tr.state_since("dev") > 0, True)
        check("current_status 读回 idle", tr.current_status("dev"), "idle")

        tr.observe("dev", "idle", 11)   # seq 变化 = 又动了一下
        check("seq 变化后 last_active_at 前进或持平",
              tr.last_active_at("dev") >= first_active, True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_alerted_suppressed_is_not_an_int() -> None:
    print("\n[13] 免打扰哨兵是字符串，不能和整数比大小（否则巡检崩）")
    tmp = Path(tempfile.mkdtemp())
    try:
        tr = IdleTracker(tmp / "idle.state")
        tr.set_alerted("pm", "suppressed")
        check("读回字符串哨兵", tr.alerted("pm"), "suppressed")
        check("isinstance 判类型后可安全跳过",
              isinstance(tr.alerted("pm"), int) and tr.alerted("pm") >= 2, False)
        tr.set_alerted("pm", 0)
        check("复位回整数 0", tr.alerted("pm"), 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_timestamp_formatting() -> None:
    print("\n[14] 时间戳格式化：从未发生显示「—」而不是 1970 年")
    check("epoch 0 → —", fmt_epoch(0), "—")
    check("秒", fmt_duration(45), "45s")
    check("分秒", fmt_duration(200), "3m20s")
    check("时分", fmt_duration(7500), "2h05m")
    check("负数不炸", fmt_duration(-5), "0s")
    check("now() 长度 19", len(now()), 19)

def test_role_spec_defaults_and_overrides() -> None:
    print("\n[15] 角色配置：默认 kind + .xteam/team.json 覆盖")
    tmp = Path(tempfile.mkdtemp())
    try:
        specs = load_roles(tmp)
        check("默认 pm 用 omp（可指定模型；opencode 的 TUI 不认 --model）",
              specs["pm"].kind, "omp")
        check("默认 dev 用 devin", specs["dev"].kind, "devin")
        check("默认不带 model", specs["pm"].model, "")
        check("空 model 不传 --model（否则 CLI 可能启动失败）",
              specs["pm"].agent_args(), [])

        (tmp / ".xteam").mkdir(parents=True, exist_ok=True)
        (tmp / ".xteam" / "team.json").write_text(json.dumps(
            {"roles": {"pm": {"model": "anthropic/claude-opus-4-5"},
                       "dev": {"kind": "claude", "model": "opus"}}}
        ), encoding="utf-8")

        specs = load_roles(tmp)
        check("team.json 覆盖 pm 的 model", specs["pm"].model,
              "anthropic/claude-opus-4-5")
        check("未指定 model 不被覆盖成空", specs["tl"].model, "")
        check("team.json 可改 kind", specs["dev"].kind, "claude")
        # 显式指定 opencode（默认已不用它了）：能力事实仍然要守住 ——
        # 有人 --set-agent pm=opencode 时必须照样拦住 --model，而不是静默丢掉
        oc = RoleSpec("pm", "opencode", "产品经理", "some/model")
        check("opencode 不接受 --model（TUI 没有这个 flag）", oc.agent_args(), [])
        check("opencode 不支持脚本指定模型", oc.supports_tui_model(), False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_role_config_errors_are_loud() -> None:
    print("\n[16] 配置写错要立刻报错，不能静默用默认值")
    tmp = Path(tempfile.mkdtemp())
    try:
        (tmp / ".xteam").mkdir(parents=True, exist_ok=True)
        (tmp / ".xteam" / "team.json").write_text("{ 坏 JSON", encoding="utf-8")
        try:
            load_roles(tmp)
            check("坏 JSON 抛错", False, True)
        except RuntimeError as exc:
            check("坏 JSON 抛错并带原因", "team.json" in str(exc), True)

        (tmp / ".xteam" / "team.json").write_text(
            json.dumps({"roles": {"nope": {"model": "x"}}}), encoding="utf-8")
        try:
            load_roles(tmp)
            check("未知角色抛错", False, True)
        except RuntimeError as exc:
            check("未知角色抛错并点名", "nope" in str(exc), True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_model_mechanism_depends_on_agent() -> None:
    print("\n[17] 选模型按 agent 能力分流（本机实测结论）")
    # opencode v2.0.20 的 TUI 顶层没有 --model（只有 run / mini 有）。硬传会让它
    # 打印帮助后退出，herdr 等不到交互态。实测过且都不通的三条替代路径：
    # OPENCODE_MODEL 环境变量、OPENCODE_CONFIG 环境变量、项目 opencode.json 的
    # model 字段（配置确实被读到，但 TUI 用持久化的「上次所选模型」覆盖它）。
    oc = RoleSpec("pm", "opencode", "产品经理", "some/model")
    check("opencode 判定为不支持 TUI --model", oc.supports_tui_model(), False)
    check("opencode 不传 --model", oc.agent_args(), [])
    check("opencode 的 describe 如实说明限制",
          "不支持脚本指定模型" in oc.describe(), True)
    try:
        oc.preflight()
        check("preflight 应拒绝 opencode + model", False, True)
    except RuntimeError as exc:
        check("preflight 拒绝并给出两条替代方案",
              ("--set-agent" in str(exc) and "opencode.json" in str(exc)), True)

    # devin / claude / codex 的 TUI 认 --model（实测 devin --model devin-large
    # 6.8s 正常起来，argv 回显 ["devin","--model","devin-large"]）
    dv = RoleSpec("dev", "devin", "开发", "devin-large")
    check("devin 支持 TUI --model", dv.supports_tui_model(), True)
    check("devin 透传 --model", dv.agent_args(), ["--model", "devin-large"])
    check("devin 的 preflight 静默通过", dv.preflight(), None)
    check("devin describe 正常", dv.describe(), "devin devin-large")

    check("无 model 时不传任何参数", RoleSpec("pm", "opencode", "x").agent_args(), [])


def test_agent_capability_table() -> None:
    print("\n[18] agent 能力表：本机实测的 kind 白名单与别名")
    # 全部来自真实 herdr agent start + argv 回显，不是照抄文档
    for kind in ("devin", "cursor", "omp", "pi", "qodercli", "claude", "codex"):
        check(f"{kind} 在 herdr kind 列表里", kind in HERDR_KINDS, True)
    check("mcode 不在 herdr kind 列表（无法编排）", "mcode" in HERDR_KINDS, False)

    check("别名 cursor-agent → cursor", normalize_kind("cursor-agent"), "cursor")
    check("别名大小写不敏感", normalize_kind("Cursor-Agent"), "cursor")
    # qoderclicn 是**另一个程序**，不是 qodercli 的别名——不能折，
    # 折错的后果是静默用错 agent。
    check("qoderclicn 不被折成 qodercli（是两个程序）",
          normalize_kind("qoderclicn"), "qoderclicn")
    check("非别名原样返回", normalize_kind("devin"), "devin")

    check("拼错时给得出建议", "cursor" in suggest_kinds("cursor-agent"), True)
    check("无法猜测时不硬凑", suggest_kinds("zzz"), "")

    check("devin 推荐模型是 swe-2-max",
          DEFAULT_MODEL_HINT.get("devin"), "swe-2-max")


def test_preflight_rejects_before_side_effects() -> None:
    print("\n[19] preflight 在建任何东西之前就拦住坏配置")
    bad_kind = RoleSpec("pm", "mcode", "产品经理", "")
    try:
        bad_kind.preflight()
        check("未知 kind 应被拒", False, True)
    except RuntimeError as exc:
        check("未知 kind 被拒并列出 herdr 支持项",
              ("herdr 不认识" in str(exc) and "devin" in str(exc)), True)
        check("并说明 mcode 不可编排", "mcode" in str(exc), True)

    # 全部实测过的 kind 都应该静默通过 preflight。
    # omp 这里用 gpt-5 而不是 opus —— opus 在 omp 里全部落在 openrouter（559 个模型），
    # 用户已明确要求未经允许不用 openrouter。
    # **注入固定模型目录**，不读本机实时表：那张表随账号/版本变，
    # 会让同一份测试在不同机器上结论相反（曾是 Erratic Test 的典型来源）。
    fixed = {"gpt-5": "openai", "deepseek-v4-pro": "deepseek",
             "opus": "openrouter", "anthropic/claude-opus-4.5": "openrouter",
             "gpt-4": "openrouter"}
    for kind, model in (("devin", "swe-2-max"), ("cursor", "gpt-5"),
                        ("pi", "anthropic/claude-opus-4-5"),
                        ("qodercli", "claude-sonnet-4-5")):
        check(f"{kind} + {model} 通过 preflight",
              RoleSpec("dev", kind, "开发", model).preflight(), None)
    # omp 只验「放行」这一行为，不验它落哪个 provider（那是本机环境决定的）
    check("omp + gpt-5 放行（不依赖实时目录）",
          RoleSpec("dev", "omp", "开发", "gpt-5").preflight(), None)

    # 曾经真踩过的坑：--model opus 会模糊匹配到 openrouter 的 anthropic/claude-opus-*。
    # 现在只**提示**不拦（用户 2026-10-02：「可以像 omp 那样自己选，不用强行限死」），
    # 拦死等于替用户做决定。详见 test_openrouter_is_warning_not_block。
    check("omp + opus 放行（只提示 openrouter 额外付费）",
          RoleSpec("pm", "omp", "产品经理", "opus").preflight(), None)






















def test_model_cost_risk_with_injected_catalog() -> None:
    print("\n[35] 成本判定可用注入目录，不依赖本机实时模型表")
    # 固定目录：同样输入永远同样结论，换机器/换版本都不受影响
    fixed = {"gpt-5": "openai", "opus": "openrouter",
             "anthropic/claude-opus-4.5": "openrouter"}
    check("裸名 gpt-5 → 直连，放行", model_cost_risk("omp", "gpt-5", fixed), "")
    check("opus → openrouter，出风险提示",
          "openrouter" in model_cost_risk("omp", "opus", fixed), True)
    check("带前缀的镜像名 → 出提示",
          "openrouter" in model_cost_risk("omp", "anthropic/claude-opus-4.5", fixed), True)
    check("非 omp 的 agent 不查目录（不依赖它）",
          model_cost_risk("devin", "swe-2-max"), "")
    check("空目录时 omp 保守提示而非放行（失败即关）",
          "无法确认" in model_cost_risk("omp", "opus", {}), True)


def test_malformed_artifacts_never_crash_state_machine() -> None:
    print("\n[26] 畸形工件不崩状态机（agent 写错一个字符就会发生）")
    # JSON 合法不等于顶层是对象。agent 写出 [1,2] / null / "s" 时，
    # 直接返回会让下游所有 .get() 抛 AttributeError，把 status 和巡检带崩。
    base = {"spec.md": "s", "request.md": "r",
            "delivered.json": '{"round":1}', "ready.json": '{"delivery":1}'}
    for name, bad in [("空文件", ""), ("坏 JSON", "{oops"), ("顶层是数组", "[1,2]"),
                      ("null", "null"), ("顶层是字符串", '"hi"'), ("顶层是数字", "42")]:
        b = Bench()
        try:
            b.write("verdict.json", bad)
            b.advance_to("ready")
            b.proto.debts(b.task)          # 不崩即通过
            check(f"verdict={name} 不崩", True, True)
        except Exception as exc:
            check(f"verdict={name} 不崩", f"{type(exc).__name__}", True)
        finally:
            b.cleanup()

    # 每个工件位置 × 4 种畸形，都不能把状态机带崩
    crashed = []
    for fname in ("spec.json", "assessment.json", "agreement.json", "blocked.json",
                  "pm-response.json", "delivered.json", "ready.json",
                  "verdict.json", "consumed.json"):
        for bad in ("[1]", "null", '"s"', ""):
            b = Bench()
            try:
                b.write(fname, bad)
                b.proto.debts(b.task)
            except Exception as exc:
                crashed.append(f"{fname}={bad!r}:{type(exc).__name__}")
            finally:
                b.cleanup()
    check("9 工件 × 4 畸形 = 36 组合全部不崩", crashed, [])


def test_env_preflight_layers() -> None:
    print("\n[27] 环境体检三层：herdr 在不在 / herdr 认不认 kind / agent 装没装")
    # 这三层曾经因为一次批量替换而变成死代码，mcode 这类「装了但 herdr 不认」的
    # agent 会被静默放行，直到启动超时才暴露——报错还长得像模型名的问题。
    #
    # 用**固定替身**而不是真 herdr：断言的是三层检查的逻辑，不是「这台机器上
    # 装了什么」。读真实环境会让结论随机器变化。
    fixed_supported = {"devin", "opencode", "omp"}

    class FakeHerdr:
        bin = __file__                     # 假装 herdr 存在

        def kinds_supported(self):
            return fixed_supported

        def agent_installed(self, kind):
            return kind in fixed_supported

    h = FakeHerdr()

    def run(kind):
        try:
            RoleSpec("x", kind, "r", "").preflight_env(h, fixed_supported)
            return "通过"
        except RuntimeError as exc:
            return str(exc)

    check("herdr 不认的 kind → 被拒", "herdr 不支持" in run("mcode"), True)
    check("压根不存在的 agent → 被拒",
         "herdr 不支持" in run("nonexistent-xyz"), True)
    check("在支持列表里且已安装 → 通过", run("devin"), "通过")
    check("preflight_env 只在 RoleSpec 上（Herdr 上的那份是死代码）",
          hasattr(Herdr, "preflight_env"), False)



def test_fake_working_is_detected() -> None:
    print("\n[28] 假 working：声称在干活但 seq 不推进，必须能判出来")
    # 实测踩过：PM 卡了 9 分钟，herdr 一直报 working、state_change_seq 死锁在
    # 同一个值。原判据只在 status != working 时才计时，这种卡死永远不告警——
    # 整条防停摆链对它完全失效。
    tmp = Path(tempfile.mkdtemp())
    try:
        tr = IdleTracker(tmp / "idle.state")
        check("working 且 seq 在动 → 停滞 0s", tr.observe("pm", "working", 100), 0)
        tr2 = IdleTracker(tmp / "idle.state")
        idle = tr2.observe("pm", "working", 100)   # 同一 seq
        check("working 但 seq 不变 → 累计停滞时长", idle >= 0, True)
        check("刚 observe 完不算假 working（未到阈值）",
              tr2.is_fake_working("pm"), False)

        # seq 一动就恢复
        check("seq 变化 → 停滞归零", tr2.observe("pm", "working", 101), 0)
        check("恢复后不再是假 working", tr2.is_fake_working("pm"), False)

        # 非 working 走原来的真空闲计时
        check("切到 idle → 按真空闲计时", tr2.observe("pm", "idle", 101) >= 0, True)
        check("切到 idle 后不算假 working", tr2.is_fake_working("pm"), False)

        # 时间真的过去了 → 判为假 working（改 working_since 模拟 15 分钟前）
        rec = json.loads((tmp / "idle.state").read_text(encoding="utf-8"))
        rec["pm"]["status"] = "working"
        rec["pm"]["working_since"] = int(time.time()) - STUCK_WORKING_SECS - 5
        (tmp / "idle.state").write_text(json.dumps(rec), encoding="utf-8")
        check("停滞超阈值判为假 working",
              IdleTracker(tmp / "idle.state").is_fake_working("pm"), True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_alert_ladder_exhaustion_must_escalate() -> None:
    print("\n[29] 告警阶梯用完不等于放弃——必须升级到人类")
    # 真实故障：apartment 的 dev 提了 blocked，obs/review 都当没看见，卡了几小时。
    # xteam 早期版本在催满两轮后直接 continue —— 全队唯一的解阻塞人退出，
    # 比「没有监控」更糟：有人以为它在管。
    # 复用模块级那个 _cli（它已经处理了 bin/ 与 lib/ 两种布局），
    # 别再写死 `parent.parent / "bin"` —— 装到 install.sh 的前缀下那是 lib/，
    # 写死的话装出来的测试直接 FileNotFoundError。
    cli = _cli
    check("存在升级给人类的函数", hasattr(cli, "escalate_to_human"), True)
    check("升级间隔有常量", hasattr(cli, "HUMAN_ESCALATE_REPEAT_SECS"), True)

    # 阶梯用完的分支里必须有升级调用，不能是裸 continue
    src = _find_cli().read_text(encoding="utf-8")
    i = src.index("level >= 2")
    branch = src[i:i + 700]
    check("阶梯用完后调用了 escalate_to_human",
          "escalate_to_human" in branch, True)
    check("升级不是一次性的（会周期重提醒）",
          "escalated_at" in branch, True)


def test_index_summarizes_all_tasks() -> None:
    print("\n[30] 结构化索引：单文件汇总，不用挨个翻 task 目录")
    b = Bench()
    try:
        b.advance_to("request")
        idx = b.proto.index()
        check("counts.total 正确", idx["counts"]["total"], 1)
        check("counts.open 正确", idx["counts"]["open"], 1)
        check("stage 判为 implement（dev 在写）", idx["tasks"][0]["stage"], "implement")
        check("owes 汇总带 task 名", idx["owes"]["dev"], ["a:implement"])
        check("含 spec 轮次", idx["tasks"][0]["spec_round"], 1)

        # 走完一轮，stage 应随之推进
        # 队列还有活 → 闭合后 PM 该取下一项（next-slice），而不是写结项报告。
        # report 只在「队列空 + 有闭合切片 + 无 REPORT.md」时才欠，所以这里
        # 必须先把 QUEUE.md 铺上，否则测的是另一条路径。
        (b.proto.dir / "QUEUE.md").write_text(
            "| # | 切片 | 状态 | 备注 |\n|---|---|---|---|\n"
            "| 1 | a | done | |\n| 2 | b | todo | |\n", encoding="utf-8")
        b.advance_to("closed")
        idx = b.proto.index()
        check("闭合后 stage=closed", idx["tasks"][0]["stage"], "closed")
        check("闭合后 open 归零", idx["counts"]["open"], 0)
        # 闭合后 PM 仍欠 next-slice（该开下一项了）——这是设计意图，不是残留
        # F-20：标签指向队列第一个未开工项 b；闭合片名 a 永不进标签（证据 B）。
        check("闭合后 PM 欠 next-slice（F-20：标签指下一项 b，非闭合片 a）",
              idx["owes"].get("pm"), ["b:next-slice"])
        check("闭合后 tl/dev 无欠账",
              [r for r in idx["owes"] if r != "pm"], [])

        p = b.proto.write_index()
        check("index.json 落盘", p.exists(), True)
        check("可重新读回", "tasks" in json.loads(p.read_text(encoding="utf-8")), True)
    finally:
        b.cleanup()


def test_wiki_records_and_searches() -> None:
    print("\n[31] wiki：决策/教训累积沉淀并可检索")
    tmp = Path(tempfile.mkdtemp())
    try:
        p = Protocol(tmp)
        p.ensure()
        wiki_append(tmp, "lessons", "判据要机械可判定",
                    "TL 第一轮提的异议都是这条。", by="pm")
        wiki_append(tmp, "decisions", "结算不含未建账单",
                    "一期没建账单，结算口径收窄。", by="pm")
        check("两个文件", len(wiki_files(tmp)), 2)

        hits = wiki_search(tmp, "判据")
        check("按内容命中", len(hits) >= 1, True)
        check("命中带章节", bool(hits[0]["section"]), True)
        check("按标题也能命中", len(wiki_search(tmp, "结算")) >= 1, True)
        check("查不到返回空", wiki_search(tmp, "不存在的词"), [])
        check("空查询返回空", wiki_search(tmp, "  "), [])

        # 追加而非覆盖：同 kind 同月再记一条，两条都在同一文件
        check("同 kind 同月写同一文件", len(wiki_files(tmp)), 2)  # lessons + decisions
        wiki_append(tmp, "lessons", "第二条教训", "内容二。", by="tl")
        lf = [f for f in wiki_files(tmp) if f.name.startswith("lessons")][0]
        text = lf.read_text(encoding="utf-8")
        check("同文件追加不覆盖",
              ("判据要机械可判定" in text and "第二条教训" in text), True)
        check("三次记录后仍只有 2 个文件", len(wiki_files(tmp)), 2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_project_docs_detected_but_untouched() -> None:
    print("\n[32] 项目 docs/ 探测：有则以它为准，xteam 只读不改")
    import time as _t
    tmp = Path(tempfile.mkdtemp())
    try:
        p = Protocol(tmp)
        p.ensure()
        check("无 docs/ 时 has_project_docs=False", p.project_docs()["has_project_docs"], False)

        (tmp / "docs" / "adr").mkdir(parents=True)
        adr = tmp / "docs" / "adr" / "001-tech.md"
        adr.write_text("# ADR-1\n用 Go\n", encoding="utf-8")
        before = adr.stat().st_mtime

        d = p.project_docs()
        check("探测到 docs", d["has_project_docs"], True)
        check("定位到 adr 文件", d["locations"].get("adr"), ["001-tech.md"])
        check("策略是引用而非另立", "只读不改" in d["policy"], True)
        check("探测没有改动项目文件", adr.stat().st_mtime, before)
        check("探测没有新建文件", sorted(x.name for x in (tmp/"docs").iterdir()), ["adr"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_openrouter_is_warning_not_block() -> None:
    print("\n[33] openrouter 只提示不拦（用户 2026-10-02：可以像 omp 那样自己选）")
    # 之前是硬拦，导致「想用 openrouter 的某个模型」根本没法配。
    # 现在：提示 + 标记，但放行 —— 拦死等于替用户做决定。
    for kind, model in (("omp", "opus"), ("omp", "openrouter/x"),
                        ("devin", "swe-2-max"), ("cursor", "gpt-5")):
        try:
            RoleSpec("pm", kind, "产品经理", model).preflight()
            check(f"{kind}/{model} 放行", True, True)
        except RuntimeError as exc:
            check(f"{kind}/{model} 放行", f"被拦: {exc}", True)


def test_model_listing_marks_openrouter() -> None:
    print("\n[34] 模型查询：能搜、能标 openrouter，默认不隐藏")
    # **不断言本机有什么模型。** 之前断言「opencode 有 >100 个模型」且「存在
    # openrouter 条目」——那是把本机环境当断言，换台机器/换个账号就挂。
    # 这里只验行为：标记、过滤、搜索的结构。
    fake = [
        {"model": "openai/gpt-5", "provider": "openai", "blocked": False},
        {"model": "openrouter/meta/muse-spark-1.3", "provider": "openrouter",
         "blocked": True},
        {"model": "opencode-go/glm-5.3", "provider": "opencode-go", "blocked": False},
    ]
    check("openrouter 条目被标记",
          next(m["blocked"] for m in fake if "openrouter" in m["model"]), True)
    check("直连条目不被标记",
          next(m["blocked"] for m in fake if m["provider"] == "openai"), False)
    check("默认不过滤（用户自己挑）", sum(1 for m in fake if m["blocked"]), 1)

    # 真机路径只验「不抛异常、返回 list」，无 opencode 也不失败
    ms = list_models("opencode")
    check("list_models 返回 list", isinstance(ms, list), True)
    if ms:
        check("条目结构一致",
              all({"model", "provider", "blocked"} <= set(m) for m in ms[:5]), True)
    else:
        print("     （本机无 opencode，跳过内容断言——这正是解耦后的表现）")


def test_multi_project_registry_and_gating() -> None:
    print("\n[36] 多项目：注册表自动发现 + 切片必须声明项目")
    root = Path(tempfile.mkdtemp())
    try:
        for n in ("frontend", "api", "admin"):
            (root / n).mkdir()
            (root / n / ".git").mkdir()      # 自动发现靠它识别「这是个仓库」
        reg = Registry(root)
        check("无注册表时按 git 仓库自动发现", sorted(reg.names()),
              ["admin", "api", "frontend"])
        reg.save({"projects": [
            {"name": "frontend", "kind": "app", "consumers": []},
            {"name": "api", "kind": "shared-api", "consumers": ["frontend", "admin"]},
            {"name": "admin", "kind": "app", "consumers": []}]})
        check("api 识别为共享契约", reg.is_shared("api"), True)
        check("frontend 不是共享契约", reg.is_shared("frontend"), False)
        check("能取到 api 的下游", sorted(reg.consumers_of("api")),
              ["admin", "frontend"])
        check("下游影响面排除自身", sorted(reg.downstream_impact(["api"])),
              ["admin", "frontend"])
        check("同时动了下游则不重复报",
              reg.downstream_impact(["api", "frontend"]), ["admin"])
        check("普通项目无下游", reg.downstream_impact(["frontend"]), [])
        check("越界路径被拒", reg.resolve_dir("../etc"), None)
        check("不存在的项目目录", reg.resolve_dir("nope"), None)

        proto = Protocol(root); proto.ensure()
        t = proto.tasks / "x"; t.mkdir(parents=True)
        (t / "spec.md").write_text("# s", encoding="utf-8")
        (t / "spec.json").write_text('{"round":1}', encoding="utf-8")
        check("未声明项目时卡在 name-project",
              proto.debts(t), {"pm": "name-project"})
        (t / "task.json").write_text('{"project":"api"}', encoding="utf-8")
        check("声明后推进到下一阶段", proto.debts(t), {"tl": "assess"})
        check("by_project 能按项目分组", proto.by_project(), {"api": ["x"]})
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_project_declared_in_spec_front_matter() -> None:
    print("\n[37] 项目也可写在 spec.md 头部（免去额外文件）")
    root = Path(tempfile.mkdtemp())
    try:
        (root / "api").mkdir()
        reg = Registry(root)
        reg.save({"projects": [{"name": "api", "kind": "shared-api",
                               "consumers": ["frontend"]}]})
        t = root / ".xteam" / "tasks" / "y"; t.mkdir(parents=True)
        (t / "spec.md").write_text("---\nproject: api\n---\n# 需求\n",
                                   encoding="utf-8")
        check("front-matter 能解析出项目", reg.project_of_task(t), "api")
        p2 = root / ".xteam" / "tasks" / "z"; p2.mkdir(parents=True)
        (p2 / "spec.md").write_text("# 没有声明\n", encoding="utf-8")
        check("没声明时返回空", reg.project_of_task(p2), "")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_repo_kind_declaration() -> None:
    print("\n[38] 仓库结构声明：init 落盘后所有行为都读它，不靠猜")
    root = Path(tempfile.mkdtemp())
    try:
        for n in ("frontend", "api", "admin"):
            (root / n).mkdir()
            (root / n / ".git").mkdir()     # 自动发现靠它识别「这是个仓库」
        reg = Registry(root)
        check("无声明时按项目数推断 multi-app", reg.repo_kind(), "multi-app")
        check("能一句话描述结构", "多项目" in reg.describe_structure(), True)
        check("is_multi 为真", reg.is_multi(), True)

        reg.save({"kind": "multi-api", "projects": [
            {"name": "frontend", "kind": "app", "consumers": []},
            {"name": "api", "kind": "shared-api",
             "consumers": ["frontend", "admin"]},
            {"name": "admin", "kind": "app", "consumers": []}]})
        check("声明优先于推断", reg.repo_kind(), "multi-api")
        check("描述里带出共享契约层",
              "共享契约层：api" in reg.describe_structure(), True)

        reg.save({"kind": "single", "projects": [
            {"name": "myapp", "kind": "app", "consumers": []}]})
        check("单应用仓库", reg.repo_kind(), "single")
        check("单应用 is_multi=False", reg.is_multi(), False)
        check("单应用描述", "单应用仓库" in reg.describe_structure(), True)

        # 声明为 single 时，多项目门禁不该生效（单项目不添摩擦）
        proto = Protocol(root); proto.ensure()
        t = proto.tasks / "s"; t.mkdir(parents=True)
        (t / "spec.md").write_text("# s", encoding="utf-8")
        (t / "spec.json").write_text('{"round":1}', encoding="utf-8")
        check("单应用下不强制声明项目", proto.debts(t), {"tl": "assess"})
    finally:
        shutil.rmtree(root, ignore_errors=True)

def test_swap_handover_and_config_persistence() -> None:
    print("\n[39] 换 agent：交接底稿要够 + 新配置要落盘")
    root = Path(tempfile.mkdtemp())
    try:
        proto = Protocol(root); proto.ensure()
        # 换 agent 会丢掉旧 pane 的对话上下文，交接底稿漏一项 = 新 agent 瞎干
        mem = proto.dir / "memory"; mem.mkdir(parents=True, exist_ok=True)
        (mem / "pm-recap.md").write_text("已确认导出走流式，别一次性读全表",
                                        encoding="utf-8")
        (mem / "tl-recap.md").write_text("已拆成 3 个切片，第 2 个待派",
                                         encoding="utf-8")
        brief = _cli._handover_brief(root, proto, "pm")
        check("带上自己的 recap", "流式" in brief, True)
        check("带上别人的 recap 尾巴", "拆成 3 个切片" in brief, True)

        # 没有 recap 时必须明说，别让新 agent 凭空假设进度
        brief2 = _cli._handover_brief(root, proto, "dev")
        check("没有 recap 时明说", "没有留下 recap" in brief2, True)

        # 协议现状：谁欠什么义务
        t = proto.tasks / "s"; t.mkdir(parents=True)
        (t / "spec.md").write_text("# s", encoding="utf-8")
        (t / "spec.json").write_text('{"round":1}', encoding="utf-8")
        brief3 = _cli._handover_brief(root, proto, "dev")
        check("交接带上未闭合切片", "s" in brief3, True)

        # 换过的配置要落盘，且不能冲掉别人的配置
        _cli._persist_role(root, "pm", RoleSpec("pm", "devin", "产品经理",
                                                 "swe-2-max"))
        _cli._persist_role(root, "tl", RoleSpec("tl", "opencode", "团队负责人", ""))
        team = json.loads((proto.dir / "team.json").read_text(encoding="utf-8"))
        check("pm 的新 agent 落盘", team["roles"]["pm"],
              {"kind": "devin", "model": "swe-2-max"})
        check("没配 model 时不写空串", "model" in team["roles"]["tl"], False)
        check("两人的配置都在", sorted(team["roles"]), ["pm", "tl"])

        # 默认模型只影响启动参数，不写进配置
        check("devin 无 model 时补默认启动模型",
              _cli._with_default_model(RoleSpec("dev", "devin", "开发", "")).model,
              "swe-2-max")
        check("opencode 无默认模型则留空",
              _cli._with_default_model(RoleSpec("pm", "opencode", "PM", "")).model, "")
        check("显式 model 不被覆盖",
              _cli._with_default_model(RoleSpec("dev", "devin", "开发", "x")).model,
              "x")
        check("load_roles 读回新配置",
              load_roles(root)["pm"].describe(), "devin swe-2-max")
        # 找 tab：**up 时 start_agent 会把 tab 改名成 role-<scope>**，
        # 只按 label == role 匹配永远找不到（实测踩过：swap 报「找不到 tl 的 tab」）。
        class FakeHerdr:
            def __init__(self, tabs): self.tabs = tabs
            def _run(self, *a, **k): return {"tabs": self.tabs}
        real = FakeHerdr([{"tab_id": "w:t1", "label": "1"},
                          {"tab_id": "w:t2", "label": "pm-proj"},
                          {"tab_id": "w:t3", "label": "tl-proj"}])
        check("按 role- 前缀找到真实 tab", _cli._find_role_tab(real, "w", "tl"), "w:t3")
        check("不会张冠李戴认成别的角色",
              _cli._find_role_tab(real, "w", "pm"), "w:t2")
        check("session.json 记的 tab_id 优先",
              _cli._find_role_tab(real, "w", "tl", "w:t9"), "w:t9")
        check("角色不在时返回空", _cli._find_role_tab(real, "w", "dev"), "")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_swap_guard_blocks_while_working() -> None:
    print("\n[40] 换 agent 的安全阀：working 时不换（会丢未完成的上下文）")
    # 调的是 cmd_swap 真正用的那个判定，不是复刻一份 —— 复刻等于什么都没测。
    g = _cli._swap_block_reason
    check("working 被拦", g("working", False) != "", True)
    check("拦截理由说清代价", "正在做的上下文" in g("working", False), True)
    check("--force 放行", g("working", True), "")
    check("idle 直接放行", g("idle", False), "")
    # done 和 idle 语义相同（herdr：都表示可以接受输入，区别只是看没看过）。
    # 漏掉它 = 刚投完 intro 的 agent 会被自己的安全阀挡住，而它根本没在干活。
    check("done 放行（= idle，只是没被看到过）", g("done", False), "")
    check("blocked 放行（它已停下，没什么可丢）", g("blocked", False), "")
    check("状态未知放行（不因读不到就挡住人）", g("unknown", False), "")


def test_cli_search_entry_does_not_crash() -> None:
    import argparse
    import contextlib
    import io

    print("\n[41] CLI 入口 cmd_search：空 wiki 不崩且目录文案正确")
    # 这条用例直接调 CLI 入口而不是库层 —— 本切片的根因就是
    # 「库里有、入口没导」在 cmd_search 炸出 NameError，而入口零覆盖。
    def run(root: Path, query: str) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _cli.cmd_search(argparse.Namespace(
                project=str(root), query=query, limit=20))
        return rc, buf.getvalue()

    # A：wiki 目录不存在 —— 不崩、返回 0，且明说「目录还不存在」
    tmp = Path(tempfile.mkdtemp())
    try:
        rc, out = run(tmp, "结算")
        check("cmd_search 空 wiki 目录不抛异常", rc, 0)
        check("目录不存在时明说", "wiki 目录还不存在" in out, True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # B：目录存在但为空 —— 不崩，且不得误报「目录还不存在」。
    # wiki_files 对「目录不存在」和「目录存在但为空」都返回 []，
    # 所以判据必须是目录存在性（wiki_dir(root).is_dir()），不是搜索结果。
    tmp = Path(tempfile.mkdtemp())
    try:
        (tmp / ".xteam" / "wiki").mkdir(parents=True)
        rc, out = run(tmp, "不存在的词")
        check("空 wiki 目录同样返回 0", rc, 0)
        check("空目录不误报目录不存在", "wiki 目录还不存在" in out, False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_qa_config_and_gate_trigger() -> None:
    print("\n[41] 第三方评审：配置可固化 + 只在门禁前跑一次")
    root = Path(tempfile.mkdtemp())
    try:
        for n in ("frontend", "api"):
            (root / n).mkdir()
            (root / n / ".git").mkdir()
        proto = Protocol(root); proto.ensure()
        reg = Registry(root)
        reg.save({"kind": "multi-api", "projects": [
            {"name": "frontend", "kind": "app", "consumers": []},
            {"name": "api", "kind": "shared-api", "consumers": ["frontend"]}]})

        qc = QaConfig(root)
        check("默认不落盘", qc.configured(), False)
        cfg = qc.load()
        check("默认手动触发（不拖慢每轮门禁）", cfg["when"], "manual")
        check("默认用已实测的 agent", cfg["agent"], "qodercli")

        # 坏配置不能让整个 qa 崩掉 —— 但**白名单外的 agent 必须硬拒**，
        # 不能像以前那样「原样返回」然后拿去做 subprocess.run：
        # 配置里写 /bin/curl 就能在巡检（when=always 自动触发）里执行任意程序。
        qc.path.write_text('{"when": "瞎写", "agent": 123}', encoding="utf-8")
        try:
            qc.load()
            check("白名单外的 agent 被拒", False, True)
        except QaAgentRejected as exc:
            check("非字符串 agent 被拒", "123" in str(exc), True)
        # when 非法仍然回落默认（它只是时机，不涉及执行谁）
        qc.path.write_text('{"when": "瞎写", "agent": "qodercli"}', encoding="utf-8")
        bad = qc.load()
        check("非法 when 回落到默认", bad["when"], "manual")

        qc.save({"agent": "qodercli", "when": "risky", "model": "",
                 "risky_projects": ["frontend"], "prompt": "", "timeout": 60})
        check("配置落盘后读回一致", qc.load()["when"], "risky")

        def task_for(project: str) -> Path:
            t = proto.tasks / f"t-{project}"
            t.mkdir(parents=True, exist_ok=True)
            (t / "task.json").write_text(json.dumps({"project": project}),
                                         encoding="utf-8")
            return t

        # manual：一律不自动跑
        qc.save({**qc.load(), "when": "manual"})
        want, why = qa_should_run(root, task_for("api"))
        check("manual 时不自动跑", want, False)
        check("并说清为什么不跑", "manual" in why, True)

        # risky：共享契约项目自动算高风险（先清掉显式名单，单独验证这一条规则）
        qc.save({**qc.load(), "when": "risky", "risky_projects": []})
        want, why = qa_should_run(root, task_for("api"))
        check("共享契约项目自动触发", want, True)
        check("理由指向下游影响", "共享契约" in why, True)
        want, _ = qa_should_run(root, task_for("frontend"))
        check("普通项目不触发", want, False)

        # risky：显式列进 risky_projects 的也算
        qc.save({**qc.load(), "risky_projects": ["frontend"]})
        want, why = qa_should_run(root, task_for("frontend"))
        check("显式高风险项目触发", want, True)
        check("理由说明是配置指定的", "高风险" in why, True)

        qc.save({**qc.load(), "when": "always"})
        check("always 一律触发", qa_should_run(root, task_for("frontend"))[0], True)

        # 结论摘要：落盘后能读出来，HTML 注释头不能混进正文
        t = task_for("api")
        review_path(t).write_text(
            "<!-- 第三方评审 · qodercli · 2026-01-01 -->\n\n## 问题\n\n发现 3 处\n",
            encoding="utf-8")
        d = review_digest(t)
        check("摘要含结论", "发现 3 处" in d, True)
        check("摘要不含注释头", "<!--" not in d, True)
        check("没跑过就没有摘要", review_digest(proto.tasks / "t-frontend"), "")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_status_queue_count_matches_queue_items() -> None:
    print("\n[42] status 队列计数与 queue_items 一致（两套口径统一）")
    # 根因：cmd_status 另写了一套裸字符串解析（把所有 | 行都算队列行、
    # 用 "`done`" not in ln 判完成），与 queue_items() 的单元格解析漂移。
    # 修法：计数抽成纯 helper Protocol.queue_counts()，cmd_status 复用它。
    # 本用例断言 helper 覆盖全部分歧类；接线由 C1–C4 的真实 CLI 覆盖。
    tmp = Path(tempfile.mkdtemp())
    try:
        p = Protocol(tmp)
        p.ensure()
        # 混合队列：裸 done / `done` / 已闭合 / todo / 非数字序号
        (p.dir / "QUEUE.md").write_text(
            "# 队列\n\n"
            "| 序 | 切片 | 状态 | 备注 |\n|---|---|---|---|\n"
            "| 1 | a | todo | x |\n"
            "| 2 | b | done | x |\n"
            "| 3 | c | `done` | x |\n"
            "| 4 | d | 已闭合 | x |\n"
            "| — | e | done | y |\n",
            encoding="utf-8")
        # 非数字序号行被 queue_items() 跳过 → 只剩 4 行；a 未完成
        pending, total = p.queue_counts()
        check("混合队列：未完成数", pending, 1)
        check("混合队列：总行数（非数字序号行跳过）", total, 4)
        check("与 queue_items 同源",
              (pending, total),
              (len([i for i in p.queue_items()
                    if i["state"] not in ("done", "已闭合")]),
               len(p.queue_items())))

        # 备注里出现反引号 done 字样，不得被算成整行完成（旧口径的漂移点）
        (p.dir / "QUEUE.md").write_text(
            "# 队列\n\n"
            "| 序 | 切片 | 状态 | 备注 |\n|---|---|---|---|\n"
            "| 1 | x | todo | 备注里提到 `done` 与 已闭合 |\n",
            encoding="utf-8")
        pending, total = p.queue_counts()
        check("备注含 done 字样不影响状态判定", (pending, total), (1, 1))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_blocked_answer_failure_escalates_not_answered() -> None:
    print("\n[43] blocked 解析失败必须升级，不得静默置 answered")
    # 真实故障：TL 停在按钮式权限 UI 上，_parse_options 解析不出 →
    # _answer_choice 返回失败文案，但旧代码无条件 set_alerted("answered")，
    # 于是守卫永久为假、巡检不再重试也不升级人类 → 静默停放 16 分钟没人知道。
    outcome = _cli.blocked_answer_outcome

    # 失败文案（解析不出选项）→ 必须升级、不得算已答
    r = outcome("未识别出选项（可能不是选择题），保留现场等 PM 处理")
    check("解析失败不算已答（必须升级）", r["mark_answered"], False)
    check("解析失败需要升级", r["needs_escalation"], True)

    # 其它失败：无 workspace / 找不到 pane / 读 pane 失败 / 投递失败
    for fail_msg in ("无 workspace，未作答", "找不到 dev 的 pane",
                     "读 pane 失败（TimeoutError），未作答"):
        r = outcome(fail_msg)
        check(f"失败文案「{fail_msg[:12]}…」判为失败+升级",
              (r["mark_answered"], r["needs_escalation"]), (False, True))

    # 边界用例（本切片要防的 bug）：投递失败的文案也以「选了」开头，
    # 却根本没送出去 —— 必须判为失败而非已答
    r = outcome("选了「Reject」但投递失败：timeout")
    check("投递失败（同以『选了』开头）不算已答",
          (r["mark_answered"], r["needs_escalation"]), (False, True))

    # 成功代答 → 算已答、不升级（不能改坏既有成功路径）
    r = outcome("选了「继续」→ 已提交，agent 恢复 working")
    check("成功代答算已答", r["mark_answered"], True)
    check("成功代答不升级（不骚扰人类）", r["needs_escalation"], False)


def test_probe_result_actually_changes_behavior() -> None:
    print("\n[42] probe 的结果必须真的改变判定，不能只改展示")
    # 回归：supports_tui_model 以前查静态 TUI_MODEL_OK，展示查 load_verified_agents()。
    # 两套来源的后果是「doctor --probe 显示 ✓，但 up --set-model 照样拒」——
    # 系统学到了却不用。这里把展示和判定钉在同一份来源上。
    import xteam_lib
    tmp = Path(tempfile.mkdtemp())
    real = xteam_lib.probe_store_path
    xteam_lib.probe_store_path = lambda: tmp / "verified.json"   # 隔离，不碰用户配置
    try:
        KIND = "opencode"        # 静态表里唯一被实测否定的（TUI 顶层无 --model）
        check("probe 之前按静态表判定为不支持", tui_model_ok(KIND), False)
        spec = RoleSpec("pm", KIND, "PM", "grok-code")
        check("probe 之前不会传 --model", spec.agent_args(), [])
        before = agent_catalog({KIND})
        check("probe 之前展示为不支持", "✗ 无 --model" in before, True)

        save_verified_agent(KIND)          # 模拟 doctor --probe 通过
        check("probe 之后判定跟着变", tui_model_ok(KIND), True)
        check("probe 之后真的传 --model", spec.agent_args(), ["--model", "grok-code"])
        check("is_verified 也跟着变", spec.is_verified(), True)
        after = agent_catalog({KIND})
        check("展示与判定一致（不再自相矛盾）",
              "✓ --model（实测）" in after and "✗ 无 --model" not in after, True)

        # probe 文件损坏不能让能力判定崩
        (tmp / "verified.json").write_text("{ 不是 json", encoding="utf-8")
        check("probe 文件损坏时退回内置实测集", tui_model_ok("devin"), True)
        check("probe 文件损坏不影响已知否定项", tui_model_ok(KIND), False)
    finally:
        xteam_lib.probe_store_path = real
        shutil.rmtree(tmp, ignore_errors=True)


def test_cross_project_dependencies_and_contracts() -> None:
    print("\n[43] 跨项目切片排序 + API 契约留痕")
    root = Path(tempfile.mkdtemp())
    try:
        for n in ("frontend", "api"):
            (root / n).mkdir()
            (root / n / ".git").mkdir()
        reg = Registry(root)
        reg.save({"kind": "multi-api", "projects": [
            {"name": "frontend", "kind": "app", "consumers": []},
            {"name": "api", "kind": "shared-api", "consumers": ["frontend"]}]})
        proto = Protocol(root); proto.ensure()

        def slice_(name: str, meta: dict) -> Path:
            t = proto.tasks / name
            t.mkdir(parents=True, exist_ok=True)
            (t / "task.json").write_text(json.dumps(meta), encoding="utf-8")
            (t / "spec.md").write_text("# s", encoding="utf-8")
            (t / "spec.json").write_text('{"round":1}', encoding="utf-8")
            return t

        cd = contracts_dir(root); cd.mkdir(parents=True, exist_ok=True)
        (cd / "POST _orders.md").write_text(
            CONTRACT_TEMPLATE.format(method="POST", path="/orders"), encoding="utf-8")
        api = slice_("api-side", {"project": "api", "contracts": ["POST _orders.md"]})
        fe = slice_("frontend-side", {"project": "frontend",
                                     "depends_on": ["api-side"]})
        check("依赖解析", reg.depends_on_task(fe), ["api-side"])
        check("前置没闭合 → 等前置而不是卡住",
              proto.debts(fe), {"tl": "wait-dep"})
        check("等的是哪个", proto.deps_blocking(fe), ["api-side"])
        check("前置切片本身照常推进", proto.debts(api)["tl"], "assess")
        check("现在能派的只有前置",
              [t.name for t in proto.dispatchable()], ["api-side"])

        (api / "closed.md").write_text("# 闭合", encoding="utf-8")
        check("前置闭合后解锁", proto.debts(fe)["tl"], "assess")
        # 已闭合的切片不算「可派」——它已经做完了，再派一遍才是 bug
        check("闭合后后继解锁，而已闭合的前置不再出现在可派列表",
              [t.name for t in proto.dispatchable()], ["frontend-side"])

        # 契约：共享契约项目声明了却没有文件 → 拦住
        # 前面为了测「前置闭合后解锁」把 api-side 标了闭合，这里解除 ——
        # 已闭合的切片不再有任何义务，拿它测契约门禁会永远得到空账。
        (api / "closed.md").unlink()
        (cd / "POST _orders.md").unlink()
        check("契约文件缺失时拦住", proto.debts(api), {"pm": "contracts-missing"})
        check("列出缺了哪个", proto.contracts_missing(api), ["POST _orders.md"])
        (cd / "POST _orders.md").write_text(
            CONTRACT_TEMPLATE.format(method="POST", path="/orders"), encoding="utf-8")
        check("建了契约就放行", proto.debts(api)["tl"], "assess")
        check("不再报缺", proto.contracts_missing(api), [])

        # 依赖环必须能查出来，否则永远排不上队还查不出原因
        slice_("x", {"depends_on": ["y"]})
        slice_("y", {"depends_on": ["x"]})
        check("依赖成环能查出来", len(proto.dep_cycles()), 1)
        # 拼错的前置名 ≠ 排队，要能区分
        slice_("z", {"depends_on": ["根本没这个切片"]})
        check("拼错的前置被单独识别",
              proto.missing_deps(proto.tasks / "z"), ["根本没这个切片"])

        # 变更留痕：改契约必须留下可查的记录
        before = (cd / "POST _orders.md").read_text(encoding="utf-8")
        contract_log_change(root, "POST _orders.md", "pm", "total 改为字符串")
        after = (cd / "POST _orders.md").read_text(encoding="utf-8")
        check("变更记录被追加", "total 改为字符串" in after, True)
        check("原内容没被冲掉", before.split("## 变更记录")[0] in after, True)
        tl = (root / ".xteam" / "watch" / "timeline.log").read_text(encoding="utf-8")
        check("时间线也留了痕", "POST _orders.md" in tl, True)

        # 文件名规范
        check("METHOD /path → 文件名",
              contract_filename("post", "/orders/{id}"), "POST _orders_{id}.md")
        check("文件名能还原",
              parse_contract_name("POST _orders_{id}.md"), ("POST", "/orders/{id}"))
        check("不合规名被拒", parse_contract_name("随便写.md"), ("", ""))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_contracts_not_required_for_plain_projects() -> None:
    print("\n[44] 普通项目不该被契约要求绑住")
    root = Path(tempfile.mkdtemp())
    try:
        (root / "app").mkdir()
        (root / "app" / ".git").mkdir()
        reg = Registry(root)
        reg.save({"kind": "single", "projects": [
            {"name": "app", "kind": "app", "consumers": []}]})
        proto = Protocol(root); proto.ensure()
        t = proto.tasks / "z"; t.mkdir()
        # 故意声明一个不存在的契约文件
        (t / "task.json").write_text(
            json.dumps({"project": "app", "contracts": ["GET _y.md"]}),
            encoding="utf-8")
        (t / "spec.md").write_text("# s", encoding="utf-8")
        (t / "spec.json").write_text('{"round":1}', encoding="utf-8")
        check("普通项目：声明了也不拦", proto.debts(t), {"tl": "assess"})
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_dep_gate_holds_after_decomposition() -> None:
    print("\n[45] 依赖门禁必须对**所有**阶段生效，不能被 request.md 绕过")
    # 回归：门禁之前只在「还没有 request.md」时拦，于是 TL 一旦先拆了解释，
    # dev 就会照着一套还不存在的 API 去实现 —— 跨项目排序的核心保证被绕过。
    root = Path(tempfile.mkdtemp())
    try:
        for n in ("frontend", "api"):
            (root / n).mkdir()
            (root / n / ".git").mkdir()
        reg = Registry(root)
        reg.save({"kind": "multi-api", "projects": [
            {"name": "frontend", "kind": "app", "consumers": []},
            {"name": "api", "kind": "shared-api", "consumers": ["frontend"]}]})
        proto = Protocol(root); proto.ensure()

        api = proto.tasks / "api-side"; api.mkdir(parents=True)
        (api / "task.json").write_text('{"project":"api"}', encoding="utf-8")
        fe = proto.tasks / "frontend-side"; fe.mkdir(parents=True)
        (fe / "task.json").write_text(
            json.dumps({"project": "frontend", "depends_on": ["api-side"]}),
            encoding="utf-8")
        (fe / "spec.md").write_text("# s", encoding="utf-8")
        (fe / "spec.json").write_text('{"round":1}', encoding="utf-8")
        (fe / "request.md").write_text("# 已拆解", encoding="utf-8")   # 关键：已拆解

        check("已拆解 + 前置未闭合 → 仍然拦", proto.debts(fe), {"tl": "wait-dep"})
        check("dev 拿不到 implement", "dev" in proto.debts(fe), False)

        (api / "closed.md").write_text("# 闭合", encoding="utf-8")
        check("前置闭合后才真的能实现", proto.debts(fe).get("dev"), "implement")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_contract_boundary_and_failure_modes() -> None:
    print("\n[46] 契约的边界与失败语义")
    root = Path(tempfile.mkdtemp())
    try:
        (root / "victim.md").write_text("原始内容", encoding="utf-8")
        cd = contracts_dir(root); cd.mkdir(parents=True, exist_ok=True)

        # 路径穿越：名字来自命令行和 task.json，都是不可信输入
        for bad in ("../../victim.md", "..%2f..%2fvictim.md", "/etc/passwd",
                    "随手写的.md"):
            check(f"越界/不合规名被拒：{bad}",
                  contract_path(root, bad) is None, True)
        check("合法名可解析",
              (contract_path(root, "POST _orders.md") or Path("x")).name,
              "POST _orders.md")

        # 写不进就必须报失败，不能「报了成功其实没写」
        no_section = cd / "POST _x.md"
        no_section.write_text("# 没有变更记录小节", encoding="utf-8")
        check("无变更记录小节 → 明确失败", contract_log_change(root, "POST _x.md",
                                                              "pm", "改了"), "")
        tl = root / ".xteam" / "watch" / "timeline.log"
        check("失败时不得写时间线（否则审计自相矛盾）",
              tl.exists() and "POST _x.md" in tl.read_text(encoding="utf-8"),
              False)
        check("原文件没被动过", no_section.read_text(encoding="utf-8"),
              "# 没有变更记录小节")

        # 正常路径：写进去 + 时间线都留痕
        good = cd / "POST _y.md"
        good.write_text(CONTRACT_TEMPLATE.format(method="POST", path="/y"),
                        encoding="utf-8")
        check("正常路径成功", contract_log_change(root, "POST _y.md", "pm", "改了") != "",
              True)
        check("变更记录写进文件了",
              "改了" in good.read_text(encoding="utf-8"), True)
        check("时间线也留痕",
              "POST _y.md" in tl.read_text(encoding="utf-8"), True)

        # 越界名不能冒充「契约已存在」
        proto = Protocol(root)
        reg = Registry(root)
        reg.save({"kind": "multi-api", "projects": [
            {"name": "api", "kind": "shared-api", "consumers": ["frontend"]}]})
        t = proto.tasks / "x"; t.mkdir(parents=True)
        (t / "task.json").write_text(
            json.dumps({"project": "api", "contracts": ["../../victim.md"]}),
            encoding="utf-8")
        check("越界引用不算「契约已存在」", proto.contracts_missing(t),
              ["../../victim.md"])
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_slice_order_and_rules_staleness() -> None:
    print("\n[47] 拓扑排序 + 章程变更检测")
    root = Path(tempfile.mkdtemp())
    try:
        for n in ("a", "b"):
            (root / n).mkdir(); (root / n / ".git").mkdir()
        proto = Protocol(root); proto.ensure()

        def sl(name, deps=None):
            t = proto.tasks / name; t.mkdir(parents=True)
            (t / "task.json").write_text(
                json.dumps({"depends_on": deps} if deps else {}), encoding="utf-8")
            return t

        # 字母序是 alpha, mid, zeta；正确顺序必须把前置放前面
        sl("zeta"); sl("mid", ["zeta"]); sl("alpha", ["mid"])
        check("拓扑序把前置排在前", [t.name for t in proto.slice_order()],
              ["zeta", "mid", "alpha"])

        # 环不能让它崩，也不能让它死循环
        sl("x", ["y"]); sl("y", ["x"])
        order = [t.name for t in proto.slice_order()]
        check("含环仍能排完（不死循环、不抛错）", len(order), 5)
        check("环仍被单独检出", proto.dep_cycles(), [["x", "y", "x"]])

        # 章程变更检测
        rdir = Path(tempfile.mkdtemp())
        proto_dir = Path(tempfile.mkdtemp())
        (rdir / "pm.md").write_text("pm 章程", encoding="utf-8")
        (rdir / "tl.md").write_text("tl 章程", encoding="utf-8")
        pfile = proto_dir / "PROTOCOL.md"
        pfile.write_text("协议", encoding="utf-8")
        check("没记录时不报噪声（首次 up 前）",
              stale_rules(root, rdir, pfile), [])
        save_rules_state(root, rules_fingerprint(rdir, pfile))
        check("刚记录后无变更", stale_rules(root, rdir, pfile), [])
        (rdir / "pm.md").write_text("pm 章程改了", encoding="utf-8")
        check("精确到改动的那个章程", stale_rules(root, rdir, pfile), ["pm"])
        (rdir / "tl.md").write_text("tl 章程也改了", encoding="utf-8")
        check("多个改动都报出来",
              sorted(stale_rules(root, rdir, pfile)), ["pm", "tl"])
        pfile.write_text("协议改了", encoding="utf-8")
        check("协议变更也算", "PROTOCOL" in stale_rules(root, rdir, pfile), True)
        check("状态已落盘", (root / ".xteam" / "rules.json").exists(), True)

        # ---- skills/（输出规范）也必须算进指纹
        # STE 输出规范住在 skills/ste/SKILL.md，PROTOCOL.md 里只有 8 条精简版。
        # 指纹不覆盖 skills/ 的话，「只改了输出规范」这种发版会让 stale 为空 →
        # sync 说「没变化」→ agent 继续按旧规范输出，而 status 看不出异常。
        sdir = Path(tempfile.mktemp())
        (sdir / "ste").mkdir(parents=True)
        sk = sdir / "ste" / "SKILL.md"
        sk.write_text("规范 v1", encoding="utf-8")
        # 干净基线：章程+协议+skills 一起记
        save_rules_state(root, rules_fingerprint(rdir, pfile, sdir))
        check("skills 干净时无变更",
              stale_rules(root, rdir, pfile, sdir), [])
        sk.write_text("规范 v2 —— 输出契约改了", encoding="utf-8")
        got = stale_rules(root, rdir, pfile, sdir)
        check("只改 skills/ 也算变更", got, ["skills/ste/SKILL.md"])
        check("skills 键被识别为「协议类」（影响所有人）",
              [is_protocol_drift(k) for k in got], [True])
        check("角色名不算协议类",
              [is_protocol_drift("pm"), is_protocol_drift("tl")], [False, False])
        shutil.rmtree(sdir, ignore_errors=True)

        # ---- 版本号 / CLI 面：回答「该不该更新」的那一半
        # 指纹是不透明哈希，答不了「这是哪版投的」。所以 rules.json 里另记版本与
        # 命令面：面板 0.1.5、章程是 0.1.8 写的，人一眼能看出该 sync 了。
        save_rules_state(root, {}, version="0.1.7",
                         cli=["say", "status", "watch"])
        meta = injected_meta(root)
        check("记下了 xteam 版本", meta.get("xteam_version"), "0.1.7")
        check("记下了 CLI 面", meta.get("cli"), ["say", "status", "watch"])
        check("新增子命令被算成 CLI 面变化",
              cli_drift(root, ["say", "status", "watch", "contracts"]),
              ["contracts"])
        check("没有新增就不算变化",
              cli_drift(root, ["say", "status", "watch"]), [])
        # 「没有基线」要用**另一个空项目**来验：fresh 项目没投过任何东西，
        # 此时整个命令面都算「新增」是没有意义的噪声 —— pane 还没起呢。
        fresh = Path(tempfile.mkdtemp())
        try:
            check("首次 up 之前没有基线 → 不算变化",
                  cli_drift(fresh, ["a", "b", "c"]), [])
        finally:
            shutil.rmtree(fresh, ignore_errors=True)

        # ---- 只改 PROTOCOL.md 时，sync 必须也能把它修好
        # 起因是一个真 bug：重投循环是 `if stale and role not in stale: continue`，
        # 而只改 PROTOCOL 时 stale={"PROTOCOL"}、三个角色都不在里面 → 一个都不重投
        # → sent=0 → save_rules_state 不执行 → 指纹永不更新 → **status 永久报警，
        # 而 sync 反复跑也修不掉**。
        # 这里验的是「sync 之后指纹必须前进」这个可观察后果 —— 比断言内部
        # 有没有调用某个函数更耐改。
        r2 = Path(tempfile.mkdtemp())
        p2 = Path(tempfile.mkdtemp()) / "PROTOCOL.md"
        p2.parent.mkdir(parents=True, exist_ok=True)
        try:
            (r2 / "pm.md").write_text("pm 章程", encoding="utf-8")
            p2.write_text("协议 v1", encoding="utf-8")
            save_rules_state(r2, rules_fingerprint(r2, p2))
            p2.write_text("协议 v2", encoding="utf-8")
            check("只改协议 → stale 里只有 PROTOCOL",
                  stale_rules(r2, r2, p2), ["PROTOCOL"])
            check("PROTOCOL 判为影响所有人",
                  is_protocol_drift("PROTOCOL"), True)
            # 模拟 sync 的核心：检测到变更就落状态（不论是否重投了谁）
            if stale_rules(r2, r2, p2):
                save_rules_state(r2, rules_fingerprint(r2, p2))
            check("sync 之后不再报警（指纹已前进）",
                  stale_rules(r2, r2, p2), [])
        finally:
            shutil.rmtree(r2, ignore_errors=True)
            shutil.rmtree(p2.parent, ignore_errors=True)

        # ---- 门狗提醒的冷却门
        # 用 observe() 凑冷却有个坑：它只在 seq 变化时重置 since，于是
        # 「先 clean 后 stale」这条路上第一次提醒会被压到下一个窗口之后 ——
        # 恰好是最该立刻喊的那次被吞掉。nag_gate 显式记「上次提醒时刻」。
        tr_root = Path(tempfile.mkdtemp())
        try:
            tk = IdleTracker(tr_root / "idle.state")
            check("第一次提醒放行", tk.nag_gate("nag:x", 1800), True)
            check("冷却内不再喊", tk.nag_gate("nag:x", 1800), False)
            check("冷却过了再喊（用 stamp 推进时间，不 sleep）",
                  tk.nag_gate("nag:x", 1800,
                              stamp=int(time.time()) + 1801), True)
            tk.clear_nag("nag:x")
            check("清掉冷却后立刻又能喊（clean→stale 那条路）",
                  tk.nag_gate("nag:x", 1800), True)
            check("nag 状态写在巡检自己的状态文件里",
                  (tr_root / "idle.state").exists(), True)
        finally:
            shutil.rmtree(tr_root, ignore_errors=True)

        shutil.rmtree(rdir, ignore_errors=True)
        shutil.rmtree(proto_dir, ignore_errors=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # ---- 上游版本比较
    # 两个具体的坑，都属于「不报错、只是永远不提示」那一类：
    #  1. tag 是 `xteam-v0.1.8`，而 lstrip("v") 对首字符是 x 的字符串无效
    #     → 版本号被当成非数字 → 恒为 0 → 「有新版」永远不显示
    #  2. 字符串比较会把 0.1.10 判成比 0.1.8 小（'1' < '8'）
    check("认得 xteam-v 前缀", _parse_version("xteam-v0.1.8"), (0, 1, 8))
    check("认得 v 前缀", _parse_version("v0.2.0"), (0, 2, 0))
    check("缺段补 0", _parse_version("1.2"), (1, 2, 0))
    check("0.1.10 比 0.1.8 新（不是字符串比较）",
          newer_than("0.1.8", "0.1.10"), True)
    check("拿 tag 原文也能比", newer_than("0.1.8", "xteam-v0.1.9"), True)
    check("本机更新时不报", newer_than("0.1.10", "xteam-v0.1.8"), False)
    check("版本缺失时不乱报", newer_than("", "0.1.9"), False)

    # ---- 真调 cmd_sync（不 inline 模拟）
    # 上面那条「sync 之后不再报警」是**在测试里重演了一遍 sync 的逻辑**，所以它是
    # 自证的：把 bin/xteam 改回原样，它照样通过。这里必须真跑 cmd_sync（桩掉
    # herdr），断言「通知到了谁」和「指纹有没有前进」。
    #
    # 验证过：把两处一起退回原样（continue 条件 + `if sent:`）→ 7 条断言失败。
    # 单退回其中一处反而抓不到 —— 因为「通知所有人」和「有变更就落状态」是
    # 冗余的两道防线，任一道单独就能让 status 不再报警。这是有意的：两道都留着。
    sync_root = Path(tempfile.mkdtemp())
    try:
        proto = Protocol(sync_root)
        proto.ensure()
        sent_log: list[tuple[str, str]] = []

        class _FakeHerdr:
            def __init__(self, scope: str = "") -> None:
                self.scope = scope

            def find_workspace(self, label: str):
                return {"workspace_id": "w1"}

            def role_map(self, ws_id: str):
                return {r: {"pane_id": f"p-{r}"} for r in ("pm", "tl", "dev")}

            def doorbell(self, pane, msg, wait_s=0, kind="", wait_empty_s=15.0):
                sent_log.append((str(pane), str(msg)))
                return "ok"

        orig_herdr = _cli.Herdr
        orig_require = _cli._require_herdr          # 见下
        orig_roledir, orig_tpl = _cli.ROLES_DIR, _cli.TEMPLATES
        orig_skills = _cli.SKILLS_DIR
        tmp_inst = Path(tempfile.mkdtemp())
        for r in ("pm", "tl", "dev"):
            (tmp_inst / "roles").mkdir(exist_ok=True)
            (tmp_inst / "roles" / f"{r}.md").write_text(f"# {r} 章程\n",
                                                       encoding="utf-8")
        (tmp_inst / "templates").mkdir(exist_ok=True)
        (tmp_inst / "templates" / "PROTOCOL.md").write_text("协议 v1\n",
                                                            encoding="utf-8")
        (tmp_inst / "skills" / "ste").mkdir(parents=True)
        (tmp_inst / "skills" / "ste" / "SKILL.md").write_text("规范 v1\n",
                                                              encoding="utf-8")
        _cli.Herdr = _FakeHerdr
        # **必须把 _require_herdr 也换掉。** cmd_sync 第一件事就是
        # `_require_herdr()`，它 `shutil.which("herdr")` 查不到就 `die()`。
        # 本机装了 herdr，所以这条测试在本地一直是过的；CI 上没有 herdr，
        # 于是它当场 SystemExit —— 而失败点在「基线是干净的」之后，
        # 与真正被测的 sync 逻辑毫无关系，看日志根本猜不到是这里。
        # 既然 Herdr 已经整个桩掉，再去要求真 herdr 就没有意义了。
        _cli._require_herdr = lambda: None
        _cli.ROLES_DIR = tmp_inst / "roles"
        _cli.TEMPLATES = tmp_inst / "templates"
        _cli.SKILLS_DIR = tmp_inst / "skills"
        try:
            # 先建立一个「刚投过、干净」的状态
            save_rules_state(sync_root, rules_fingerprint(
                _cli.ROLES_DIR, _cli.TEMPLATES / "PROTOCOL.md",
                _cli.SKILLS_DIR), version="0.1.8", cli=["say", "status"])
            check("基线是干净的",
                  stale_rules(sync_root, _cli.ROLES_DIR,
                              _cli.TEMPLATES / "PROTOCOL.md", _cli.SKILLS_DIR),
                  [])

            # **只改 PROTOCOL.md**，章程和 skills 都不动
            (_cli.TEMPLATES / "PROTOCOL.md").write_text("协议 v2\n",
                                                        encoding="utf-8")
            sent_log.clear()
            ns = argparse.Namespace(project=str(sync_root), workspace=None)
            _cli.cmd_sync(ns)
            check("只改协议：三个角色都被通知（不能让一个人都不重投）",
                  sorted(p for p, _m in sent_log), ["p-dev", "p-pm", "p-tl"])
            check("通知里指明了要重读哪个文件",
                  any(".xteam/PROTOCOL.md" in m for _p, m in sent_log), True)
            check("通知里说清章程本身没变（不用重读全文）",
                  any("章程本身没变" in m for _p, m in sent_log), True)
            # **关键**：指纹必须前进，否则 status 永久报警且 sync 修不掉
            check("sync 之后 status 不再报警",
                  stale_rules(sync_root, _cli.ROLES_DIR,
                              _cli.TEMPLATES / "PROTOCOL.md", _cli.SKILLS_DIR),
                  [])
            check("重跑一次不再打扰", _cli.cmd_sync(ns), 0)
            check("第二次没有重复通知", len(sent_log), 3)

            # **只改 skills/**：也算协议类变更，且提示给的是文件路径
            (_cli.SKILLS_DIR / "ste" / "SKILL.md").write_text("规范 v2\n",
                                                              encoding="utf-8")
            sent_log.clear()
            _cli.cmd_sync(ns)
            check("只改 skills/ 也会通知到人",
                  sorted(p for p, _m in sent_log), ["p-dev", "p-pm", "p-tl"])
            check("提示里给了 skills 的可读路径",
                  any("SKILL.md" in m for _p, m in sent_log), True)

            # **只改一个角色的章程**：只有那个角色重投全文，其他人不被噪音打扰
            (_cli.ROLES_DIR / "pm.md").write_text("# pm 章程 v2\n",
                                                  encoding="utf-8")
            sent_log.clear()
            _cli.cmd_sync(ns)
            check("只改 pm 章程 → 只有 pm 被通知",
                  [p for p, _m in sent_log], ["p-pm"])
            check("pm 收到的是章程全文",
                  any("pm 章程 v2" in m for _p, m in sent_log), True)

            # **CLI 新增子命令**：只告知、不重投章程全文
            save_rules_state(sync_root, {}, version="0.1.8",
                             cli=["say", "status"])
            sent_log.clear()
            _cli._CLI_SURFACE[:] = ["say", "status", "contracts"]
            _cli.cmd_sync(ns)
            check("新增子命令 → 仍然通知（否则 agent 永远不知道）",
                  sorted(p for p, _m in sent_log), ["p-dev", "p-pm", "p-tl"])
            check("新增子命令只是告知，不重投章程全文",
                  any("新增了这些子命令" in m for _p, m in sent_log), True)
            check("告知里点名了 contracts",
                  any("contracts" in m for _p, m in sent_log), True)
        finally:
            _cli.Herdr = orig_herdr
            _cli._require_herdr = orig_require
            _cli.ROLES_DIR, _cli.TEMPLATES = orig_roledir, orig_tpl
            _cli.SKILLS_DIR = orig_skills
            _cli._CLI_SURFACE[:] = []
            shutil.rmtree(tmp_inst, ignore_errors=True)
    finally:
        shutil.rmtree(sync_root, ignore_errors=True)




def test_version_bump_carries_at_hundred() -> None:
    print("\n[新] 版本号满 100 进位（十进制，patch 可走到 99）")
    # 直接从 pack.sh 里**抽出真的 bump_version** 来跑，而不是在这里重写一遍 ——
    # 重写的话，测试就只验证了「我以为的规则」，pack.sh 改坏了也不会红。
    #
    # pack.sh 只存在于**源码布局**：install.sh 装的是 bin/ roles/ templates/
    # tests/ docs/，不含 pack.sh（它是发版工具，不是运行时）。所以装完的副本里
    # 跑不到这条 —— **必须跳过而不是崩**。崩掉的话，`pack.sh --worktree` 的
    # 自证会失败，而那条链路正是发版前的最后一道闸。
    pack_path = Path(__file__).resolve().parent.parent / "pack.sh"
    if not pack_path.exists():
        print("     （本机是安装布局，没有 pack.sh —— 跳过版本进位检查）")
        return
    pack = pack_path.read_text(encoding="utf-8")
    m = re.search(r"^bump_version\(\) \{.*?^\}", pack, re.S | re.M)
    check("pack.sh 里找得到 bump_version", bool(m), True)
    if not m:
        return
    body = m.group(0)
    runner = ("die() { echo \"die: $1\" >&2; exit 1; }\n"
              "say() { :; }\nBUMP=''\n" + body + "\n")
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "b.sh"
        script.write_text(runner, encoding="utf-8")
        script.chmod(0o755)

        def bump(ver: str, kind: str) -> str:
            # 注意必须 **source** 抽出来的脚本 —— 直接 `bump_version` 等于在一个
            # 没有该函数的 shell 里调用，会得到 "command not found"。
            r = subprocess.run(
                ["/bin/bash", "-c",
                 f'. "{script}"; BUMP={kind}; bump_version {ver}'],
                capture_output=True, text=True)
            return r.stdout.strip() or f"<err:{r.stderr.strip()[:40]}>"

        # 频繁发版是常态，所以 patch 必须能一路走到 99 —— 满 10 进位会让人
        # 每 10 次发版就被动升 minor，或者接受「0.1.10」这种别扭的号。
        check("0.2.0 --patch→ 0.2.1", bump("0.2.0", "patch"), "0.2.1")
        check("0.2.9 --patch→ 0.2.10（两位数不是问题）",
              bump("0.2.9", "patch"), "0.2.10")
        check("0.2.98 --patch→ 0.2.99", bump("0.2.98", "patch"), "0.2.99")
        check("0.2.99 --patch→ 0.3.0（满 100 才进位）",
              bump("0.2.99", "patch"), "0.3.0")
        # minor/major 只在你主动升、或攒满 99 次时才动
        check("0.1.99 --minor→ 0.2.0（主动升）", bump("0.1.99", "minor"), "0.2.0")
        check("0.99.5 --minor→ 1.0.0（minor 也满 100 进位）",
              bump("0.99.5", "minor"), "1.0.0")
        check("0.2.0 --major→ 1.0.0（主动升）", bump("0.2.0", "major"), "1.0.0")
        # 越界的号也进位，不继续往上数
        check("手改成 0.2.120 也能正确进位",
              bump("0.2.120", "patch"), "0.3.0")
        check("非 X.Y.Z 被拒", bump("0.1.x", "patch").startswith("<err:"), True)
        check("未知档位被拒", bump("0.1.1", "huge").startswith("<err:"), True)

        # 连升 200 次：**每段都不该出现三位数**。这是「上限 99」的全部理由 ——
        # 三位数版本号会让一部分版本比较器（含 Homebrew 早期解析）措手不及。
        seq = ["0.2.0"]
        for _ in range(200):
            seq.append(bump(seq[-1], "patch"))
        check("连升 200 次后落在 0.4.0", seq[-1], "0.4.0")
        three_digit = [v for v in seq
                       if any(len(seg) > 2 for seg in v.split("."))]
        check("全程不出现三位数段", three_digit[:3], [])
        check("确实经过了 99（说明上限不是随手写的）",
              any(v.endswith(".99") for v in seq), True)


def test_model_name_sanity_filter() -> None:
    print("\n[48] 模型名合理性过滤（防把欢迎语当模型名）")
    # 回归：实测 `claude models` 没有 models 子命令，会忽略参数直接进交互会话，
    # stdout 里是一段欢迎语。解析器把其中某行当模型名 → probe 报「启动超时」，
    # 而真因是模型名是一句话。那个报错会把人引向「agent 认不认这个模型」的错方向。
    for good in ("swe-2-max", "anthropic/claude-opus-4-5",
                 "deepinfra/ByteDance/Seed-2.0-code", "gpt-5", "opus"):
        check(f"认得真实模型名：{good}", plausible_model(good), True)
    bad = [
        "I've loaded the context: global rules about test data boundaries",
        "我已准备好协助你。我可以帮助处理软件开发任务、代码编辑、调试。",
        "Error: stdin is not a terminal",
        "x" * 90,
        "",
    ]
    for b in bad:
        check(f"挡掉非模型名：{b[:32]!r}", plausible_model(b), False)


def test_review_report_fixes() -> None:
    print("\n[49] 审查报告核实后修的五条（回归）")
    # #2 契约路径编码碰撞（数据丢失）：/user_profiles 与 /user/profiles
    #    曾经都叫 GET _user_profiles.md，两个接口互相覆盖。
    f1 = contract_filename("GET", "/user_profiles")
    f2 = contract_filename("GET", "/user/profiles")
    check("含下划线的路径不再与斜杠路径撞名", f1 != f2, True)
    for p in ("/user_profiles", "/user/profiles", "/a_b/c", "/orders/{id}", "/a/b/c"):
        check(f"编码可逆：{p}", parse_contract_name(contract_filename("GET", p))[1], p)
    # 向后兼容：旧文件不含 __ ，仍按老规则读
    check("旧文件仍读得对", parse_contract_name("GET _a_b_c.md"), ("GET", "/a/b/c"))
    # front-matter 让契约文件自己记住真实路径
    import tempfile as _tf
    d = Path(_tf.mkdtemp())
    try:
        q = d / "GET _a_b_c.md"
        q.write_text(CONTRACT_TEMPLATE.format(method="GET", path="/a_b/c"),
                     encoding="utf-8")
        check("front-matter 说了算", contract_real_path(q), ("GET", "/a_b/c"))
        old = d / "GET _x_y.md"; old.write_text("# 老文件", encoding="utf-8")
        check("老文件退回按文件名解码", contract_real_path(old), ("GET", "/x/y"))
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # #5 单应用项目解析：项目名就是根目录名，位置是根目录本身
    root = Path(tempfile.mkdtemp())
    try:
        reg = Registry(root)
        check("单应用：按根目录名解析到根目录", reg.resolve_dir(root.name), root)
        check("单应用：'.' 也认", reg.resolve_dir("."), root)
        check("越界仍然拒绝", reg.resolve_dir(".."), None)
        check("含斜杠仍然拒绝", reg.resolve_dir("a/b"), None)
        check("不存在的项目仍是 None", reg.resolve_dir("nope"), None)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_permission_auto_allow_policy() -> None:
    print("\n[50] 权限弹窗自动放行：只放开发范围内，不可逆操作永不自动")
    # 回归：最初只从文本里取**第一个路径**，于是「Allow `rm -rf /tmp/x`」
    # 被当成「要写 /tmp」而放行 —— 那正是最该拦住的一条。现在命令与路径
    # 都要过黑名单。
    deny_cases = [
        "Allow command? `rm -rf /tmp/x`\n❯ 1. Yes\n  2. No",
        "Allow `git push --force`?\n1. Yes\n2. No",
        "Do you want to run `sudo apt install x`?\n1. Yes\n2. No",
        "需要授权：执行 `DROP TABLE users`\n1. 允许\n2. 拒绝",
    ]
    for tail in deny_cases:
        got = _cli.pick_permission_answer(tail, project="/p")
        check(f"不可逆操作不自动放行：{tail.splitlines()[0][:34]}",
              got["act"], "deny")

    allow_cases = [
        ("Do you want to allow writing to /tmp/build.log?\n❯ 1. Yes\n  2. No",
         "/tmp 路径"),
        ("需要授权：写入 /private/tmp/a.txt\n1. 允许\n2. 拒绝", "中文 + 软链路径"),
        ("Allow writing /p/src/a.py?\n1. Always allow\n2. No", "项目内，且选 always"),
    ]
    for tail, label in allow_cases:
        got = _cli.pick_permission_answer(tail, project="/p")
        check(f"开发范围内自动放行：{label}", got["act"], "allow")
    check("优先选 always（一次授权管到底）",
          _cli.pick_permission_answer(allow_cases[2][0], project="/p")["text"],
          "Always allow")

    # 认不出来就不动 —— 宁可卡住也不乱按
    for tail, label in [
        ("Do you want to proceed?\n1. Yes\n2. No", "认不出请求对象"),
        ("下一步做哪个？\n1. 退租切片（推荐）\n2. 续租", "普通选择题，不是权限框"),
        ("Allow it?\n1. 确认\n2. 取消", "认不出肯定选项"),
    ]:
        check(f"不乱按：{label}",
              _cli.pick_permission_answer(tail, project="/p")["act"], "none")

    # 选项解析要认真实 TUI 的光标字符（❯ ▸ ▶ 等），否则整个选项列表都读不到
    opts = _cli._parse_options("❯ 1. Yes\n  ▸ 2. Always allow\n    3. No")
    check("认得 ❯ / ▸ 开头的选项", [o["text"] for o in opts],
          ["Yes", "Always allow", "No"])


def test_workspace_auto_detection() -> None:
    print("\n[51] workspace 自动认「你当前在的那个」+ 项目参数不被覆盖")
    # #1 位置参数曾覆盖全局 --project：argparse 里子解析器的同名参数会覆盖它，
    #    于是 `xteam --project X up .` 里的 X 被静默忽略，状态全写到当前目录。
    #    实测踩过：`--project /tmp/x up .` 把 .xteam 写进了 ~/Server/dev。
    import argparse as _ap
    ap = _ap.ArgumentParser()
    ap.add_argument("--project", default=".")
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("up")
    u.add_argument("dir", nargs="?", default=None)     # 修复后：不同名
    a = ap.parse_args(["--project", "/tmp/EXPECTED", "up"])
    check("全局 --project 不再被位置参数覆盖", a.project, "/tmp/EXPECTED")
    b = ap.parse_args(["--project", "/tmp/IGNORED", "up", "/tmp/EXPECTED"])
    check("位置参数单独给也认", b.dir, "/tmp/EXPECTED")

    # #2 cwd 反查：pane 的 cwd 是「我在哪个 workspace」的事实
    # 真继承 Herdr，只把两个数据源换成假的 —— 动态挂方法会让静态检查
    # （tests/lint_names.py 的属性校验）判定「类上没有这个方法」而报错，
    # 那是测试写法的问题，不该去放宽检查。
    class FakeHerdr(Herdr):
        def __init__(self, binary="herdr", scope=""):
            super().__init__(binary, scope)
            self.wss, self.panes = [], {}
        def workspaces(self):
            return self.wss
        def _run(self, *a, **k):
            ws = a[3] if len(a) > 3 else ""
            return {"panes": self.panes.get(ws, [])}
        def role_map(self, ws_id):
            return {}
        def find_workspace(self, label):
            return next((w for w in self.wss if w.get("label") == label), None)

    WSS = [{"workspace_id": "w1", "label": "dev"},
           {"workspace_id": "w2", "label": "other"}]
    PANES = {"w1": [{"pane_id": "w1:p1", "cwd": "/home/u/Server/dev"}],
             "w2": [{"pane_id": "w2:p1", "cwd": "/home/u/Herd/x"}]}
    f = FakeHerdr(scope="t")
    f.wss, f.panes = WSS, PANES
    # /home/u/Server/dev/pm-team 在 dev 的 pane 之内 → 命中 dev
    hits = f.detect_by_cwd(Path("/home/u/Server/dev/pm-team"))
    check("子目录也能认出上层 workspace", hits[0][0], "dev")
    check("无关目录不命中", f.detect_by_cwd(Path("/tmp")), [])
    check("显式指定永远优先", _cli.resolve_ws_label(Path("/tmp"), "mine")[0], "mine")


def test_recap_path_and_alert_isolation() -> None:
    print("\n[52] recap：路径正确、记账不污染告警阶梯、按需触发")
    import tempfile as _tf
    from xteam_lib import IdleTracker
    root = Path(_tf.mkdtemp())
    try:
        # #1 路径：提示里曾写 .pm/memory（改名前残留），代码读 .xteam/memory
        want = recap_path(root, "pm")
        check("recap 落在 .xteam/memory 下", ".xteam/memory/pm-recap.md" in str(want),
              True)
        check("不再是 .pm 残留", "/.pm/" not in str(want), True)

        # #2 记账独立：塞字符串进 alerted 会让 working 分支的 `alerted < 1` 抛
        #    TypeError，巡检直接崩 —— 必须在另一个字段上记。
        t = IdleTracker(root / "idle.state")
        t.set_recap_asked("dev", "slice-a")
        check("recap 记账不影响 alerted", t.alerted("dev"), 0)
        try:
            _ = t.alerted("dev") < 1          # 巡检 working 分支就在做这个比较
            ok = True
        except TypeError:
            ok = False
        check("working 分支的比较不会抛 TypeError", ok, True)
        check("recap 记账本身可读回", t.recap_asked_for("dev"), "slice-a")

        # #3 按需：只问在这个切片里留下过工件的角色
        task = root / "tasks" / "s"; task.mkdir(parents=True)
        (task / "request.md").write_text("# r", encoding="utf-8")      # tl 拆过
        (task / "delivered.json").write_text("{}", encoding="utf-8")    # dev 交付过
        check("参与过的角色被识别", roles_touched_task(task), {"tl", "dev"})
        (task / "verdict.json").write_text("{}", encoding="utf-8")
        check("判过 gate 的也算 pm", roles_touched_task(task), {"pm", "tl", "dev"})

        bare = root / "tasks" / "empty"; bare.mkdir(parents=True)
        (bare / "closed.md").write_text("x", encoding="utf-8")
        check("空切片不惊动任何人", roles_touched_task(bare), set())
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_no_stale_path_literals() -> None:
    print("\n[53] 没有失效/散落的路径字面量（改名这类事故的守卫）")
    # 回归：`.pm` → `.xteam` 改名时漏掉了 _maybe_request_recap 里手拼的 f-string，
    # 于是 recap 全写进黑洞，latest_recap() 永远读不到；而 agent 们为了绕过去
    # 改成「双写」，token 成本直接翻倍。这个测试就是防止下次改名再漏。
    repo = Path(__file__).resolve().parent.parent
    import ast as _ast
    offenders: list[str] = []
    # **不能只 glob("*.py")** —— CLI 主程序叫 `bin/xteam`，没有 .py 后缀，
    # 按扩展名扫会漏掉整个主文件。这个守卫本身已经因为这个漏过一次。
    # **测试自身不该假设 bin/。** 装到 install.sh 前缀下是 lib/，写死 bin/ 的
    # 那几处在包里全是 FileNotFoundError。这一类错我犯了三次（test_protocol、
    # lint_names、这个守卫自己），所以钉一条：全仓库不再出现按 "bin" 找代码。
    for f in sorted(_find_cli().parent.parent.iterdir()):
        if not (f.is_file() and
                (f.suffix == ".py" or f.name in ("xteam", "xteam_lib.py"))):
            continue
        text = f.read_text(encoding="utf-8")
        # 跳过 docstring 所在的行：解释「这里曾修过 .pm 残留」的说明文字本身
        # 含 .pm，那是注释不是代码。只按 "#" 剥不掉 —— 它在三引号里面。
        doc_lines: set[int] = set()
        try:
            for node in _ast.walk(_ast.parse(text, filename=str(f))):
                if isinstance(node, (_ast.Module, _ast.FunctionDef,
                                     _ast.AsyncFunctionDef, _ast.ClassDef)):
                    d = _ast.get_docstring(node, clean=False)
                    ds = getattr(node, "body", None)
                    if d and ds and isinstance(ds[0], _ast.Expr):
                        e = ds[0].value
                        doc_lines.update(range(e.lineno, (e.end_lineno or e.lineno) + 1))
        except SyntaxError:
            pass
        for i, line in enumerate(text.splitlines(), 1):
            if i in doc_lines:
                continue
            code = line.split("#", 1)[0]
            if '".pm"' in code or "'.pm'" in code or ".pm/" in code:
                offenders.append(f"{f.name}:{i} 出现 .pm 残留")
    check("代码里没有 .pm 残留", offenders, [])

    # 工作目录名只应有一处定义
    # 同样用 _find_cli 认出的目录，别再写死 bin/
    code = _find_cli().parent
    lib = (code / "xteam_lib.py").read_text(encoding="utf-8")
    cli = (code / "xteam").read_text(encoding="utf-8")
    defines = [l for l in lib.splitlines()
               if l.strip().startswith("XTEAM_DIRNAME")]
    check("XTEAM_DIRNAME 只有一处定义", len(defines), 1)
    # 除定义与说明注释外，代码里不该再直接写 ".xteam"
    #
    # **原来这里写的是 `'"\.xteam"'`，于是这条守卫一直是空转的。**
    # Python 里 `"\."` 不会把反斜杠吃掉（不认识的转义原样保留），所以它找的是
    # 「源码里字面写着反斜杠 + 点 + xteam」—— 这种字符串在整个仓库里一次都没出现，
    # 于是 hard 恒为空、断言恒过：真有人硬编码了 `".xteam"`，这条守卫也抓不到。
    # 它看着在防回归，实际什么都没防。正确写法是 raw string。
    hard = [i for i, l in enumerate(lib.splitlines(), 1)
            if r'".xteam"' in l and not l.strip().startswith("XTEAM_DIRNAME")
            and "之前" not in l]
    hard += [i for i, l in enumerate(cli.splitlines(), 1) if r'".xteam"' in l]
    check("两处代码都不再硬编码工作目录名", hard, [])
    check("CLI 确实用的是常量", "XTEAM_DIRNAME" in cli, True)

    # 真正要守的是「只认一种布局」。按字符串找 `"bin"` 会误伤正确的双认判据
    # （_find_cli 同时试 bin/ 与 lib/），所以改成**在两种布局下真跑一遍**。
    check("test_protocol 自己两种布局都能找到 CLI",
          _find_cli().parent.name in ("bin", "lib"), True)
    lint_src = (repo / "tests" / "lint_names.py").read_text(encoding="utf-8")
    check("lint_names 也认 lib/ 布局", '"lib"' in lint_src, True)

    # 给 agent 看的模板/章程也不能有旧路径 —— 它们照着写
    for f in list((repo / "roles").glob("*.md")) + \
            list((repo / "templates").glob("*.md")):
        check(f"{(f.name)} 里没有 .pm 旧路径",
              ".pm/" in f.read_text(encoding="utf-8"), False)


def test_watchdog_token_leaks_and_crash() -> None:
    print("\n[54] 巡检：不再每轮重复投递，字符串哨兵不再崩巡检")
    import tempfile as _tf
    from xteam_lib import IdleTracker
    root = Path(_tf.mkdtemp())
    try:
        # #1 崩：alerted 按设计是 int-or-string 混合，但调用点写 alerted(role) < 1。
        #    角色被标 suppressed/answered 后，下一轮巡检走到 working 分支就抛
        #    TypeError，**整个看门狗死掉**。两个哨兵值都会触发。
        for sentinel in ("suppressed", "answered"):
            t = IdleTracker(root / f"s-{sentinel}")
            t.set_alerted("dev", sentinel)
            check(f"{sentinel} 时 alert_level 可安全比较",
                  t.alert_level("dev"), 99)
            check(f"{sentinel} 时视为本轮已处理", t.is_settled("dev"), True)
            try:
                _ = t.alert_level("dev") < 1      # 巡检 working 分支在做这个
                ok = True
            except TypeError:
                ok = False
            check(f"{sentinel} 不会抛 TypeError", ok, True)
        t2 = IdleTracker(root / "s-int")
        t2.set_alerted("dev", 0)
        check("整数 0 仍是 0（正常门铃）", t2.alert_level("dev"), 0)
        t2.set_alerted("dev", 2)
        check("阶梯顶到 2", t2.alert_level("dev"), 2)

        # #2 泄漏：context 高时原来每 60 秒问一次 recap（写空串 + 判据是「== 切片名」，
        #    空串永远不等于它）。现在有冷却。
        t3 = IdleTracker(root / "s-cooldown")
        t3.set_recap_asked("dev", "")            # context 触发的那次
        check("刚问过 → 冷却内", t3.recap_asked_ago("dev") < 60, True)
        check("冷却期内不该再问",
              t3.recap_asked_ago("dev") < RECAP_CONTEXT_COOLDOWN, True)
        check("没记录时视为可以问",
              IdleTracker(root / "s-fresh").recap_asked_ago("dev") > 10 ** 8, True)
        check("切片名记账仍按名字走", t3.recap_asked_for("dev"), "")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_restarted_role_selfcheck_and_modelable_defaults() -> None:
    print("\n[55] 重启的 agent 会自检；默认角色跑在可指定模型的 agent 上")
    from xteam_lib import DEFAULT_ROLES, TUI_MODEL_OK, load_verified_agents
    # 默认不用 opencode：它的交互 TUI 不认 --model，只能吃 opencode.json 里
    # 那份指定不了的模型（实测落到 deepseek）。omp 实测认 --model。
    for role, (kind, _cn) in DEFAULT_ROLES.items():
        check(f"{role} 默认不是 opencode", kind != "opencode", True)
        check(f"{role} 的 agent 可指定模型",
              kind in TUI_MODEL_OK or kind in load_verified_agents(), True)
    check("dev 仍用 devin（swe-2-max 实测可用）", DEFAULT_ROLES["dev"][0], "devin")

    # 重启的 agent 必须拿到现状，否则是孤儿：只有章程，它会从零开始问
    root = Path(tempfile.mkdtemp())
    try:
        proto = Protocol(root); proto.ensure()
        t = proto.tasks / "s"; t.mkdir(parents=True)
        (t / "spec.md").write_text("# s", encoding="utf-8")
        (t / "spec.json").write_text('{"round":1}', encoding="utf-8")
        brief = _cli._handover_brief(root, proto, "tl")
        check("底稿说清现在欠什么义务", "tl 欠" in brief, True)
        check("底稿列出未闭合切片", "s" in brief, True)
        check("没 recap 时明说，别让它凭空假设", "以 .xteam/ 下的工件为准" in brief, True)
        src = Path(_cli.__file__).read_text(encoding="utf-8")
        check("up 投章程时附带现状（不是只投章程）",
              "章程+现状已投" in src, True)
        check("明确要求先自检别先问", "不要先开口问" in src, True)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_atomic_json_and_swap_transaction() -> None:
    print("\n[56] JSON 原子写 + 损坏不再静默 + swap 两阶段替换")
    import tempfile as _tf
    from xteam_lib import (write_json, CORRUPT_JSON, _note_corrupt,
                          _read_json)
    # #1 原子写：读者要么看到旧的完整内容，要么看到新的完整内容
    root = Path(_tf.mkdtemp())
    try:
        f = root / "s.json"
        write_json(f, {"a": 1})
        check("写成功", json.loads(f.read_text(encoding="utf-8")), {"a": 1})
        write_json(f, {"a": 2})
        check("覆盖写", json.loads(f.read_text(encoding="utf-8")), {"a": 2})
        check("不留临时文件", [p.name for p in root.iterdir()], ["s.json"])

        # #2 损坏不再静默：仍返回 {}（不能崩），但要记一笔
        CORRUPT_JSON.clear()
        bad = root / "bad.json"
        bad.write_text("{ 不是 json", encoding="utf-8")
        check("坏文件仍返回空而不是崩", _read_json(bad), {})
        check("但记下了损坏", json_health(), [str(bad)])
        check("同一文件不重复刷屏", _note_corrupt(bad, "再报一次") or json_health(),
              [str(bad)])
        # 空文件是合法状态，不该报成损坏
        empty = root / "empty.json"
        empty.write_text("", encoding="utf-8")
        CORRUPT_JSON.clear()
        _read_json(empty)
        check("空文件不算损坏", json_health(), [])
        # 顶层不是对象也算损坏（agent 写出 [1,2] 是真实可能）
        arr = root / "arr.json"
        arr.write_text("[1,2]", encoding="utf-8")
        CORRUPT_JSON.clear()
        _read_json(arr)
        check("顶层非对象算损坏", len(json_health()), 1)
        CORRUPT_JSON.clear()
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # #3 swap 两阶段：确认新 agent 活了之后才关旧 tab
    src = Path(_cli.__file__).read_text(encoding="utf-8")
    i_start = src.index("试起新的")
    i_close = src.index("herdr.close_tab(tab_id)")
    check("起新 agent 在关旧 tab 之前", i_start < i_close, True)
    check("新 agent 失败时明确说旧的不受影响",
          "原 {role} 保持不动" in src, True)
    # 临时名经 agent_name() 合法化（大写 workspace 也能起），不断言字面量拼接。
    check("用临时名避开全局唯一约束", "agent_name(f\"swap-{role}\"" in src, True)

    # #3b **argv 真的把 --model 传出去了吗**
    # 上面所有 agent_args() 断言都是在**单独**调它。而「它算对了」和
    # 「start_agent 把它拼进了 argv」是两件事 —— PR #1 就栽在这里：
    # `extra = spec.agent_args()` 那行还在，`argv += ["--"] + extra` 被删了。
    # 于是 agent_args() 的单测全绿，而每个 agent 实际都跑在**默认模型**上：
    # set-model / omp 默认这套设计整个失效，没有任何测试会红。
    # 所以这里必须真跑一次 start_agent 抓 argv。
    import xteam_lib as _lib

    class _FakeHerdr(_lib.Herdr):
        def __init__(self) -> None:          # 绕开 __init__ 里的真实探测
            self.bin = "herdr"
            self.scope = "Sites"             # 大写：agent_name 正是为它而加
            self.calls: list[tuple] = []

        def _run(self, *argv, **kw):        # noqa: D102
            self.calls.append(argv)

    class _Modelable(_lib.RoleSpec):
        # 绕开 probe 状态：这里只关心「有 --model 时会不会被拼进 argv」，
        # 而 tui_model_ok 依赖本机跑过 probe，会让结果随机器变。
        def supports_tui_model(self) -> bool:
            return True

    _h = _FakeHerdr()
    _h.start_agent("p1", "t1", _Modelable(role="pm", kind="devin", cn="开发",
                                           model="devin-large"))
    _argv0 = _h.calls[0]
    check("start_agent 把 --model 拼进了 argv（agent_args 算对了不等于传出去了）",
          "--model" in _argv0, True)
    check("--model 后面紧跟模型名", "devin-large" in _argv0, True)
    check("agent name 已合法化（大写 workspace → 小写 slug）", _argv0[2], "pm-sites")

    # opencode 的 TUI 不认 --model，那类 kind 一个都不能传
    class _NoModel(_lib.RoleSpec):
        def supports_tui_model(self) -> bool:
            return False

    _h3 = _FakeHerdr()
    _h3.start_agent("p2", "t2", _NoModel(role="pm", kind="opencode", cn="产品",
                                         model="whatever"))
    check("不支持 TUI model 的 kind 不传 --model", "--model" in _h3.calls[0], False)

    # slug 太长时必须截断到 herdr 的 32 字符上限，而不是被拒
    check("超长 workspace 名被截断到 32 字符",
          len(_lib.agent_name("pm", "x" * 80)), 32)


def test_recap_no_loop_and_next_slice_pushes() -> None:
    print("\n[57] recap 不循环 + 队列有活时系统自己往下推")
    import tempfile as _tf
    from xteam_lib import IdleTracker, OBLIGATIONS
    root = Path(_tf.mkdtemp())
    try:
        # #1 recap 循环：context 分支把记账写成 ""，而切片分支判据是「== 切片名」，
        #    "" 不等于任何切片名 → 下一轮又命中切片分支；冷却挂在 context 分支后面，
        #    此时 need 已 True 所以根本没被检查 → **每 60 秒问一次，跑一夜几百次**
        #    （用户实测 696 次）。修法是冷却提到入口，涵盖所有触发原因。
        src = Path(_cli.__file__).read_text(encoding="utf-8")
        i_cool = src.index("if tracker.recap_asked_ago(role) < RECAP_CONTEXT_COOLDOWN")
        i_slice = src.index("# 刚闭合的切片：只问真正参与过的角色")
        check("冷却在切片分支之前（涵盖所有触发原因）", i_cool < i_slice, True)

        # 冷却生效时切片分支根本不该被走到
        t = IdleTracker(root / "s")
        t.set_recap_asked("dev", "")            # 刚问过（context 触发）
        check("冷却期内不会再问（不论哪条路径）",
              t.recap_asked_ago("dev") < RECAP_CONTEXT_COOLDOWN, True)

        # #2 队列有活却没人欠账 → PM 欠 start-next，否则 idle+owes:none 被当成
        #    「合法终态」，三个 pane 全闲着而人成了唯一推进力。
        proto = Protocol(root); proto.ensure()
        (proto.dir / "QUEUE.md").write_text(
            "| # | 切片 | 状态 | 备注 |\n|---|---|---|---|\n"
            "| 1 | a | todo | |\n| 2 | b | todo | |\n", encoding="utf-8")
        d = proto.overall_debts()
        check("队列有活而 PM 无欠账 → PM 欠 start-next",
              any("start-next" in x for x in d.get("pm", [])), True)
        (proto.dir / "QUEUE.md").write_text(
            "| # | 切片 | 状态 | 备注 |\n|---|---|---|---|\n"
            "| 1 | a | done | |\n", encoding="utf-8")
        check("队列清空后不再有这个义务（此时问人类才对）",
              proto.overall_debts().get("pm"), None)

        # #3 文案不能在教 PM 去问人类
        check("next-slice 教的是自己取下一项",
              "先自己看 QUEUE.md" in OBLIGATIONS["next-slice"], True)
        check("next-slice 明确禁止问「可以开始下一件吗」",
              "别问「可以开始下一件吗」" in OBLIGATIONS["next-slice"], True)
        check("start-next 说明这不是「做完了」而是停住了",
              "是停住了" in OBLIGATIONS["start-next"], True)
        pm_charter = (repo_doc("roles/pm.md")).read_text(encoding="utf-8")
        check("PM 章程禁止问人类「可以开始下一件吗」",
              "不要问人类「可以开始下一件吗」" in pm_charter, True)
        tl_charter = (repo_doc("roles/tl.md")).read_text(encoding="utf-8")
        check("TL 章程明确「方案成本差太多」不是阻塞理由",
              "不是阻塞理由" in tl_charter, True)
        check("TL 章程列举了不算阻塞的实现选择",
              "全是实现选择" in tl_charter, True)
        # 任务边界：spec 范围节 → request 禁区节 → review 先核禁区 → dev 多一点都不写。
        # 缺任何一环，超范围改动都只在事后被发现（甚至不被发现）。
        check("PM 章程要求 spec 写「范围」节",
              "「范围」节" in pm_charter, True)
        check("PM 章程写明 spec 没写的一律不做",
              "spec 没写的，一律不做" in pm_charter, True)
        check("TL 章程缺范围节的 spec 直接打回",
              "缺范围节" in tl_charter, True)
        check("TL 章程要求 request 写「禁区」节",
              "「禁区」节" in tl_charter, True)
        check("TL 章程 review 先核禁区",
              "先核禁区" in tl_charter, True)
        dev_charter = (repo_doc("roles/dev.md")).read_text(encoding="utf-8")
        check("DEV 章程禁区是硬线多一点都不写",
              "多一点都不写" in dev_charter, True)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_version_flag() -> None:
    print("\n[58] --version / -v：回答「这台机器跑的是哪个构建」")
    import subprocess
    binx = _find_cli()
    for flag in ("--version", "-v"):
        r = subprocess.run([sys.executable, str(binx), flag],
                           capture_output=True, text=True, timeout=60)
        out = r.stdout
        check(f"{flag} 退出码 0", r.returncode, 0)
        check(f"{flag} 打出 xteam 版本号", out.startswith("xteam "), True)
        check(f"{flag} 含提交号", "提交：" in out, True)
        check(f"{flag} 含 Python 版本", "Python：" in out, True)
        check(f"{flag} 含资源路径", "资源：" in out, True)
        check(f"{flag} 含布局（装的还是源码）", "布局：" in out, True)
        check(f"{flag} 不是报错", "usage:" not in out, True)
    # 不带子命令应给帮助而不是静默退出（但 --version 优先）
    r = subprocess.run([sys.executable, str(binx)], capture_output=True,
                       text=True, timeout=60)
    check("不带子命令给帮助", "usage:" in r.stdout, True)
    # VERSION 文件在包里（发布到另一台机器要能对上版本）
    check("VERSION 文件存在", (repo_doc("VERSION")).exists(), True)
    check("版本号不是 '未知'",
          (repo_doc("VERSION")).read_text(encoding="utf-8").strip() != "未知", True)


def test_qa_agent_whitelist() -> None:
    print("\n[64] 第三方评审白名单：配置里不能塞任意可执行程序")
    from xteam_lib import QaConfig, QaAgentRejected, QA_ALLOWED, run_external_review

    root = Path(tempfile.mkdtemp())
    (root / ".xteam").mkdir()
    qc = QaConfig(root)

    # 每一个都必须被拒 —— 这些都是「配置 = 任意代码执行」的实际利用形式
    for bad in ("/bin/curl", "/usr/bin/env", "./evil.sh", "../../bin/sh",
                "rm", "python3 -c", "", "qodercli; rm -rf /"):
        qc.path.write_text(json.dumps({"agent": bad}), encoding="utf-8")
        try:
            cfg = qc.load()
            check(f"拒绝 {bad!r}", False, True)
        except QaAgentRejected:
            check(f"拒绝 {bad!r}", True, True)

    # 白名单里的正常放行
    for ok in sorted(QA_ALLOWED):
        qc.path.write_text(json.dumps({"agent": ok}), encoding="utf-8")
        check(f"白名单内 {ok} 放行", qc.load()["agent"], ok)

    # 第二道闸：绕过 load 直接调执行函数，仍然拒
    task = root / ".xteam" / "tasks" / "t1"
    task.mkdir(parents=True)
    ok, note = run_external_review(
        root, task, {"agent": "/bin/curl", "model": "", "timeout": 5})
    check("绕过 load 直接执行也被拒", ok, False)
    check("拒绝原因说清是白名单", "白名单" in note, True)

    shutil.rmtree(root, ignore_errors=True)


def test_permission_path_boundary() -> None:
    print("\n[65] 自动放行的目录边界：startswith 会放行越界路径")
    import importlib.util
    # permission_verdict 在 bin/xteam 里（脚本，不是模块），用 spec 抠出来跑
    src = Path(_find_cli()).read_text(encoding="utf-8")
    ns: dict = {}
    start = src.index("def permission_verdict")
    end = src.index("def pick_permission_answer")
    exec("ALLOW_PATHS=['/tmp']\nNEVER_AUTO=[]\nfrom pathlib import Path\n"
         + src[start:end], ns)
    v = ns["permission_verdict"]

    # 这些以前全是 allow：startswith 只是字符串前缀匹配
    for bad in ("/tmp/../Users/admin/.ssh/authorized_keys",
                "/tmp2/secret", "/repo-evil/file", "/repo/../etc/passwd"):
        check(f"越界不放行 {bad}", v(bad, project="/repo"), "unknown")

    # 真正在允许目录内的照常放行
    for good in ("/tmp", "/tmp/ok.txt", "/tmp/sub/deep/file.py", "/repo/src/a.py"):
        check(f"目录内放行 {good}", v(good, project="/repo"), "allow")

    # 符号链接：链接在 /tmp 下，指向 ~/.ssh —— resolve 后必须露馅
    link = Path("/tmp") / "xteam-test-sneaky-link"
    ssh = Path.home() / ".ssh"
    if not ssh.exists():
        check("跳过符号链接用例（~/.ssh 不存在）", True, True)
    else:
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(ssh)
            check("符号链接指向外部不放行", v(str(link), project="/repo"), "unknown")
        finally:
            if link.is_symlink():
                link.unlink()

    # 相对路径无法在无 cwd 时判边界 -> 交给人
    check("相对路径不自动放行", v("relative/path", project="/repo"), "unknown")


def test_waiting_deps_excludes_cycles_and_typos() -> None:
    print("\n[66] status 的「等前置」剔除成环与拼错（F-1 口径）")
    root = Path(tempfile.mkdtemp())
    try:
        proto = Protocol(root); proto.ensure()

        def slice_(name: str, deps: list) -> None:
            t = proto.tasks / name
            t.mkdir(parents=True, exist_ok=True)
            (t / "task.json").write_text(
                json.dumps({"depends_on": deps}), encoding="utf-8")
            (t / "spec.md").write_text("# s", encoding="utf-8")
            (t / "spec.json").write_text('{"round": 1}', encoding="utf-8")

        slice_("a", ["b"]); slice_("b", ["a"])
        slice_("bad", ["ap-side"])
        slice_("fe-side", ["api-side"])
        slice_("api-side", [])
        # deps_blocking 本身照旧三类全收——状态机地板不能动；
        # 「等前置」过滤是渲染层的活：环上与拼错的切片被剔除，真在等的留下。
        waiting = _cli._waiting_deps(proto, proto.open_tasks())
        check("成环的 a 不算等前置", "a" not in waiting, True)
        check("成环的 b 不算等前置", "b" not in waiting, True)
        check("拼错的 bad 不算等前置", "bad" not in waiting, True)
        check("合法等待 fe-side 留下", waiting.get("fe-side"), ["api-side"])
        check("无依赖 api-side 不在等", "api-side" not in waiting, True)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_workspace_belongs_gate() -> None:
    print("\n[67] status 显式 workspace 的归属闸（F-2 口径）")
    root = Path(tempfile.mkdtemp())
    try:
        proto = Protocol(root); proto.ensure()

        class FakeHerdr:
            """只答 detect_by_cwd 的桩——归属判据就认它 + session.json。"""
            def __init__(self, hits: list) -> None: self._hits = hits
            def detect_by_cwd(self, cwd: Path): return self._hits

        wb = _cli._workspace_belongs
        check("pane cwd 落在项目内 → 属于，放行",
              wb(FakeHerdr([("inside", 4)]), proto, root, "inside"), True)
        check("detect 查无此 ws → 不属于，要拒",
              wb(FakeHerdr([]), proto, root, "other"), False)
        check("detect 命中的是别的 ws，也不算属于",
              wb(FakeHerdr([("inside", 4)]), proto, root, "other"), False)
        (proto.dir / "session.json").write_text(
            json.dumps({"workspace": "seen"}), encoding="utf-8")
        check("session.json 记过的 workspace → 属于",
              wb(FakeHerdr([]), proto, root, "seen"), True)
        check("session.json 记的不是它 → 仍不属于",
              wb(FakeHerdr([]), proto, root, "other"), False)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_herdr_missing_converges_and_never_swallows() -> None:
    print("\n[68] herdr 缺失收敛到地基提示，且不吞其他异常（F-3 口径）")
    import contextlib
    import io

    def func_raising(exc: Exception):
        def _f(_a: argparse.Namespace) -> int:
            raise exc
        return _f

    def func_ok(_a: argparse.Namespace) -> int:
        return 7

    ns = argparse.Namespace()

    # herdr 抛的 FileNotFoundError → die（SystemExit(1)）+ 地基文案。
    ns.func = func_raising(FileNotFoundError(2, "No such file or directory",
                                             "herdr"))
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            _cli._dispatch(ns)
        check("herdr FNF 应转成 die", "no-exit", "SystemExit")
    except SystemExit as exc:
        check("herdr FNF → SystemExit(1)", exc.code, 1)
        check("提示含地基文案", "找不到 herdr" in buf.getvalue(), True)

    # 别的 FileNotFoundError（项目文件丢了）照常抛出去，不吞。
    ns.func = func_raising(FileNotFoundError(2, "No such file or directory",
                                             str(Path("x") / "gone.txt")))
    try:
        _cli._dispatch(ns)
        check("非 herdr FNF 应传播", "no-raise", "propagate")
    except FileNotFoundError:
        check("非 herdr FNF 不被收敛点吞掉", True, True)
    except SystemExit:
        check("非 herdr FNF 被误吞成 herdr 提示", False, True)

    # 其他异常（编程错误）也不被吞。
    ns.func = func_raising(ValueError("boom"))
    try:
        _cli._dispatch(ns)
        check("ValueError 应传播", "no-raise", "propagate")
    except ValueError:
        check("ValueError 不被吞", True, True)

    # 正常分发不受影响。
    ns.func = func_ok
    check("正常命令透传 rc", _cli._dispatch(ns), 7)

    # 入口预检：which 查无 herdr → die；桩 which 而非环境，免得依赖本机装没装。
    orig = _cli.shutil.which
    try:
        _cli.shutil.which = lambda _c: None
        buf = io.StringIO()
        try:
            with contextlib.redirect_stderr(buf):
                _cli._require_herdr()
            check("预检应 die", "no-exit", "SystemExit")
        except SystemExit as exc:
            check("预检 herdr 缺失 → SystemExit(1)", exc.code, 1)
            check("预检提示同一句地基文案",
                  "找不到 herdr" in buf.getvalue(), True)
    finally:
        _cli.shutil.which = orig


def test_prompt_blocked_roles_detection() -> None:
    print("\n[69] status 的 blocked 判定：停在权限框的角色被标出（F-4 口径）")

    PROMPT_TAIL = (" ● Running command\n"
                   "❭ 1 Yes, allow `wc` commands\n"
                   "  8 No\n"
                   "↑↓ select · ↵ confirm · esc cancel\n")
    PLAIN_TAIL = " ● Thinking · 15m 29s\n"

    class PaneHerdr:
        """只答 read_pane 的桩——判定的全部输入就是 pane 尾巴文本。"""
        def __init__(self, tails: dict, fail: tuple = ()) -> None:
            self._tails, self._fail = tails, set(fail)
        def read_pane(self, pane_id: str, lines: int = 60) -> str:
            if pane_id in self._fail:
                raise RuntimeError("pane gone")
            return self._tails[pane_id]

    rows = [("pm",  "经理", "idle", "none", "", "p1"),
            ("dev", "开发", "done", "implement", "cur", "p3")]

    check("停在权限框的 dev 被标出",
          _cli._prompt_blocked_roles(
              PaneHerdr({"p1": PLAIN_TAIL, "p3": PROMPT_TAIL}), rows),
          ["dev"])
    check("无框不误报",
          _cli._prompt_blocked_roles(
              PaneHerdr({"p1": PLAIN_TAIL, "p3": PLAIN_TAIL}), rows),
          [])
    check("pane read 失败降级为无框、不抛错",
          _cli._prompt_blocked_roles(
              PaneHerdr({"p1": PLAIN_TAIL}, fail=("p3",)), rows),
          [])
    check("无 pane_id 的角色直接跳过",
          _cli._prompt_blocked_roles(
              PaneHerdr({}),
              [("dev", "开发", "done", "implement", "cur", "")]),
          [])


def test_confirm_delivery_conditional_enter() -> None:
    print("\n[70] say 送达判据：转 working 才算；排队占位符才补 enter（F-5 口径）")

    QUEUED = ("────────── (bypass permissions on) ─\n"
              "❭ Press Enter to send queued messages now\n")
    BUSY = " ● Thinking · 15m 29s\n"

    class BellHerdr:
        """agent_status 按序出值；_run/read_pane 记调用，供断言序列。"""
        def __init__(self, states: list, tails: dict) -> None:
            self._states = list(states)
            self._tails = tails
            self.calls: list = []
        def agent_status(self, pane_id: str) -> str:
            self.calls.append("agent_status")
            return self._states.pop(0) if self._states else "done"
        def read_pane(self, pane_id: str, lines: int = 60) -> str:
            self.calls.append("read_pane")
            return self._tails[pane_id]
        def _run(self, *args):
            self.calls.append(" ".join(args))
            return {}

    # 已 working + 占位符（v3/AC-8：mid-turn 时门铃是排队进 TUI 的，
    # 必须读一次尾巴——占位符在就补一次 enter 再复核）。
    h = BellHerdr(["working", "working"], {"p3": QUEUED})
    check("已 working+占位符 → 补 enter 后送达",
          _cli._confirm_delivery(h, "p3"), (True, "working", True))
    check("序列=status/读尾巴/send-keys/status",
          h.calls, ["agent_status", "read_pane",
                    "pane send-keys p3 enter", "agent_status"])

    # 已 working + 输入行有内容（无占位符）→ 不按 enter（F-6 边界）。
    h = BellHerdr(["working"], {"p3": BUSY})
    check("已 working+有内容 → 送达不补键",
          _cli._confirm_delivery(h, "p3"), (True, "working", False))
    check("序列=status/读尾巴（无 send-keys）",
          h.calls, ["agent_status", "read_pane"])

    # done + 尾巴有占位符 → 补 enter → 复核转 working。
    h = BellHerdr(["done", "working"], {"p3": QUEUED})
    check("占位符在 → 补 enter 后送达", _cli._confirm_delivery(h, "p3"),
          (True, "working", True))
    check("补键后复核了 agent_status",
          h.calls,
          ["agent_status", "read_pane", "pane send-keys p3 enter",
           "agent_status"])

    # done + 尾巴无占位符（输入行可能有内容）→ 不补，未送达。
    h = BellHerdr(["done"], {"p3": BUSY})
    check("无占位符 → 不补、未送达", _cli._confirm_delivery(h, "p3"),
          (False, "done", False))
    check("无占位符没有 send-keys",
          "send-keys" in " ".join(h.calls), False)

    # 补了仍未转 working → 未送达。
    h = BellHerdr(["done", "done"], {"p3": QUEUED})
    check("补了仍 done → 未送达", _cli._confirm_delivery(h, "p3"),
          (False, "done", True))


def test_cli_surface_populated_through_main() -> None:
    print("\n[71] CLI 面快照经过 main() 才定型（F-6 口径）")
    import contextlib
    import io

    orig_argv = sys.argv
    orig_surface = list(_cli._CLI_SURFACE)
    try:
        sys.argv = ["xteam", "--version"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                _cli.main()          # 快照必须在所有 add_parser 之后才取得到全集
            except SystemExit:
                pass
        surface = _cli._cli_surface()
        check("经 main() 后 CLI 面非空", bool(surface), True)
        for c in ("say", "status", "watch", "contracts", "up"):
            check(f"CLI 面含 {c}", c in surface, True)
        check("二级子命令不进一级面",
              "list" not in surface and "new" not in surface
              and "run" not in surface, True)
    finally:
        sys.argv = orig_argv
        _cli._CLI_SURFACE[:] = orig_surface   # 快照别漏给后面的用例


def test_nag_self_stale_three_states() -> None:
    print("\n[72] _nag_self_stale 三态：缺失/损坏静默 · 不一致报 · 一致不报")
    import hashlib

    tmp = Path(tempfile.mkdtemp())
    logf = tmp / "watch" / "events.log"
    tracker = IdleTracker(tmp / "watch" / "idle.state")
    self_file = tmp / ".xteam" / "watch" / "self.json"

    def stale_lines() -> list[str]:
        if not logf.exists():
            return []
        return [ln for ln in logf.read_text(encoding="utf-8").splitlines()
                if "SELF-STALE" in ln]

    # 记录缺失 → 静默。
    _cli._nag_self_stale(tmp, logf, tracker)
    check("无 self.json 不报", stale_lines(), [])

    # 记录损坏 → 静默（不得崩）。
    self_file.parent.mkdir(parents=True, exist_ok=True)
    self_file.write_text("{broken", encoding="utf-8")
    _cli._nag_self_stale(tmp, logf, tracker)
    check("self.json 损坏不报", stale_lines(), [])

    # 指纹不同 → SELF-STALE 且提重启；冷却内不重复喊。
    self_file.write_text(json.dumps({"bin_xteam_sha256": "0" * 64}),
                         encoding="utf-8")
    _cli._nag_self_stale(tmp, logf, tracker)
    lines = stale_lines()
    check("指纹不一致报 SELF-STALE", len(lines), 1)
    check("SELF-STALE 行提到重启", "重启" in lines[0], True)
    _cli._nag_self_stale(tmp, logf, tracker)
    check("冷却内不重复报", len(stale_lines()), 1)

    # 指纹相同 → 不报，且清掉冷却（下次真变能立刻喊）。
    cur = hashlib.sha256(Path(_cli.__file__).resolve()
                         .read_bytes()).hexdigest()
    self_file.write_text(json.dumps({"bin_xteam_sha256": cur}),
                         encoding="utf-8")
    _cli._nag_self_stale(tmp, logf, tracker)
    check("指纹一致不报", len(stale_lines()), 1)
    check("一致后冷却已清", "nag:self-stale" not in tracker.state, True)


def test_recap_asked_set_converges() -> None:
    print("\n[73] recap 集合记账：三片各问一次后收敛；context 空串不污染")
    tmp = Path(tempfile.mkdtemp())
    tr = IdleTracker(tmp / "idle.state")
    for t in ("s1", "s2", "s3"):
        tr.set_recap_asked("pm", t)
    check("三片都进已问集合", tr.recap_asked_all("pm"), {"s1", "s2", "s3"})
    check("单槽位仍记最近一次（兼容读法）", tr.recap_asked_for("pm"), "s3")

    tr.set_recap_asked("pm", "")            # context 分支写空串的那次
    check("context 空串不进集合", tr.recap_asked_all("pm"),
          {"s1", "s2", "s3"})
    check("空串仍进单槽位（冷却用例语义不变）", tr.recap_asked_for("pm"), "")

    tr.set_recap_asked("pm", "s1")
    check("重复问同一片不重复记", tr.recap_asked_all("pm"),
          {"s1", "s2", "s3"})
    tr2 = IdleTracker(tmp / "idle.state")
    check("集合跨实例持久", tr2.recap_asked_all("pm"), {"s1", "s2", "s3"})
    check("alerted 阶梯仍不被污染（[52] 不变量）", tr2.alerted("pm"), 0)


def test_unstarted_queue_gate_for_pm_nags() -> None:
    print("\n[74] 未开工判据：spec.md 在=已开工不催 PM；无 spec.md/目录缺失仍催（F-11）")
    # 闭合切片 + 队列 pending 项已开工 → PM 不欠（下游在跑是合法等待）
    b = Bench("done1")
    try:
        (b.proto.dir / "QUEUE.md").write_text(
            "| # | 切片 | 状态 | 备注 |\n|---|---|---|---|\n"
            "| 1 | done1 | done | |\n| 2 | q1 | doing | |\n", encoding="utf-8")
        b.advance_to("closed")
        # q1 已开工 = spec.md 在；补到共识完成（TL 欠 decompose），让 PM 无 per-task
        # 债务——才能看到「不补 start-next」是判据所致而不是被别的欠账盖住。
        q1 = b.proto.tasks / "q1"
        q1.mkdir()
        (q1 / "spec.md").write_text("# spec\n", encoding="utf-8")
        (q1 / "spec.json").write_text('{"round": 1}\n', encoding="utf-8")
        (q1 / "assessment.json").write_text(
            '{"spec_round": 1, "verdict": "agree"}\n', encoding="utf-8")
        (q1 / "agreement.json").write_text(
            '{"spec_round": 1}\n', encoding="utf-8")
        check("队列项已开工 → next-slice 让位（PM 不欠）", b.owes("pm"), [])
        check("已开工 → overall 也不补 start-next",
              b.proto.overall_debts().get("pm"), None)
        check("欠账落在下游 TL（decompose），不是 PM",
              b.proto.overall_debts().get("tl"), ["q1:decompose"])
        (q1 / "spec.md").unlink()
        check("删 spec.md = 未开工 → PM 欠 next-slice",
              b.owes("pm"), ["next-slice"])
        shutil.rmtree(q1)
        check("目录缺失（未开工另一形态）→ 仍欠 next-slice",
              b.owes("pm"), ["next-slice"])
    finally:
        b.cleanup()

    # start-next 补位：无人欠账 + 队列有未开工项才催；开工后不补
    b2 = Bench("probe")
    try:
        b2.task.rmdir()                                    # 没有任何切片目录
        (b2.proto.dir / "QUEUE.md").write_text(
            "| # | 切片 | 状态 | 备注 |\n|---|---|---|---|\n"
            "| 1 | q9 | todo | |\n", encoding="utf-8")
        check("队列有未开工项且无人欠账 → PM 欠 start-next",
              b2.proto.overall_debts().get("pm"), ["(队列):start-next"])
        q9 = b2.proto.tasks / "q9"
        q9.mkdir(parents=True)
        (q9 / "spec.md").write_text("# spec\n", encoding="utf-8")
        (q9 / "spec.json").write_text('{"round": 1}\n', encoding="utf-8")
        (q9 / "assessment.json").write_text(
            '{"spec_round": 1, "verdict": "agree"}\n', encoding="utf-8")
        (q9 / "agreement.json").write_text(
            '{"spec_round": 1}\n', encoding="utf-8")
        check("q9 已过共识（已开工）→ PM 不欠 start-next",
              b2.proto.overall_debts().get("pm"), None)
        check("欠账落在下游 TL（decompose），不是 PM",
              b2.proto.overall_debts().get("tl"), ["q9:decompose"])
    finally:
        b2.cleanup()

    # 「队列空 + 闭合 → report」分支不受新判据影响（禁区，防回归再钉一次）
    b3 = Bench("fin")
    try:
        (b3.proto.dir / "QUEUE.md").write_text(
            "| # | 切片 | 状态 | 备注 |\n|---|---|---|---|\n"
            "| 1 | fin | done | |\n", encoding="utf-8")
        b3.advance_to("closed")
        check("队列全 done + 闭合 + 无 REPORT → PM 欠 report（不变）",
              b3.proto.overall_debts().get("pm"), ["(项目):report"])
    finally:
        b3.cleanup()


def test_status_merges_overall_synthetic_debts() -> None:
    print("\n[75] status 合并 overall 合成项：无 per-task 时显示、有则不覆盖（F-12）")
    merge = _cli._merge_overall_pending
    check("无 per-task + overall 有 report → 显示",
          merge([], ["(项目):report"]), [("(项目)", "report")])
    check("合成名拆回 (scope, obligation) 同形",
          merge([], ["(队列):start-next"]), [("(队列)", "start-next")])
    check("有 per-task 欠账 → 不覆盖（report 不挤掉 next-slice）",
          merge([("f-done", "next-slice")], ["(项目):report"]),
          [("f-done", "next-slice")])
    check("两边都空 → 空", merge([], []), [])
    check("多条合成项全映射",
          merge([], ["(项目):report", "(队列):start-next"]),
          [("(项目)", "report"), ("(队列)", "start-next")])

    src = Path(_cli.__file__).read_text(encoding="utf-8")
    check("cmd_status 走合并函数（接线在）",
          "_merge_overall_pending(debts.get(role" in src, True)
    check("合并前取了 overall_debts", "overall = proto.overall_debts()" in src, True)


def test_say_recheck_window() -> None:
    print("\n[76] say 复核窗口：无占位符两轮复核，mid-turn 不误报（F-14 口径）")
    import argparse
    import contextlib
    import io
    import tempfile
    import xteam_lib as _lib

    BUSY = " ● Thinking · 15m 29s\n"

    class FlipHerdr:
        """agent_status 按调用次数翻转：第 flip_at 次起返回 working。"""
        def __init__(self, flip_at: int) -> None:
            self._flip_at = flip_at
            self.calls: list = []
        def agent_status(self, pane_id: str) -> str:
            self.calls.append("agent_status")
            return "working" if len(self.calls) >= self._flip_at else "done"
        def read_pane(self, pane_id: str, lines: int = 60) -> str:
            self.calls.append("read_pane")
            return BUSY
        def _run(self, *args):
            self.calls.append(" ".join(args))
            return {}

    # 常量钉死：具名、非零、bin/xteam 与 lib 是同一份（spec §2.1 复核窗口）。
    check("SAY_RECHECK_S 具名且非零", _lib.SAY_RECHECK_S > 0, True)
    check("bin/xteam 用的是同一个常量", _cli.SAY_RECHECK_S, _lib.SAY_RECHECK_S)

    # 单测不真睡（窗口存在由 e2e 计时证明），临时缩小复核间隔，跑完恢复。
    real = _cli.SAY_RECHECK_S
    _cli.SAY_RECHECK_S = 0.01
    try:
        # mid-turn：第 2 轮复核才转 working → 送达（修复前只有 1 轮必然误报）。
        h = FlipHerdr(2)
        check("第2轮转working → 送达", _cli._confirm_delivery(h, "p3"),
              (True, "working", False))
        check("复核序列 = status/读尾巴/status（无 send-keys）",
              h.calls, ["agent_status", "read_pane", "agent_status"])

        # 两轮都不动 → 未送达（真失败仍要报）。
        h = FlipHerdr(999)
        check("两轮皆done → 未送达", _cli._confirm_delivery(h, "p3"),
              (False, "done", False))
        check("未送达也查了第 2 轮",
              h.calls, ["agent_status", "read_pane", "agent_status"])
    finally:
        _cli.SAY_RECHECK_S = real

    # prompt 失败走冻结的 prompt-failed 分支（F-5 §2.2 第三种渲染），rc=1。
    class FailHerdr:
        def __init__(self, scope=None) -> None:
            pass
        def find_workspace(self, label):
            return {"workspace_id": "wQ"}
        def role_map(self, workspace_id):
            return {"dev": {"pane_id": "wQ:p3"}}
        def doorbell(self, pane, message, wait_s=0.0, kind="", wait_empty_s=15.0):
            return "prompt-failed: boom"

    real_herdr = _cli.Herdr
    _cli.Herdr = FailHerdr
    try:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ".xteam").mkdir()
            args = argparse.Namespace(project=td, workspace="w1",
                                      role="dev", message="x", settle=0,
                                      wait_empty=0.0)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                 contextlib.redirect_stderr(err):
                rc = _cli.cmd_say(args)
            check("prompt失败 rc=1（不吞成成功）", rc, 1)
            check("prompt失败主行含 prompt-failed（冻结格式）",
                  "prompt-failed" in out.getvalue(), True)
            check("prompt失败 stderr 含投递失败",
                  "投递失败" in err.getvalue(), True)
    finally:
        _cli.Herdr = real_herdr


def _mk_stamps(root: Path, slug: str, stamps: dict[str, float],
               contents: dict[str, str] | None = None) -> None:
    """造一个切片目录，文件时间戳写死 —— stats/throughput 的测试布景。"""
    task = root / ".xteam" / "tasks" / slug
    task.mkdir(parents=True, exist_ok=True)
    for name, ts in stamps.items():
        path = task / name
        body = (contents or {}).get(name) or ("{}" if name.endswith(".json") else "# x")
        path.write_text(body, encoding="utf-8")
        os.utime(path, (ts, ts))


def test_phase_times_and_throughput_from_mtimes() -> None:
    print("\n[85] stats：分段耗时/中位/并行度/重叠命中都从 mtime 算")
    t0 = 1_700_000_000.0
    b = Bench()
    try:
        # a、b 走完全程；c 只到 request；d 是「a 的冻结窗口里正在写」的那一片。
        _mk_stamps(b.tmp, "a", {"spec.md": t0, "request.md": t0 + 600,
                                "delivered.json": t0 + 1800, "ready.json": t0 + 2100,
                                "verdict.json": t0 + 2400, "closed.md": t0 + 2460})
        _mk_stamps(b.tmp, "b", {"spec.md": t0 + 3000, "request.md": t0 + 3300,
                                "delivered.json": t0 + 4500, "ready.json": t0 + 4800,
                                "verdict.json": t0 + 5100, "closed.md": t0 + 5160},
                   contents={"verdict.json": json.dumps(
                       {"round": 1, "delivery": 1, "verdict": "PASS"})})
        _mk_stamps(b.tmp, "c", {"spec.md": t0 + 6000, "request.md": t0 + 6600})
        _mk_stamps(b.tmp, "d", {"request.md": t0 + 1700, "delivered.json": t0 + 2000})
        rows = {r["slice"]: r for r in b.proto.phase_times()}
        check("a 的分段耗时（分钟）", rows["a"]["minutes"],
              {"tl_decomp": 10.0, "dev_impl": 20.0, "tl_review": 5.0,
               "pm_gate": 5.0, "close": 1.0})
        check("a 的合计 41m", rows["a"]["total"], 41.0)
        check("c 只到 request：后面几段是 None 不是 0",
              [rows["c"]["minutes"][p] for p in ("tl_review", "pm_gate", "close")],
              [None, None, None])
        check("c 未闭合 → 没有合计", rows["c"]["total"], None)
        check("b 的判定读自 verdict.json", rows["b"]["verdict"], "PASS")
        check("a 没有 verdict.json → 判定 None", rows["a"]["verdict"], None)
        tp = b.proto.throughput(list(rows.values()))
        check("片数/闭合数", (tp["slices"], tp["closed"]), (4, 2))
        check("sum 只累有合计的片（41 + 36）", tp["sum_minutes"], 77.0)
        check("墙 = 最晚 spec - 最早 spec", tp["wall_minutes"], 100.0)
        check("并行度 = sum/墙", tp["parallelism"], 0.77)
        check("中位 dev 实现", tp["median"]["dev_impl"], 20.0)
        check("中位合计（41/36 → 38.5）", tp["median"]["total"], 38.5)
        check("冻结合计（a 10m + b 10m）", tp["freeze_minutes"], 20.0)
        check("重叠命中：a 的冻结窗口里 d 正在写", tp["freeze_with_other_writer"], 1)
    finally:
        b.cleanup()


def test_phase_times_missing_file_and_backwards_mtime() -> None:
    print("\n[86] stats：缺文件 → None；mtime 回退 → 负值原样（不夹成 0）")
    t0 = 1_700_000_000.0
    b = Bench()
    try:
        _mk_stamps(b.tmp, "half", {"spec.md": t0})
        _mk_stamps(b.tmp, "back", {"spec.md": t0, "request.md": t0 - 600})
        rows = {r["slice"]: r for r in b.proto.phase_times()}
        check("只写了 spec：tl_decomp 是 None", rows["half"]["minutes"]["tl_decomp"], None)
        check("request 早于 spec：负时长原样",
              rows["back"]["minutes"]["tl_decomp"], -10.0)
        tp = b.proto.throughput(list(rows.values()))
        check("中位只吃有值的那一片（负值也算数）", tp["median"]["tl_decomp"], -10.0)
    finally:
        b.cleanup()


def test_gate_window_freezes_other_implement() -> None:
    print("\n[77] 已交付未判窗口冻结其它 implement；verdict 对准恢复（F-16）")
    b = Bench("a")
    try:
        # a 推进到已交付（无 ready/verdict）：TL review 期间同样是窗口。
        b.advance_to("delivered")
        tb = b.proto.tasks / "b"
        tb.mkdir()
        (tb / "request.md").write_text("# 拆解\n", encoding="utf-8")
        check("ready 未写也在窗口（交付即冻结）",
              b.proto.pending_gate_tasks(), ["a"])
        check("窗口内 b 的 implement 冻结（dev 不欠）", b.owes("dev"), [])
        check("tl 仍欠 chase（窗口要有人关）", b.owes("tl"), ["chase"])
        # start-next 同条件冻结：队列放未开工项且 PM 无 per-task 债也不补。
        (b.proto.dir / "QUEUE.md").write_text(
            "| 序 | 切片 | 状态 | 备注 |\n|---|---|---|---|\n| 1 | q1 | todo | |\n",
            encoding="utf-8")
        check("窗口内未开工项也不催 PM 开新片",
              b.proto.overall_debts().get("pm"), None)
        b.advance_to("ready")
        check("ready 后未判 → 仍 pending", b.proto.pending_gate_tasks(), ["a"])
        check("pm 欠 gate（对准 ready 的 verdict 缺）", b.owes("pm"), ["gate"])
        # verdict 对准这次交付 → 窗口关，义务恢复。
        b.advance_to("verdict")     # {"round":1,"delivery":1,"verdict":"PASS"}
        check("verdict 对准 → pending 清空", b.proto.pending_gate_tasks(), [])
        check("窗口关 → b 的 implement 回来",
              "implement" in b.owes("dev"), True)
        check("a 自己转欠 consume（不被冻）", "consume" in b.owes("dev"), True)
        b.advance_to("consumed")
        check("a 静默后 dev 只剩 b:implement", b.owes("dev"), ["implement"])
        # FAIL 对准当前交付 → 不算 pending（fail_pending 照常欠 implement）。
        b.write("verdict.json", {"round": 1, "delivery": 1, "verdict": "FAIL"})
        check("FAIL 已对准 → 非 pending", b.proto.pending_gate_tasks(), [])
        check("FAIL 的 a 照常欠 implement（fail_pending 保留）",
              "implement" in b.owes("dev"), True)
        # verdict 只对准旧交付：重交 r2 未判 → 窗口重新开。
        b.write("delivered.json",
                {"round": 2, "head": "ccc", "verification": []})
        check("verdict 对准旧交付 → 重交仍 pending",
              b.proto.pending_gate_tasks(), ["a"])
        check("重交未判窗口内 b 又冻结", b.owes("dev"), [])
    finally:
        b.cleanup()

    # 无待判片：行为与现状一致（防回归）。
    b2 = Bench("solo")
    try:
        b2.advance_to("request")
        check("无待判片 → solo 欠 implement",
              b2.owes("dev"), ["implement"])
        check("无待判片 → pending_gate 为空",
              b2.proto.pending_gate_tasks(), [])
    finally:
        b2.cleanup()


def test_shadowed_install_detection() -> None:
    print("\n[78] PATH 遮蔽检测：全命中顺序 / 启动器解析 / 同一份双向（F-18）")
    tmp = Path(tempfile.mkdtemp())
    try:
        # 布景：mine = 本次运行的实现；other/lib = 另一份安装。
        me = tmp / "mine" / "bin" / "xteam"
        me.parent.mkdir(parents=True)
        me.write_text("# impl of THIS install\n", encoding="utf-8")
        me.chmod(0o755)
        other_lib = tmp / "other" / "lib"
        other_lib.mkdir(parents=True)
        other_impl = other_lib / "xteam"
        other_impl.write_text("# impl of OTHER install\n", encoding="utf-8")

        # install.sh 生成格式的启动器（指向另一份安装）。
        bin_a = tmp / "bin_a"; bin_a.mkdir()
        launcher = bin_a / "xteam"
        launcher.write_text(
            "#!/usr/bin/env bash\n"
            f"export PM_TEAM_HOME=\"${{PM_TEAM_HOME:-{tmp}/other/share}}\"\n"
            f"exec python3 \"{other_impl}\" \"$@\"\n", encoding="utf-8")
        launcher.chmod(0o755)

        # 同一份安装的两种入口：软链 + 指向同一实现的启动器。
        bin_b = tmp / "bin_b"; bin_b.mkdir()
        link = bin_b / "xteam"
        link.symlink_to(me)
        bin_c = tmp / "bin_c"; bin_c.mkdir()
        same_launcher = bin_c / "xteam"
        same_launcher.write_text(
            "#!/usr/bin/env bash\n"
            f"exec python3 \"{me}\" \"$@\"\n", encoding="utf-8")
        same_launcher.chmod(0o755)
        # 不可执行的同名文件不算命中。
        noexec = tmp / "noexec"; noexec.mkdir()
        (noexec / "xteam").write_text("nope\n", encoding="utf-8")

        hits = _cli._path_hits("xteam", f"{bin_a}:{bin_b}:{noexec}")
        check("全命中按优先级顺序（不可执行不列）", hits, [launcher, link])
        dup = _cli._path_hits("xteam", f"{bin_b}:{bin_b}")
        check("重复目录如实列出", dup, [link, link])
        check("空 PATH 无命中", _cli._path_hits("xteam", str(tmp / "none")), [])

        check("启动器解析到 exec 的真身",
              _cli._resolve_entry(launcher), other_impl.resolve())
        check("软链解析到链目标", _cli._resolve_entry(link), me.resolve())
        check("脚本本体解析自身",
              _cli._resolve_entry(other_impl), other_impl.resolve())

        check("首份指另一份安装 → 遮蔽",
              _cli._shadow_hit([launcher, link], me.resolve()), launcher)
        check("首份是同一份的软链 → 不误报",
              _cli._shadow_hit([link], me.resolve()), None)
        check("首份是同 root 的启动器 → 不误报",
              _cli._shadow_hit([same_launcher], me.resolve()), None)
        check("PATH 无命中 → 不遮蔽",
              _cli._shadow_hit([], me.resolve()), None)
        check("遮蔽只认第 1 份（第 2 份不同不碍事）",
              _cli._shadow_hit([link, launcher], me.resolve()), None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_say_three_state_render() -> None:
    print("\n[79] say 三态单值 + mid-turn 占位符补 enter（F-19 v2/v3）")
    import argparse
    import contextlib
    import io

    PH = "❭ Press Enter to send queued messages now\n"
    CONTENT = " ● Thinking · 15m 29s\n❭ Guide Devin while it works\n"

    class CtlHerdr:
        """doorbell 恒成功（status=idle）；agent_status 由队列驱动；尾巴由 tail 定。"""
        def __init__(self, statuses: list, tail: str) -> None:
            self._st = list(statuses)
            self._tail = tail
            self.calls: list = []
        def find_workspace(self, label):
            return {"workspace_id": "wV"}
        def role_map(self, workspace_id):
            return {"dev": {"pane_id": "wV:p3"}}
        def doorbell(self, pane, message, wait_s=0.0, kind="", wait_empty_s=15.0):
            return "status=idle"
        def agent_status(self, pane_id: str) -> str:
            self.calls.append("agent_status")
            return self._st.pop(0) if self._st else "idle"
        def read_pane(self, pane_id: str, lines: int = 60) -> str:
            self.calls.append("read_pane")
            return self._tail
        def _run(self, *args):
            self.calls.append(" ".join(args))
            return {}

    def say_with(stub) -> tuple[int, str, str]:
        real_herdr = _cli.Herdr
        _cli.Herdr = lambda scope=None: stub
        try:
            with tempfile.TemporaryDirectory() as td:
                (Path(td) / ".xteam").mkdir()
                args = argparse.Namespace(project=td, workspace="w1",
                                          role="dev", message="x", settle=0,
                                          wait_empty=0.0)
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), \
                     contextlib.redirect_stderr(err):
                    rc = _cli.cmd_say(args)
            return rc, out.getvalue(), err.getvalue()
        finally:
            _cli.Herdr = real_herdr

    real_sleep = _cli.SAY_RECHECK_S
    _cli.SAY_RECHECK_S = 0.01
    try:
        # (a) 立刻 working、输入行有内容 → 已送达 rc0，绝不按 enter（AC-1/AC-9）。
        h = CtlHerdr(["working"], CONTENT)
        rc, out, err = say_with(h)
        check("立刻working → 已送达 rc0", rc, 0)
        check("(a) 主行含已送达", "已送达" in out, True)
        check("(a/AC-9) 有内容不按 enter",
              [c for c in h.calls if "send-keys" in c], [])
        check("(a) 已 working 也读了 pane（v3）", "read_pane" in h.calls, True)

        # (b) 恒 idle → 已投递·未确认 rc0（AC-2；不许说未送达/已送达）。
        h = CtlHerdr(["idle", "idle"], CONTENT)
        rc, out, err = say_with(h)
        check("恒idle → rc=0（非失败）", rc, 0)
        check("(b) 含已投递", "已投递" in out, True)
        check("(b) 含未确认", "未确认" in out, True)
        check("(b) stdout 不含未送达/已送达",
              ("未送达" in out) or ("已送达" in out), False)
        check("(b) stderr 不含未送达", "未送达" in err, False)

        # (b') 延迟转 working（队列第 3 次才 working = 窗口外）→ 同样 (b)。
        h = CtlHerdr(["idle", "idle", "working"], CONTENT)
        rc, out, err = say_with(h)
        check("延迟转working 仍是已投递·未确认", "已投递·未确认" in out, True)

        # AC-8：mid-turn（working）+ 排队占位符 → 读 pane → 补一次 enter 再复核。
        h = CtlHerdr(["working", "working"], PH)
        rc, out, err = say_with(h)
        check("mid-turn+占位符 → 已送达 rc0", rc, 0)
        check("(AC-8) 占位符在 → 恰补一次 enter",
              [c for c in h.calls if "send-keys" in c],
              ["pane send-keys wV:p3 enter"])
    finally:
        _cli.SAY_RECHECK_S = real_sleep


def test_queue_order_picks_slice_and_next_slice_label() -> None:
    print("\n[80] 义务名按 QUEUE 序挑片 + next-slice 标签取首个 todo（F-20）")
    QH = "| # | 切片 | 状态 | 备注 |\n|---|---|---|---|\n"
    b = Bench("zz-late")
    try:
        b.advance_to("specv")                       # spec.json r1 → tl:assess
        second = b.proto.tasks / "aa-early"
        second.mkdir(parents=True, exist_ok=True)
        (second / "spec.md").write_text("# spec\n", encoding="utf-8")
        (second / "spec.json").write_text('{"round": 1}', encoding="utf-8")

        # 无 QUEUE → 目录序回退（AC-5 的退回形态）
        check("无 QUEUE：tl pending[0] 取目录序 aa-early",
              b.proto.obligations()["tl"][0], ("aa-early", "assess"))

        q = b.proto.dir / "QUEUE.md"
        q.write_text(QH + "| 1 | zz-late | doing | |\n"
                          "| 2 | aa-early | doing | |\n", encoding="utf-8")
        check("错开序：tl pending[0] 取 QUEUE 序 zz-late",
              b.proto.obligations()["tl"][0], ("zz-late", "assess"))
        check("overall_debts 同样按队列序",
              b.proto.overall_debts()["tl"][0], "zz-late:assess")

        q.write_text(QH + "| 1 | aa-early | doing | |\n"
                          "| 2 | zz-late | doing | |\n", encoding="utf-8")
        check("一致序：tl pending[0] 仍 aa-early（不变）",
              b.proto.obligations()["tl"][0], ("aa-early", "assess"))

        q.write_text(QH + "| 1 | zz-late | doing | |\n", encoding="utf-8")
        check("有记序在前、无记序保持目录序缀后",
              [t.name for t in b.proto._tasks_by_queue_order()],
              ["zz-late", "aa-early"])

        q.write_text(QH + "| 9 | done-old | done | |\n"
                          "| 3 | zz-late | doing | |\n", encoding="utf-8")
        check("_queue_order 连 done 项也记序",
              b.proto._queue_order().get("done-old"), 9)

        q.write_text("这不是表格\n???\n", encoding="utf-8")
        check("坏 QUEUE：_queue_order 为空", b.proto._queue_order(), {})
        check("坏 QUEUE：tl pending[0] 回退目录序（不崩）",
              b.proto.obligations()["tl"][0], ("aa-early", "assess"))
    finally:
        b.cleanup()

    # next-slice 标签 = 首个未开工项；闭合片名永不进标签（AC-3/证据B）
    c = Bench("done-old")
    try:
        c.advance_to("closed")
        q = c.proto.dir / "QUEUE.md"
        q.write_text(QH + "| 1 | done-old | done | |\n"
                          "| 2 | fresh-next | todo | |\n", encoding="utf-8")
        check("next-slice 标签 = 首个 todo 项 fresh-next",
              c.proto.obligations()["pm"][0], ("fresh-next", "next-slice"))
        check("overall_debts 标签同口径",
              c.proto.overall_debts()["pm"], ["fresh-next:next-slice"])
        check("闭合片名不进任何欠账标签",
              "done-old" in (str(c.proto.overall_debts())
                             + str(c.proto.obligations())), False)
        check("_next_slice_label 直验", c.proto._next_slice_label(),
              "fresh-next")

        q.write_text(QH + "| 1 | done-old | done | |\n", encoding="utf-8")
        check("无未开工项 → 标签空", c.proto._next_slice_label(), "")
        check("无未开工项 → next-slice 让位（现状不变）",
              "next-slice" in str(c.proto.obligations()["pm"]), False)
    finally:
        c.cleanup()


def test_pane_version_drift_warning() -> None:
    print("\n[81] status 头部版本漂移警告三态（F-21）")
    b = Bench("a")
    try:
        xdir = b.proto.dir
        real_version = _cli._version()

        # 三态：不等 → 警告行；一致 → None；无记录 → None
        (xdir / "rules.json").write_text(
            '{"xteam_version": "0.0.1"}', encoding="utf-8")
        line = _cli._pane_version_drift(b.tmp)
        check("版本不等 → 返回警告行", isinstance(line, str), True)
        check("警告行含 pane 版", "0.0.1" in line, True)
        check("警告行含运行版", real_version in line, True)
        check("警告行含修复指引", "xteam up" in line, True)

        (xdir / "rules.json").write_text(
            f'{{"xteam_version": "{real_version}"}}', encoding="utf-8")
        check("版本一致 → 不警告", _cli._pane_version_drift(b.tmp), None)

        (xdir / "rules.json").unlink()
        check("无记录 → 不警告（老会话不吵）",
              _cli._pane_version_drift(b.tmp), None)

        # session.json 有键时按字面口径也认（spec 的名义源）
        (xdir / "session.json").write_text(
            '{"xteam_version": "0.0.1"}', encoding="utf-8")
        line = _cli._pane_version_drift(b.tmp)
        check("session.json 的 xteam_version 也触发", isinstance(line, str), True)
        check("session 口径同样含两版本",
              "0.0.1" in line and real_version in line, True)
        (xdir / "session.json").write_text('{"panes": {}}', encoding="utf-8")
        check("session 无键且 rules 无 → 不警告",
              _cli._pane_version_drift(b.tmp), None)
    finally:
        b.cleanup()


def test_undispatched_tasks_threshold() -> None:
    print("\n[82] request 派发超时点名 TL（mtime > IDLE_ALERT_SECS 才算没人领，F-23）")
    import os
    import time as _t
    b = Bench("stale")
    try:
        b.advance_to("request")                     # request.md 在、无 delivered
        req = b.task / "request.md"

        # 未超阈值：刚落地的 request 不该点名 TL（不误报，AC-2）
        check("刚落地 → 不点名", b.proto.undispatched_tasks(), [])

        # 超阈值：mtime 拨回 601s 前 → 点名
        old = _t.time() - 601
        os.utime(req, (old, old))
        check("mtime 601s 前 → 点名 stale",
              b.proto.undispatched_tasks(), ["stale"])

        # 恰好阈值边上不算超（< now-600 才算超；now_ts 可注入钉死边界）
        edge = _t.time() - IDLE_ALERT_SECS + 5
        os.utime(req, (edge, edge))
        check("阈值内（599s 前）→ 不点名",
              b.proto.undispatched_tasks(), [])
        edge = _t.time() - IDLE_ALERT_SECS - 5
        os.utime(req, (edge, edge))
        check("阈值外（605s 前）→ 点名",
              b.proto.undispatched_tasks(), ["stale"])

        # delivered.json 在 = 已领走，不再点名
        b.advance_to("delivered")
        check("已交付 → 不点名", b.proto.undispatched_tasks(), [])

        # closed 片即使有滞留 request 也不点名
        c = Bench("gone")
        try:
            c.advance_to("closed")
            r = c.task / "request.md"
            o = _t.time() - 3600
            os.utime(r, (o, o))
            check("closed → 不点名", c.proto.undispatched_tasks(), [])
        finally:
            c.cleanup()

        # 队列序也适用：两片同时超时按 QUEUE 序点名
        d = Bench("zz")
        try:
            d.advance_to("request")
            (d.proto.tasks / "aa").mkdir(parents=True, exist_ok=True)
            (d.proto.tasks / "aa" / "request.md").write_text(
                "# 拆解\n", encoding="utf-8")
            for name in ("zz", "aa"):
                p = d.proto.tasks / name / "request.md"
                o = _t.time() - 900
                os.utime(p, (o, o))
            (d.proto.dir / "QUEUE.md").write_text(
                "| # | 切片 | 状态 | 备注 |\n|---|---|---|---|\n"
                "| 1 | zz | doing | |\n| 2 | aa | doing | |\n",
                encoding="utf-8")
            check("超时多片按 QUEUE 序点名", d.proto.undispatched_tasks(),
                  ["zz", "aa"])
        finally:
            d.cleanup()
    finally:
        b.cleanup()


def test_identity_goes_into_system_prompt() -> None:
    print("\n[81] 身份进系统提示词：/new、/clear 清不掉它")
    import xteam_lib
    from xteam_lib import (SYSTEM_PROMPT_ARG, RESET_COMMAND, RoleSpec,
                           context_hint, reset_command, write_identity,
                           identity_path)
    # 表本身：omp / pi 是实测过的（别的 kind 加进来之前必须同样实测）
    check("omp 认 --append-system-prompt", SYSTEM_PROMPT_ARG.get("omp"),
          "--append-system-prompt")
    check("pi 也认（与 omp 同一套 TUI，实测 /new 后身份仍在）",
          SYSTEM_PROMPT_ARG.get("pi"), "--append-system-prompt")
    check("omp 的原地重开命令是 /new", RESET_COMMAND.get("omp"), "/new")
    check("pi 的原地重开命令是 /new（实测）", RESET_COMMAND.get("pi"), "/new")
    check("没实测过的 kind 不硬塞 flag（如 opencode）",
          SYSTEM_PROMPT_ARG.get("opencode"), None)
    check("没实测过的 kind 不改写它的启动行为",
          RESET_COMMAND.get("devin"), None)

    # agent_args：带身份文件才加 flag，且**不能吞掉 --model**
    with_id = RoleSpec("pm", "omp", "产品经理", "gpt-5", "/p/.xteam/identity/pm.md")
    check("omp 带身份时拼上 flag 和路径", with_id.agent_args(),
          ["--model", "gpt-5", "--append-system-prompt",
           "/p/.xteam/identity/pm.md"])
    check("omp 不带身份时维持原样",
          RoleSpec("pm", "omp", "产品经理", "gpt-5").agent_args(),
          ["--model", "gpt-5"])
    check("不支持的 kind 有了身份也不硬塞",
          RoleSpec("dev", "devin", "开发", "", "/p/id.md").agent_args(), [])

    root = Path(tempfile.mkdtemp())
    try:
        path = write_identity(root, "pm", _cli.ROLES_DIR, label="demo")
        check("身份文件落在 .xteam/identity/<role>.md",
              path, identity_path(root, "pm"))
        text = path.read_text(encoding="utf-8")
        charter = (_cli.ROLES_DIR / "pm.md").read_text(encoding="utf-8").strip()
        check("章程全文一字不落地附在后面", charter[:200] in text, True)
        check("带上角色与项目名", ("pm" in text and "demo" in text), True)
        check("写明重置后先 whoami", "xteam whoami" in text, True)
        check("写明身份不会被重置清掉", "系统提示词" in text, True)

        # 门铃前缀：只有「身份不在系统提示词里」的 kind 才需要
        check("omp 不加门铃噪音", context_hint("pm", "omp"), "")
        devin_hint = context_hint("dev", "devin")
        check("devin 的门铃自带身份提醒",
              ("dev" in devin_hint and "xteam whoami" in devin_hint), True)
        check("devin 没有原地重开命令（该走 swap）", reset_command("devin"), "")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_context_pct_all_three_kinds() -> None:
    print("\n[82] context 占用解析：omp 的进度条格式也要认（默认 agent 就是它）")
    from xteam_lib import parse_context_pct, REOPEN_ADVISE_PCT
    check("opencode 格式（带括号）", parse_context_pct(" 392.8K (37%)"), 37)
    check("devin 格式", parse_context_pct(" 168k / 262k tokens (64%)"), 64)
    # 实测 omp 状态栏（2026-10-07）：百分号夹在横杠里，右边跟 `|` + 窗口上限
    check("omp 进度条格式", parse_context_pct(
        " pi > [max] X > [T] /p > $1.18 >----------------------51%------------|-----1M-"),
        51)
    check("omp 新会话的小数（0.7%）", parse_context_pct(
        " pi > [max] X > [T] /p > $0.00 >-0.7%----------------|-----1M-"), 0)
    check("pi 的占用/窗口格式", parse_context_pct(
        " ↑12k ↓100 R512 CH4.0% $0.025 (sub) 2.6%/500k (auto)"), 2)
    check("解析不出就不猜", parse_context_pct("no numbers here"), None)
    # 阈值两处（recap 60 / 重开 75）都吃这个解析：必须真的能跨过
    check("omp 的 80% 能触发重开建议",
          (parse_context_pct("-----80%------|----1M-") or 0) >= REOPEN_ADVISE_PCT,
          True)


def test_whoami_and_reopen_intro() -> None:
    print("\n[83] whoami 一屏交回现场；重开的 intro 不重复烧章程")
    import contextlib
    import io
    b = Bench("identity-probe")
    try:
        b.advance_to("ready")                   # 这一级：PM 欠 gate
        root, proto = b.tmp, b.proto
        mem = proto.dir / "memory"
        mem.mkdir(parents=True, exist_ok=True)
        (mem / "pm-recap.md").write_text(
            "## 做完什么\n- 队列清了\n## 下一步 / 遗留\n- 等 identity-probe 的 verdict\n",
            encoding="utf-8")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _cli.cmd_whoami(argparse.Namespace(project=str(root), role="pm"))
        out = buf.getvalue()
        check("whoami 正常返回", rc, 0)
        check("说清你是谁", "你是 pm" in out, True)
        check("列出你欠的义务", "identity-probe:gate" in out, True)
        check("列出未闭合切片", "identity-probe" in out, True)
        check("带你上次的 recap 尾巴",
              "等 identity-probe 的 verdict" in out, True)
        check("给出该读的路径", "PROTOCOL.md" in out, True)

        # 没有 .xteam 的目录要报错，而不是假装有内容
        bare = Path(tempfile.mkdtemp())
        try:
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    _cli.cmd_whoami(argparse.Namespace(project=str(bare), role="pm"))
                code = 0
            except SystemExit as exc:
                code = exc.code or 0
            check("没有 .xteam 时明确报错（不假装成功）", code != 0, True)
        finally:
            shutil.rmtree(bare, ignore_errors=True)

        # intro：身份在系统提示词里的 kind 不重复抄章程；别的 kind 必须抄全文
        charter = (_cli.ROLES_DIR / "pm.md").read_text(encoding="utf-8").strip()
        head = charter[:200]
        intro_omp = _cli._restart_intro(root, proto, "pm", "omp", "why")
        check("omp 的 intro 不重复抄章程", head in intro_omp, False)
        check("omp 的 intro 指向系统提示词", "系统提示词" in intro_omp, True)
        check("omp 的 intro 仍带现状", "identity-probe" in intro_omp, True)
        intro_devin = _cli._restart_intro(root, proto, "pm", "devin", "why")
        check("devin 的 intro 带章程全文", head in intro_devin, True)
        intro_forced = _cli._restart_intro(root, proto, "pm", "omp", "why",
                                           with_charter=True)
        check("显式要求时照样带（up 复用老 pane 的场景）",
              head in intro_forced, True)

        # 重置生效判据：会话变了（omp）或新出现回执（pi）；读不到才不否决
        class FakeHerdr:
            def __init__(self, sessions, pane_text="", seq=None):
                self.sessions = sessions
                self.text = pane_text
                self.reads = 0
                self.seq = seq or []
            def agent_session(self, pane):
                v = self.sessions[min(self.reads, len(self.sessions) - 1)]
                return v
            def read_pane(self, pane, lines=200):
                self.reads += 1
                if self.seq:
                    return self.seq[min(self.reads, len(self.seq) - 1)]
                return self.text
        ok, why = _cli._wait_reset_ok(FakeHerdr(["a"]), "p", "a", 0, timeout=0)
        check("会话没变也没有回执 → 判定没生效", ok, False)
        ok, why = _cli._wait_reset_ok(FakeHerdr(["b"]), "p", "a", 0, timeout=1)
        check("会话标识变了 → 生效", (ok, why), (True, "会话标识已变"))
        # pi 不暴露会话：靠 pane 里多出一条 New session started 回执
        ok, why = _cli._wait_reset_ok(
            FakeHerdr([""], seq=["", "✓ New session started"]), "p", "", 0,
            timeout=1)
        check("回执多了一条 → 生效", (ok, "回执" in why), (True, True))
        # 滚动区里**留着旧回执**（次数没涨）→ 不能当成新会话
        ok, why = _cli._wait_reset_ok(
            FakeHerdr([""], pane_text="[ok] New session started"), "p", "", 1,
            timeout=0)
        check("旧回执不算生效（数次数，不数有无）", ok, False)
        ok, why = _cli._wait_reset_ok(FakeHerdr([""], pane_text=""), "p", "", 0,
                                      timeout=0)
        check("该 kind 什么都不暴露时不否决（说清依据）",
              (ok, "无法确认" in why), (True, True))

        # session.json 的身份记录：决定 reopen 要不要把章程抄进对话
        check("没记身份 → 按「没有」处理", _cli._session_identity({}, "pm"), "")
        check("记了身份就认",
              _cli._session_identity({"identity": {"pm": "/p/id.md"}}, "pm"),
              "/p/id.md")

        # 巡检的重开建议：落事件 + 时间线 + 通知，冷却期内不重复喊
        tr = IdleTracker(root / "idle.state")
        logged: list = []
        real_notify = _cli.notify
        _cli.notify = lambda t, b: logged.append((t, b))
        try:
            class PaneHerdr:
                def read_pane(self, pane, lines=10):
                    return " $1.18 >----------------80%----------|-----1M-"
            role_map = {"pm": {"pane_id": "p", "agent_status": "idle"}}
            events = proto.dir / "watch" / "events.log"
            _cli._maybe_advise_reopen(root, PaneHerdr(), role_map, tr, events)
            check("高位 context 落事件",
                  "REOPEN-SUGGEST" in events.read_text(encoding="utf-8"), True)
            timeline = (proto.dir / "watch" / "timeline.log").read_text(
                encoding="utf-8")
            check("建议里给出可执行的命令", "xteam reopen pm" in timeline, True)
            check("响通知（唯一能打断人的通道）", len(logged), 1)
            _cli._maybe_advise_reopen(root, PaneHerdr(), role_map, tr, events)
            check("冷却期内不重复喊", len(logged), 1)

            class LowHerdr:
                def read_pane(self, pane, lines=10):
                    return " $0.00 >-0.7%---------|-----1M-"
            _cli._maybe_advise_reopen(root, LowHerdr(), role_map, tr, events)
            check("重开后（数字掉下来）冷却被清掉",
                  "reopen:pm" in tr.state, False)

            class WorkingHerdr:
                def read_pane(self, pane, lines=10):
                    raise AssertionError("working 时不该读 pane")
            _cli._maybe_advise_reopen(
                root, WorkingHerdr(),
                {"pm": {"pane_id": "p", "agent_status": "working"}}, tr, events)
            check("working 时不打扰（也不采样）", len(logged), 1)
        finally:
            _cli.notify = real_notify
    finally:
        b.cleanup()


def test_charter_reply_timestamp_rule() -> None:
    print("\n[84] 三角色章程钉死「对人类文字回复首行带 [YYYY-MM-DD HH:MM:SS]」（F-24）")
    tokens = ("文字回复", "第一行", "[YYYY-MM-DD HH:MM:SS]")

    def has_rule(text: str) -> bool:
        return all(t in text for t in tokens)

    for role in ("pm", "tl", "dev"):
        text = repo_doc(f"roles/{role}.md").read_text(encoding="utf-8")
        check(f"{role}.md 含首行时间戳硬规则", has_rule(text), True)

    # 双向钉死：没有这句的旧章程必须过不了守卫
    old = ("## 每次输出都带时间戳\n\n```bash\nxteam stamp dev \"x\"\n```\n\n"
           "**不要手写时间戳。** 手写的可靠性等于「你对自己何时看过表」的记忆。\n")
    check("缺这句的旧章程过不了守卫", has_rule(old), False)
    compliant = ("对人类的文字回复，第一行必须含当前系统时间 "
                 "[YYYY-MM-DD HH:MM:SS]。")
    for t in tokens:
        check(f"三字样缺「{t}」不算数", has_rule(compliant.replace(t, "")), False)


def test_touches_absent_means_conflict() -> None:
    print("\n[87] touches 缺失/坏 JSON/空 → 未知 = 跟谁都冲突（保守方向）")
    b = Bench()
    try:
        ta, tb = b.proto.tasks / "a", b.proto.tasks / "b"
        tb.mkdir(exist_ok=True)
        check("缺 touches.json → None（未知）", b.proto.touches(ta), None)
        check("未知 vs 任意 → 算冲突", b.proto.conflict_with(ta, ["b"]), ["b"])
        (tb / "touches.json").write_text('{"paths": ["docs/*.md"]}', encoding="utf-8")
        check("对面也未知 → 仍算冲突", b.proto.conflict_with(ta, ["b"]), ["b"])
        (ta / "touches.json").write_text('{"paths": []}', encoding="utf-8")
        check("空列表 = 未知，不是「不碰任何文件」", b.proto.touches(ta), None)
        (ta / "touches.json").write_text("{ 坏 json", encoding="utf-8")
        check("坏 JSON → None（不许抛）", b.proto.touches(ta), None)
        (ta / "touches.json").write_text('{"paths": "bin/x"}', encoding="utf-8")
        check("paths 不是列表 → None", b.proto.touches(ta), None)
    finally:
        b.cleanup()


def test_conflict_glob_both_directions() -> None:
    print("\n[88] 触碰冲突：双向 glob；不相交才放行")
    b = Bench()
    try:
        ta, tb = b.proto.tasks / "a", b.proto.tasks / "b"
        tb.mkdir(exist_ok=True)
        (ta / "touches.json").write_text('{"paths": ["bin/*.py"]}', encoding="utf-8")
        (tb / "touches.json").write_text('{"paths": ["bin/xteam_lib.py"]}',
                                         encoding="utf-8")
        check("glob vs 命中的文件 → 相交", b.proto.conflict_with(ta, ["b"]), ["b"])
        (tb / "touches.json").write_text('{"paths": ["bin/xteam"]}', encoding="utf-8")
        check("glob 没命中它（它是无扩展名的二进制）→ 不相交",
              b.proto.conflict_with(ta, ["b"]), [])
        (tb / "touches.json").write_text('{"paths": ["bin"]}', encoding="utf-8")
        check("声明目录 = 碰它下面所有文件 → 相交",
              b.proto.conflict_with(ta, ["b"]), ["b"])
        (tb / "touches.json").write_text('{"paths": ["docs/*.md"]}', encoding="utf-8")
        check("不相交 → 空（可以并行）", b.proto.conflict_with(ta, ["b"]), [])
        check("自己不算自己的冲突", b.proto.conflict_with(ta, ["a"]), [])
        (tb / "touches.json").write_text('{"paths": ["./bin/xteam_lib.py"]}',
                                         encoding="utf-8")
        check("前导 ./ 归一化后仍相交", b.proto.conflict_with(ta, ["b"]), ["b"])
    finally:
        b.cleanup()


def test_overlap_allows_disjoint_implement() -> None:
    print("\n[89] 解锁重叠：触碰不相交时，待判窗口里别的片也能开工")
    b = Bench("a")
    try:
        b.advance_to("delivered")                  # a：已交付未判，窗口开着
        (b.task / "touches.json").write_text('{"paths": ["bin/xteam"]}',
                                             encoding="utf-8")
        tb = b.proto.tasks / "b"
        tb.mkdir()
        (tb / "request.md").write_text("# 拆解\n", encoding="utf-8")
        check("b 没声明触碰 = 未知 → 仍冻结（向后兼容）",
              b.proto.debts(tb).get("dev"), None)
        (tb / "touches.json").write_text('{"paths": ["bin/xteam"]}', encoding="utf-8")
        check("b 跟 a 碰同一批文件 → 冻结", b.proto.debts(tb).get("dev"), None)
        (tb / "touches.json").write_text('{"paths": ["docs/*.md"]}', encoding="utf-8")
        check("b 触碰不相交 → dev 可以开工 b", b.proto.debts(tb).get("dev"), "implement")
        check("a 自己是待判片 → 不催它 implement", b.proto.debts(b.task).get("dev"), None)
        check("待判窗口照旧可查（状态面/兜底还用它）",
              b.proto.pending_gate_tasks(), ["a"])
    finally:
        b.cleanup()


def test_start_next_still_gated_by_pending_window() -> None:
    print("\n[90] start-next：待判窗口 + 下一片未声明触碰 → 不催（第 74 组口径不变）")
    b = Bench("probe")
    try:
        b.task.rmdir()                             # 先造「只有队列」的真空态
        (b.proto.dir / "QUEUE.md").write_text(
            "| # | 切片 | 状态 | 备注 |\n|---|---|---|---|\n"
            "| 1 | q9 | todo | |\n", encoding="utf-8")
        check("队列有未开工项、无人欠账 → PM 欠 start-next",
              b.proto.overall_debts().get("pm"), ["(队列):start-next"])
        a = b.proto.tasks / "a"
        a.mkdir()
        (a / "spec.md").write_text("# spec\n", encoding="utf-8")
        (a / "spec.json").write_text('{"round": 1}', encoding="utf-8")
        (a / "request.md").write_text("# 拆解\n", encoding="utf-8")
        (a / "delivered.json").write_text('{"round": 1}', encoding="utf-8")
        check("出现待判窗口 + 下一片没声明触碰 → 不再催（保守）",
              b.proto.overall_debts().get("pm"), None)
    finally:
        b.cleanup()


def test_consume_preempts_implement() -> None:
    print("\n[91] 别的片有没消费的 FAIL → 不开新片（返工优先）")
    b = Bench("a")
    try:
        b.advance_to("delivered")
        b.write("verdict.json", {"round": 1, "delivery": 1, "verdict": "FAIL"})
        (b.task / "touches.json").write_text('{"paths": ["bin/xteam"]}',
                                             encoding="utf-8")
        tb = b.proto.tasks / "b"
        tb.mkdir()
        (tb / "request.md").write_text("# 拆解\n", encoding="utf-8")
        (tb / "touches.json").write_text('{"paths": ["docs/*.md"]}', encoding="utf-8")
        check("FAIL 对准本次交付 → dev 欠 a 重做（不是 consume）",
              b.proto.debts(b.task).get("dev"), "implement")
        check("b 触碰不相交，但有没消费的 FAIL 压着 → 不给 implement",
              b.proto.debts(tb).get("dev"), None)
        b.write("consumed.json", {"round": 1})
        b.write("delivered.json", {"round": 2, "changed": ["bin/xteam"]})
        check("消费 + 重交 → a 转 TL chase（a 这轮不再是重做义务）",
              b.proto.debts(b.task).get("dev"), None)
        check("FAIL 已消费 → b 的 implement 放开",
              b.proto.debts(tb).get("dev"), "implement")
    finally:
        b.cleanup()


def test_subagent_capability_table_is_verified_only() -> None:
    print("\n[92] 只读子代理：只在实测表里的才给 ✓（不假装有能力）")
    check("omp 在表里 → ✓", subagent_capability(["omp"]), "omp ✓")
    check("没实测的 kind → ?（未实测）", subagent_capability(["agy"]),
          "agy ?（未实测）")
    check("多个 kind 按名字排序渲染",
          subagent_capability(["omp", "devin"]), "devin ?（未实测） / omp ✓")
    check("空列表 → 空串（怎么显示由调用方定）", subagent_capability([]), "")


class _FakeHerdr(Herdr):
    """门铃判据的替身：只覆盖外部边界（pane 读 / prompt / send-keys / agent 表）。

    形状全部照 2026-10-07 实测的 omp 18.x：空框 `╰─`、草稿 `╰─ <字>`、
    长消息被收成粘贴块 `╰─ txt #2`。
    """

    def __init__(self, tails: list[str], kind: str = "omp") -> None:
        self.tails = list(tails)
        self.kind = kind
        self.calls: list[tuple] = []

    def agents(self) -> list[dict]:
        return [{"pane_id": "wX:p1", "agent": self.kind}]

    def read_pane(self, pane_id: str, lines: int = 60, source: str = "recent") -> str:
        self.calls.append(("read", source))
        return self.tails.pop(0) if len(self.tails) > 1 else self.tails[0]

    def _prompt(self, target: str, message: str) -> tuple[int, str]:
        self.calls.append(("prompt", message))
        return 0, ""

    def _run(self, *args: str, timeout: int = 30) -> dict:
        self.calls.append(("run",) + args)
        return {}

    def agent_status(self, pane_id: str) -> str:
        return "working"


def test_box_state_reads_omp_input_line() -> None:
    print("\n[93] 输入框判据：omp 的 `╰─` 空框 / 草稿 / 粘贴块（实测形状）")
    empty = ("   Esc Working…\n"
             " >---6%-------------------------------------------------|--------------1M-\n"
             "╰─\n")
    draft = empty.replace("╰─\n", "╰─ HALF-TYPED 我在打这句话\n")
    chip = empty.replace("╰─\n", "╰─ txt #2\n")
    check("空框 → empty", _FakeHerdr([empty]).box_state("wX:p1"), "empty")
    check("草稿 → draft", _FakeHerdr([draft]).box_state("wX:p1"), "draft")
    check("粘贴块（长消息被收成 chip）→ draft",
          _FakeHerdr([chip]).box_state("wX:p1"), "draft")
    # 聚焦的 pane 会在输入行右侧挂提示（android_dev 实测形状）：那是提示不是草稿。
    # 0.2.6 把空框判成草稿 → 所有门铃被静默跳过，巡检还倒过来说「无响应」。
    focused = "╰─" + " " * 120 + "Shift+Tab to change thinking effort\n"
    check("聚焦时的右侧提示不算草稿（0.2.6 的假阳性）",
          _FakeHerdr([focused]).box_state("wX:p1"), "empty")
    focused_draft = ("╰─ 我在打这句话" + " " * 100
                     + "Shift+Tab to change thinking effort\n")
    check("有草稿 + 右侧提示 → draft",
          _FakeHerdr([focused_draft]).box_state("wX:p1"), "draft")
    check("认不出的 kind → unknown（不预检也不补键）",
          _FakeHerdr([draft], kind="devin").box_state("wX:p1"), "unknown")
    check("找不到提示符 → unknown",
          _FakeHerdr(["没有任何提示符\n"]).box_state("wX:p1"), "unknown")


def test_doorbell_guards_human_draft_and_retries_enter() -> None:
    print("\n[94] 门铃：框里有草稿不投；长消息成粘贴块才补一次回车")
    empty = "╰─\n"
    draft = "╰─ HALF-TYPED 我在打这句话\n"
    chip = "╰─ txt #2\n"
    long_msg = "行\n" * PASTE_CHIP_LINES        # ≥ 阈值：走「投完复核」路径
    short_msg = "一句话门铃"

    h = _FakeHerdr([draft])
    res = h.doorbell("wX:p1", short_msg, wait_empty_s=0.0)
    check("框里有草稿 → 返回 skipped-draft", res.startswith("skipped-draft"), True)
    check("草稿在时不投（没调 prompt）",
          [c for c in h.calls if c[0] == "prompt"], [])
    check("草稿在时也不补回车（绝不替人按）",
          [c for c in h.calls if c[0] == "run"], [])

    h = _FakeHerdr([empty, chip, empty])       # 投前空 → 投完是粘贴块 → 补键后提交
    res = h.doorbell("wX:p1", long_msg, wait_empty_s=0.0)
    check("长消息成粘贴块 → 补一次 send-keys enter",
          [c for c in h.calls if c[0] == "run"],
          [("run", "pane", "send-keys", "wX:p1", "enter")])
    check("提交成功 → 状态里没有 submit=stuck", "submit=stuck" in res, False)

    h = _FakeHerdr([empty, empty])             # 短消息：行内提交，框一直是空的
    h.doorbell("wX:p1", short_msg, wait_empty_s=0.0)
    check("短消息不复核（一次回车都不补）",
          [c for c in h.calls if c[0] == "run"], [])

    h = _FakeHerdr([empty, chip])              # 补了也不提交 → 如实报出，不装成功
    res = h.doorbell("wX:p1", long_msg, wait_empty_s=0.0)
    check("补回车也没提交 → submit=stuck", "submit=stuck" in res, True)
    check("最多补两次（不无限按）",
          len([c for c in h.calls if c[0] == "run"]), 2)

    h = _FakeHerdr([draft], kind="devin")      # 认不出的 kind：退回旧行为
    res = h.doorbell("wX:p1", short_msg, wait_empty_s=0.0)
    check("认不出的 kind 照投（不预检）",
          [c[0] for c in h.calls if c[0] == "prompt"], ["prompt"])
    check("认不出的 kind 不补键", [c for c in h.calls if c[0] == "run"], [])
    check("认不出的 kind 返回 status", res.startswith("status="), True)


def test_render_board_and_choice_detector() -> None:
    print("\n[95] 看板渲染 + 选择题识别（纯函数，不碰 herdr）")
    state = {
        "title": "xteam board  T  'demo'  /tmp/demo  （xteam 0.2.8）",
        "roles": [
            {"role": "pm", "status": "working", "held": "5m10s",
             "owes": "next-slice", "where": "console +14", "box": "empty"},
            {"role": "tl", "status": "blocked", "held": "3m10s",
             "owes": "none", "where": "", "box": "draft"},
        ],
        "alerts": ["⚠ tl 停在选择框上（等你答，不是做完）"],
        "slices": [{"name": "console-ui-commit", "stage": "settling",
                    "note": "未开工  pm:settle"}],
        "events": ["10-07 23:07:26 BLOCKED-ANSWER tl …"],
    }
    frame = render_board(state)
    check("标题在", "xteam board" in frame, True)
    check("角色行带义务与在哪", "next-slice @ console +14" in frame, True)
    check("输入框有草稿会标出来", "✍ 输入框有草稿" in frame, True)
    check("链路块在", "链路（为什么不动）" in frame, True)
    check("链路块带原因", "停在选择框上" in frame, True)
    check("切片块带阶段", "[settling]" in frame, True)
    check("事件块在", "BLOCKED-ANSWER" in frame, True)
    check("不上色时没有 ANSI", "\033[" in frame, False)
    check("上色时有 ANSI", "\033[" in render_board(state, color=True), True)
    check("空状态不炸", render_board({}).splitlines()[0], "xteam board")
    check("没有角色时说清是空的",
          "（没有在跑的角色）" in render_board({"roles": []}), True)

    # 实测形状：omp 的多选框（android_dev 里 tl 停的那个）。
    choice_tail = ("|   [ ] 应用管理                        |\n"
                   "|   [ ] Other (type your own)           |\n"
                   "| Space toggle · Enter next · Up/Down move · Esc cancel |\n")
    check("omp 的选择框 → 认出来", detect_choice_prompt(choice_tail), True)
    check("普通输出 → 不误判", detect_choice_prompt("正在跑测试…\n"), False)


def main() -> int:
    for fn in (
        test_chain_walks_one_role_at_a_time,
        test_unconsumed_verdict_keeps_dev_busy,
        test_round_regression_is_conservative,
        test_version_bump_carries_at_hundred,
        test_legacy_consumed_key_is_readable,
        test_closed_frees_everyone_and_prompts_next_slice,
        test_all_closed_without_report_pm_owes_report,
        test_fail_round_reopens_the_loop,
        test_multiple_tasks_accumulate_debts,
        test_idle_requires_both_signals,
        test_missing_pane_detected_and_cleared,
        test_alert_escalation_ladder,
        test_ensure_is_idempotent,
        test_tracker_records_last_active_and_state_since,
        test_alerted_suppressed_is_not_an_int,
        test_timestamp_formatting,
        test_role_spec_defaults_and_overrides,
        test_role_config_errors_are_loud,
        test_model_mechanism_depends_on_agent,
        test_agent_capability_table,
        test_preflight_rejects_before_side_effects,
        test_agreement_round_is_mandatory,
        test_tl_objection_reopens_the_discussion,
        test_blocked_forces_pm_to_act,
        test_parse_options_prefers_recommended,
        test_parse_options_falls_back_to_first,
        test_queue_recommends_head_when_unmarked,
        test_malformed_artifacts_never_crash_state_machine,
        test_env_preflight_layers,
        test_fake_working_is_detected,
        test_alert_ladder_exhaustion_must_escalate,
        test_index_summarizes_all_tasks,
        test_wiki_records_and_searches,
        test_project_docs_detected_but_untouched,
        test_openrouter_is_warning_not_block,
        test_model_listing_marks_openrouter,
        test_model_cost_risk_with_injected_catalog,
        test_multi_project_registry_and_gating,
        test_project_declared_in_spec_front_matter,
        test_repo_kind_declaration,
        test_swap_handover_and_config_persistence,
        test_swap_guard_blocks_while_working,
        test_qa_config_and_gate_trigger,
        test_probe_result_actually_changes_behavior,
        test_cross_project_dependencies_and_contracts,
        test_contracts_not_required_for_plain_projects,
        test_dep_gate_holds_after_decomposition,
        test_contract_boundary_and_failure_modes,
        test_slice_order_and_rules_staleness,
        test_model_name_sanity_filter,
        test_review_report_fixes,
        test_permission_auto_allow_policy,
        test_workspace_auto_detection,
        test_recap_path_and_alert_isolation,
        test_no_stale_path_literals,
        test_watchdog_token_leaks_and_crash,
        test_restarted_role_selfcheck_and_modelable_defaults,
        test_atomic_json_and_swap_transaction,
        test_recap_no_loop_and_next_slice_pushes,
        test_version_flag,
        test_qa_agent_whitelist,
        test_permission_path_boundary,
        test_cli_search_entry_does_not_crash,
        test_status_queue_count_matches_queue_items,
        test_blocked_answer_failure_escalates_not_answered,
        test_waiting_deps_excludes_cycles_and_typos,
        test_workspace_belongs_gate,
        test_herdr_missing_converges_and_never_swallows,
        test_prompt_blocked_roles_detection,
        test_confirm_delivery_conditional_enter,
        test_cli_surface_populated_through_main,
        test_nag_self_stale_three_states,
        test_recap_asked_set_converges,
        test_unstarted_queue_gate_for_pm_nags,
        test_status_merges_overall_synthetic_debts,
        test_say_recheck_window,
        test_gate_window_freezes_other_implement,
        test_shadowed_install_detection,
        test_say_three_state_render,
        test_queue_order_picks_slice_and_next_slice_label,
        test_pane_version_drift_warning,
        test_undispatched_tasks_threshold,
        test_identity_goes_into_system_prompt,
        test_context_pct_all_three_kinds,
        test_whoami_and_reopen_intro,
        test_charter_reply_timestamp_rule,
        test_phase_times_and_throughput_from_mtimes,
        test_phase_times_missing_file_and_backwards_mtime,
        test_touches_absent_means_conflict,
        test_conflict_glob_both_directions,
        test_overlap_allows_disjoint_implement,
        test_start_next_still_gated_by_pending_window,
        test_consume_preempts_implement,
        test_subagent_capability_table_is_verified_only,
        test_box_state_reads_omp_input_line,
        test_doorbell_guards_human_draft_and_retries_enter,
        test_render_board_and_choice_detector,
    ):
        fn()
    print()
    if FAILURES:
        print(f"✗ {len(FAILURES)} 条失败：{', '.join(FAILURES)}")
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
