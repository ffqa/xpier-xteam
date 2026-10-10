"""`xteam board` 的全屏界面（textual）。

被 `xteam_lib.run_board_tui()` 懒加载 —— **只有真的要看板时才 import textual**。
这个模块本身可以直接 import 来跑测试（需要 textual 装着）。

板面（用户 2026-10-10 定的）：
    顶：title（项目 + 三角色真状态一行）
    左：笔记 / 输入（想事情、安排任务，一键推给当前角色）
    中：三角色卡片（点或 Tab 切目标，选中卡高亮 + ◀ 发送目标）
    右：spec 档案（已闭合切片，Enter 翻开 spec 全文）
    底：状态（最后一次操作 + 链路告警，没有阻塞就说通畅）
"""

from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, Footer, Header, Input, Label,
                             ListItem, ListView, Static, TextArea)
from rich.panel import Panel
from rich.text import Text

try:                                     # 单测里直接 import 本模块时给个友好失败
    from xteam_lib import _box_state_text, _spec_view_lines
except ImportError:                      # pragma: no cover - 装在 lib/ 布局下
    from .xteam_lib import _box_state_text, _spec_view_lines  # type: ignore


class ArchiveList(ListView):
    """spec 档案列表。j/k 跟 vim 一致，Enter 翻开 —— 都是 ListView 自带动作的别名。

    放在列表自己身上而不是 App 上：App 级绑定会被聚焦中的 TextArea 吃掉，
    那人在笔记里打 j 就变成翻档案。**按键归谁，看焦点在谁身上。**
    """

    BINDINGS = [
        Binding("j", "cursor_down", "下一片"),
        Binding("k", "cursor_up", "上一片"),
        Binding("g", "scroll_home", "第一片"),
        Binding("G", "scroll_end", "最后一片"),
    ]


class SpecScreen(ModalScreen):
    """翻开一片 spec 的全文。Esc / q 关。"""

    BINDINGS = [
        Binding("escape,q", "dismiss", "关闭"),
        Binding("j,down", "scroll_down", "下一行"),
        Binding("k,up", "scroll_up", "上一行"),
        Binding("pagedown,f", "page_down", "下半页"),
        Binding("pageup,b", "page_up", "上半页"),
        Binding("g", "top", "回到顶部"),
        Binding("G", "bottom", "跳到末尾"),
    ]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="spec_dialog"):
            yield Label(self._title, id="spec_title")
            with VerticalScroll(id="spec_scroll"):
                yield Static(Text(self._body), id="spec_body")
            yield Label("Esc 合上 · j/k 滚行 · Ctrl+F/B 翻页 · g/G 顶末",
                        id="spec_hint")

    def action_scroll_down(self) -> None:
        self.query_one("#spec_scroll", VerticalScroll).scroll_down()

    def action_scroll_up(self) -> None:
        self.query_one("#spec_scroll", VerticalScroll).scroll_up()

    def action_page_down(self) -> None:
        self.query_one("#spec_scroll", VerticalScroll).scroll_page_down()

    def action_page_up(self) -> None:
        self.query_one("#spec_scroll", VerticalScroll).scroll_page_up()

    def action_top(self) -> None:
        self.query_one("#spec_scroll", VerticalScroll).scroll_home()

    def action_bottom(self) -> None:
        self.query_one("#spec_scroll", VerticalScroll).scroll_end()

    def on_mount(self) -> None:
        self.query_one("#spec_scroll", VerticalScroll).focus()

class BoardApp(App):
    """一屏看板：谁在干什么（真状态）、链路为什么不动、spec 档案随手翻。"""

    CSS = """
    Screen { layout: vertical; }
    #top { height: 3; }
    #body { height: 1fr; }
    #left { width: 34; border: round $primary; padding: 0 1; }
    #left Label { margin: 0 0 1 0; }
    #note { height: 1fr; border: none; }
    #left_buttons { height: 3; }
    #left_buttons Button { width: 1fr; }
    #cmd { margin: 1 0 0 0; }
    #middle { width: 1fr; border: round $accent; padding: 0 1; }
    .role_card { height: auto; border: round $panel; padding: 0 1; margin: 0 0 1 0; }
    .role_card.target { border: heavy yellow; background: $boost; }
    #right { width: 46; border: round $secondary; padding: 0 1; }
    #archive { height: 1fr; }
    #status { height: auto; min-height: 3; border: round $warning; padding: 0 1; }
    #alerts { height: auto; color: $warning; }
    #spec_dialog { align: center middle; width: 80%; height: 80%;
                  border: thick $accent; background: $surface; padding: 1 2; }
    #spec_scroll { height: 1fr; border: none; }
    #spec_hint { color: $text-muted; }
    """

    BINDINGS = [
        Binding("tab", "cycle_target", "切角色", priority=True),
        Binding("q", "quit_board", "退出"),
        Binding("r", "refresh_now", "刷新"),
        Binding("j,down", "archive_down", "档案下一片"),
        Binding("k,up", "archive_up", "档案上一片"),
        Binding("enter", "open_spec", "翻开 spec"),
        Binding("a", "quick_qa", "外部评审"),
    ]

    def __init__(self, root=None, proto=None, herdr=None, label="",
                 role_map=None, refresh_secs=2.0, collector=None,
                 dispatcher=None, qa_runner=None, rotator=None,
                 note_writer=None) -> None:
        super().__init__()
        self.root = root
        self.proto = proto
        self.herdr = herdr
        self.label = label
        self.role_map = role_map or {}
        self.refresh_secs = refresh_secs
        self._collector = collector
        self._dispatcher = dispatcher
        self._qa_runner = qa_runner
        self._rotator = rotator
        self._note_writer = note_writer
        self.state: dict = {}
        self.roles: list[str] = []
        self.target_idx = 0
        self.archive: list[dict] = []
        self.archive_idx = 0
        self.status_text = "就绪"
        self.status_kind = "info"
        self.alerts_text = None          # 状态区链路文案（可断言）
        self._card_texts: dict = {}      # 每张卡当前画的文本（可断言）
        self._archive_lines: list = []   # 档案当前的行（可断言）

    # ---- 组装 ----
    def compose(self) -> ComposeResult:
        yield Header(id="top")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Label("笔记 / 安排 —— 在这里想事情，写完推给当前角色")
                yield TextArea(id="note")
                with Horizontal(id="left_buttons"):
                    yield Button("推给当前角色", id="push", variant="primary")
                    yield Button("存为 note", id="note_save", variant="default")
                yield Input(id="cmd",
                            placeholder=":qa <切片> · :rotate <角色> · q 退出")
            with VerticalScroll(id="middle"):
                pass
            with Vertical(id="right"):
                yield Label("spec 档案（已闭合 · Enter 翻开全文）")
                yield ListView(id="archive")
        with Vertical(id="status"):
            yield Label("", id="alerts")
            yield Label("", id="statusline")

    def on_mount(self) -> None:
        self.title = "xteam Board"
        self.sub_title = str(self.label)
        self._collect()
        self.set_interval(max(0.05, self.refresh_secs), self._collect)

    # ---- 采集（herdr 调用放到工作线程，不让界面卡） ----
    @work(thread=True, exclusive=True)
    def _collect(self) -> None:
        try:
            st = self._collector()
        except Exception as exc:                  # noqa: BLE001
            self.call_from_thread(self._set_status, f"采集失败: {exc}", "error")
            return
        self.call_from_thread(self._apply, st)

    def _apply(self, st: dict) -> None:
        self.state = st or {}
        roles = [r.get("role", "") for r in self.state.get("roles") or []]
        if roles != self.roles:
            self.roles = roles
            self._rebuild_role_cards()
        else:
            self._refresh_role_cards()
        self.archive = list(self.state.get("closed") or [])
        if not self.archive:
            self.archive_idx = 0
        else:
            self.archive_idx = max(0, min(self.archive_idx, len(self.archive) - 1))
        self._refresh_archive()
        self._refresh_header()

    # ---- 顶部 title ----
    def _refresh_header(self) -> None:
        chips = []
        for r in self.state.get("roles") or []:
            st = str(r.get("status", "idle"))
            mark = "◀" if r.get("role") == self._target() else " "
            chips.append(f"{mark}{r.get('role','')} {st}"
                         f"{(' ' + str(r.get('held'))) if r.get('held') and r['held'] != '-' else ''}")
        hud = " · ".join(chips) if chips else "（没有在跑的角色）"
        alerts = self.state.get("alerts") or []
        self.alerts_text = (Text("链路：" + " ；".join(str(a) for a in alerts),
                                style="bold yellow") if alerts
                            else Text("链路通畅，没有阻塞", style="dim"))
        try:
            self.query_one("#alerts", Label).update(self.alerts_text)
            self.query_one("#statusline", Label).update(
                Text(f"[{self.status_kind}] {self.status_text}",
                     style={"success": "green", "error": "red",
                            "warn": "yellow"}.get(self.status_kind, "cyan")))
        except Exception:
            return                      # 界面还没装完（首次采集就跑进来了）

    # ---- 中间：三角色 ----
    def _target(self) -> str:
        if not self.roles:
            return "pm"
        return self.roles[self.target_idx % len(self.roles)]

    def _rebuild_role_cards(self) -> None:
        middle = self.query_one("#middle", VerticalScroll)
        middle.remove_children()
        for r in self.state.get("roles") or []:
            middle.mount(Static(id=f"card_{r.get('role','')}", classes="role_card"))
        # mount() 不 await：卡片这一刻还没进树，立刻填内容会 query 不到。
        # 等这一次刷新完成再填 —— 首帧才不会是一片空卡片。
        self.call_after_refresh(self._refresh_role_cards)

    def _refresh_role_cards(self) -> None:
        target = self._target()
        for r in self.state.get("roles") or []:
            name = str(r.get("role", ""))
            try:
                card = self.query_one(f"#card_{name}", Static)
            except Exception:
                continue
            is_target = name == target
            card.set_classes("role_card target" if is_target else "role_card")
            st = str(r.get("status", "idle"))
            style = {"working": "green", "blocked": "red",
                     "done": "cyan"}.get(st, "white")
            t = Text()
            if is_target:
                t.append("◀ 发送目标\n", style="bold black on yellow")
            t.append(f"{name}  ", style="bold")
            t.append(st, style=f"bold {style}")
            if r.get("held") and r["held"] != "-":
                t.append(f"  {r['held']}")
            t.append("\n")
            doing = str(r.get("activity_title") or "").strip()
            t.append(doing if doing else "（没有活动标题）", style="italic")
            t.append("\n欠 ")
            owes = str(r.get("owes") or "none")
            t.append(owes, style="cyan")
            if r.get("where"):
                t.append(f" @ {r['where']}")
            box = _box_state_text(str(r.get("box") or ""))
            if box:
                t.append(f"\n{box}", style="yellow")
            self._card_texts[name] = t
            card.update(Panel(t, title=name, border_style="yellow" if is_target else "white"))

    # ---- 右侧：spec 档案 ----
    def _refresh_archive(self) -> None:
        try:
            lv = self.query_one("#archive", ListView)
        except Exception:
            return                      # 界面还没装完（首次采集就跑进来了）
        lv.clear()
        self._archive_lines = []
        for i, c in enumerate(self.archive):
            verdict = str(c.get("verdict") or "PASS")
            title = str(c.get("title") or "")
            mark = "▶ " if i == self.archive_idx else "  "
            line = f"{mark}{c.get('time','')}  {verdict}  {c.get('name','')}"
            if title:
                line += f"  {title}"
            self._archive_lines.append(line)
            lv.append(ListItem(Label(line)))
        if self.archive:
            try:
                lv.index = self.archive_idx
            except Exception:
                pass

    # ---- 动作 ----
    def action_cycle_target(self) -> None:
        if not self.roles:
            return
        self.target_idx = (self.target_idx + 1) % len(self.roles)
        self._refresh_role_cards()
        self._refresh_header()
        self._set_status(f"发送目标 → {self._target()}", "info")

    def action_refresh_now(self) -> None:
        self._collect()

    def action_archive_down(self) -> None:
        if self.archive:
            self.archive_idx = (self.archive_idx + 1) % len(self.archive)
            self._refresh_archive()

    def action_archive_up(self) -> None:
        if self.archive:
            self.archive_idx = (self.archive_idx - 1) % len(self.archive)
            self._refresh_archive()

    def action_open_spec(self) -> None:
        if not self.archive:
            return
        entry = self.archive[self.archive_idx]
        self._open_spec(str(entry.get("name") or ""))

    def action_quick_qa(self) -> None:
        self.query_one("#cmd", Input).value = ":qa "

    def action_quit_board(self) -> None:
        self.exit()

    def _open_spec(self, name: str) -> None:
        title, lines = _spec_view_lines(self.proto.dir / "tasks" / name, name)
        self.push_screen(SpecScreen(title, "\n".join(lines)))

    def _set_status(self, text: str, kind: str = "info") -> None:
        self.status_text = text
        self.status_kind = kind
        try:
            self.query_one("#statusline", Label).update(
                Text(f"[{kind}] {text}",
                     style={"success": "green", "error": "red",
                            "warn": "yellow"}.get(kind, "cyan")))
        except Exception:
            pass

    # ---- 事件 ----
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "archive" and self.archive:
            self.archive_idx = event.list_view.index
            self.action_open_spec()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        # 只记索引，**不重建列表** —— 重建会改 index，又会抛 Highlighted，
        # 那是自己递归自己。高亮由 ListView 自己画。
        if event.list_view.id == "archive" and self.archive:
            if 0 <= event.list_view.index < len(self.archive):
                self.archive_idx = event.list_view.index

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "push":
            self._push_note()
        elif event.button.id == "note_save":
            self._save_note()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        cmd = event.value.strip()
        event.input.value = ""
        if not cmd:
            return
        if cmd in (":q", ":quit", ":exit"):
            self.exit()
            return
        if cmd in (":r", ":refresh"):
            self.action_refresh_now()
            self._set_status("已刷新", "success")
            return
        if cmd.startswith(":qa"):
            self._run_qa(cmd[3:].strip())
            return
        if cmd.startswith(":rotate"):
            self._rotate(cmd[7:].strip())
            return
        # 不带冒号也当成「推给当前角色」—— 左边就是干这个的
        self._dispatch(cmd)

    def _push_note(self) -> None:
        note = self.query_one("#note", TextArea).text.strip()
        if not note:
            self._set_status("左边是空的，没什么可推", "warn")
            return
        self._dispatch(note)
        self.query_one("#note", TextArea).text = ""

    def _save_note(self) -> None:
        note = self.query_one("#note", TextArea).text.strip()
        if not note:
            self._set_status("左边是空的，没什么可存", "warn")
            return
        try:
            path = self._note_writer(note)
            self._set_status(f"已存为 note：{path.name}", "success")
            self.query_one("#note", TextArea).text = ""
        except Exception as exc:                  # noqa: BLE001
            self._set_status(f"存 note 失败: {exc}", "error")

    def _dispatch(self, msg: str) -> None:
        role = self._target()
        self._set_status(f"正在推给 {role}…", "info")

        def _do() -> None:
            try:
                ok, reason = self._dispatcher(role, msg)
            except Exception as exc:              # noqa: BLE001
                ok, reason = False, str(exc)
            self.call_from_thread(
                self._set_status,
                (f"✓ 已推给 {role}: {reason}" if ok else f"⚠ {role} 没收到: {reason}"),
                "success" if ok else "warn")

        # **不能用嵌套的 @work** —— 那个装饰器只在类定义期生效，运行时套在内层
        # 函数上什么都不会跑（门铃就静默消失了）。run_worker 才是运行时入口。
        self.run_worker(_do, thread=True, exclusive=True, group="dispatch")

    def _run_qa(self, slice_name: str) -> None:
        if not slice_name:
            self._set_status(":qa 要带切片名，例：:qa login-api", "warn")
            return
        self._set_status(f"正在外部评审 {slice_name}…", "info")

        def _do() -> None:
            try:
                ok, note = self._qa_runner(slice_name)
            except Exception as exc:              # noqa: BLE001
                ok, note = False, str(exc)
            self.call_from_thread(
                self._set_status,
                f"{'✓' if ok else '✗'} 外部评审 {slice_name}: {note[:120]}",
                "success" if ok else "error")

        self.run_worker(_do, thread=True, exclusive=True, group="qa")

    def _rotate(self, role: str) -> None:
        role = role or self._target()
        self._set_status(f"正在轮换 {role} 会话…", "info")

        def _do() -> None:
            try:
                ok, msg = self._rotator(role)
            except Exception as exc:              # noqa: BLE001
                ok, msg = False, str(exc)
            self.call_from_thread(
                self._set_status,
                f"{'✓' if ok else '⚠'} 轮换 {role}: {msg}",
                "success" if ok else "error")

        self.run_worker(_do, thread=True, exclusive=True, group="rotate")

    def on_click(self, event) -> None:
        # 鼠标点角色卡 = 把它设为发送目标（点两下正是用户想要的）。
        # 用 event.widget 而不是坐标反查：坐标是相对屏幕的，反查要先做偏移换算，
        # Pilot 与真终端给的坐标语义还不一样，一换就全点歪。
        card = getattr(event, "widget", None)
        while card is not None and not (isinstance(card, Static)
                                        and "role_card" in card.classes):
            card = card.parent
        if card is not None and card.id and card.id.startswith("card_"):
            name = card.id[5:]
            if name in self.roles:
                self.target_idx = self.roles.index(name)
                self._refresh_role_cards()
                self._refresh_header()
                self._set_status(f"发送目标 → {name}", "info")


def run_board(root: Path, proto, herdr, label: str, role_map: dict,
              refresh_secs: float = 2.0, collector=None, dispatcher=None,
              qa_runner=None, rotator=None, note_writer=None) -> bool:
    """起板子（textual 已由调用方确认可 import）。"""
    BoardApp(root=root, proto=proto, herdr=herdr, label=label, role_map=role_map,
             refresh_secs=refresh_secs, collector=collector, dispatcher=dispatcher,
             qa_runner=qa_runner, rotator=rotator, note_writer=note_writer).run()
    return True
