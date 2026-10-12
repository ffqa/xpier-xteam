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
                             MarkdownViewer,
                             ListItem, ListView, Static, TextArea)
from rich.text import Text

try:                                     # 单测里直接 import 本模块时给个友好失败
    from xteam_lib import (_box_state_text, wrap_display, read_doc,
                           doc_files_in, project_doc_files, render_doc_markdown)
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


class DocsScreen(ModalScreen):
    """文档翻阅层：左边一份目录，右边正文。

    **不只 spec** —— 一片切片的 request（当时怎么说的）、closed（怎么收的）、
    verdict（谁拍的板）同样是记忆；加上项目级 README / 协议 / 队列 / 报告，
    以及「存为 note」落下去的 wiki 笔记。j/k 在目录里选，正文跟着换。
    """

    BINDINGS = [
        Binding("escape,q", "dismiss", "关闭"),
        Binding("j,down", "next_doc", "下一篇"),
        Binding("k,up", "prev_doc", "上一篇"),
        Binding("pagedown,f", "page_down", "下半页"),
        Binding("pageup,b", "page_up", "上半页"),
        Binding("g", "top", "回到顶部"),
        Binding("G", "bottom", "跳到末尾"),
    ]

    def __init__(self, title: str, files: list) -> None:
        super().__init__()
        self._title = title
        self._files = list(files)
        self._idx = 0

    def compose(self) -> ComposeResult:
        with Horizontal(id="docs_dialog"):
            with Vertical(id="docs_left"):
                yield Label(self._title, id="docs_title")
                yield Label("文档（这一片的 + 项目级）· j/k 切换", id="docs_sub")
                yield ArchiveList(id="docs_list")
            with Vertical(id="docs_right"):
                # Markdown 直接渲染：标题/列表/代码块/表格都按排版显示，
                # 不再把 `#`、`-`、`**` 这些源码符号怼到人眼前。
                # mermaid 代码块已由 render_doc_markdown() 转成 ASCII 图，
                # 所以它也走同一条渲染路（图本身就是普通代码块）。
                yield MarkdownViewer("", id="docs_body", show_table_of_contents=False)
                yield Label("j/k 选文档 · g/G 顶末 · Esc 合上",
                            id="docs_hint")

    def on_mount(self) -> None:
        lv = self.query_one("#docs_list", ArchiveList)
        for label, path in self._files:
            lv.append(ListItem(Label(label)))
        if self._files:
            lv.index = 0
        lv.focus()
        self._load(0)

    def _load(self, idx: int) -> None:
        viewer = self.query_one("#docs_body", MarkdownViewer)
        if not self._files:
            viewer.document.update("（没有可翻的文档）")
            return
        self._idx = max(0, min(idx, len(self._files) - 1))
        _label, path = self._files[self._idx]
        title, lines = read_doc(path)
        width = max(30, (self.size.width or 100) - 44)
        body = render_doc_markdown("\n".join(lines), width=width)
        # 文档名单独一行（MarkdownViewer 自己会渲染正文，标题行只是给人定位）
        viewer.document.update(f"**{title}**\n\n{body}")

    def action_next_doc(self) -> None:
        self._load(self._idx + 1)
        self.query_one("#docs_list", ArchiveList).index = self._idx

    def action_prev_doc(self) -> None:
        self._load(self._idx - 1)
        self.query_one("#docs_list", ArchiveList).index = self._idx

    def action_page_down(self) -> None:
        self.query_one("#docs_body", MarkdownViewer).scroll_page_down()

    def action_page_up(self) -> None:
        self.query_one("#docs_body", MarkdownViewer).scroll_page_up()

    def action_top(self) -> None:
        self.query_one("#docs_body", MarkdownViewer).scroll_home()

    def action_bottom(self) -> None:
        self.query_one("#docs_body", MarkdownViewer).scroll_end()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "docs_list":
            self._load(event.list_view.index)


def _flat_button(label: str, **kw) -> "Button":
    """不抢焦点的扁平按钮。

    **为什么必须不可聚焦**：焦点留在按钮上时，j/k 被 Button 的消息处理吃掉，
    用户翻右侧档案突然全线失灵（实测：点了「推给 pm」之后按 j 无反应）。
    textual 的 CSS 没有 can-focus 这个属性，只能在代码里关。
    """
    btn = Button(label, **kw)
    btn.can_focus = False
    return btn


class ActionMenu(ModalScreen):
    """动作菜单：一个列表，选中即执行。

    kander 的终端 TUI 没有顶部菜单栏，入口全是单键 + 上下文动作（`a` 派 agent、
    `g` 送 hook）。这里照它的形状做两个：
      · `o` —— 对**当前选中对象**的动作（档案里选中的那片，或正在看的目标角色）
      · `m` —— 全局动作（刷新 / 项目文档 / 退出）
    单键能做到的事不放菜单，菜单只放"要选一下"的事。
    """

    BINDINGS = [
        Binding("escape,q", "dismiss", "关闭"),
        Binding("j,down", "cursor_down", "下一项"),
        Binding("k,up", "cursor_up", "上一项"),
    ]

    def __init__(self, title: str, items: list) -> None:
        """items: [(标签, 回调)]。回调在选中时同步调用，然后关掉菜单。"""
        super().__init__()
        self._title = title
        self._items = list(items)

    def compose(self) -> ComposeResult:
        with Vertical(id="menu_dialog"):
            yield Label(self._title, id="menu_title")
            # 用 ArchiveList（ListView 子类，带 j/k/g/G）—— 普通 ListView 不认
            # j/k，菜单就只剩方向键能走，键盘用户直接卡住
            yield ArchiveList(id="menu_list")
            yield Label("j/k 选 · Enter 执行 · Esc 关", id="menu_hint")

    def on_mount(self) -> None:
        lv = self.query_one("#menu_list", ArchiveList)
        for label, _cb in self._items:
            lv.append(ListItem(Label(label)))
        if self._items:
            lv.index = 0
        lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        i = event.list_view.index
        if 0 <= i < len(self._items):
            _label, cb = self._items[i]
            self.dismiss()
            cb()


class CheatSheet(ModalScreen):
    """键位速查表。没有菜单栏，就必须有一屏把所有键列全 —— 这是 kander 的 `?`。"""

    BINDINGS = [Binding("escape,q,?", "dismiss", "关闭")]

    def __init__(self, rows: list) -> None:
        super().__init__()
        self._rows = rows

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="cheat_dialog"):
            yield Label("键位（? 或 Esc 关）", id="cheat_title")
            for left, right in self._rows:
                yield Label(f"  {left:<14} {right}")

    def on_mount(self) -> None:
        self.query_one("#cheat_dialog", VerticalScroll).focus()


class BoardApp(App):
    """一屏看板：谁在干什么（真状态）、链路为什么不动、spec 档案随手翻。"""

    CSS = """
    Screen { layout: vertical; }
    /* 顶栏压成一行：只放三角色真状态。textual 自带 Header 的时钟与加载图标
       没有信息量，还白占 3 行。 */
    #top { height: 1; background: $primary; color: $text; }
    #body { height: 1fr; }
    /* 宽度分配：中间是主角（三角色），左右是辅助。右侧档案行短，不需要 46 列。 */
    /* 左栏：一行一行的紧凑排布。 Button 默认 height 3 + line-pad 1，
       在 30 列里就是三面空白围一个字 —— 按钮因此看起来巨大。全部压成一行。 */
    #left { width: 30; border: round $primary; padding: 0 1; }
    #left Label { margin: 0 0 1 0; }
    /* 笔记区要「看起来是能写字的地方」—— 给个框。同时限高：1fr 会把下面的
       命令输入行挤出屏幕（左栏里它排最后）。 */
    #note { height: 1fr; min-height: 6; max-height: 16;
            border: round $panel; padding: 0 1; }
    #row_actions { height: auto; }
    /* line-pad 不接受 0（textual 只认正整数），横向留 1 列由它负责。 */
    #row_actions Button { width: 1fr; min-width: 0; height: 1;
                          border: none; padding: 0; background: $boost; }
    #row_actions Button:hover { background: $primary; }
    #qa { width: 1fr; min-width: 0; height: 1; border: none;
          padding: 0; background: $warning; color: $text; }
    #qa:hover { background: $warning; }
    /* 命令输入一行就够：它只接受 :qa / :rotate / :respawn / :q 这种短命令，
       长话在上面的笔记区写（那里会换行）。Input 默认高 3 行带边框，一行足矣。 */
    #cmd { height: 1; border: none; padding: 0 1; background: $surface;
           margin: 1 0 0 0; }
    /* 中间不套外框：卡片自己就是盒子。双层边框把每张卡都挤小一圈。 */
    #middle { width: 1fr; min-width: 52; padding: 0 1; }
    .role_card { height: auto; border: round $panel; padding: 0 1; margin: 0 0 1 0;
                 border-title-align: left; border-title-color: $text; }
    /* 目标卡：重边框 + 反色，切角色时一眼能看到是谁 */
.role_card.target { border: heavy yellow; background: $boost;
                        border-title-color: $text; }
    /* pane 挂了的卡：红虚线框 + 深一点的底。别用 $error-darken-3 这种变量名 ——
       textual 的主题里没有它，整个样式表会解析失败（板子直接起不来）。 */
    .role_card.dead { border: dashed $error; background: $boost; }
    /* 卡内跳转钮：一行、无框、靠右。存在感低但找得到 —— 它是个快捷方式，
       不是这个卡的主要动作（主要动作是点卡选中/拉起）。 */
    .role_card .jump { width: 1fr; min-width: 0; height: 1; border: none;
                       padding: 0; background: $panel; color: $text-muted; }
    .role_card .jump:hover { background: $primary; color: $text; }
    #right { width: 34; border: round $secondary; padding: 0 1; }
    #archive { height: 1fr; }
    #status { height: auto; min-height: 3; border: round $warning; padding: 0 1; }
    #alerts { height: auto; color: $warning; }
    #statusline { height: auto; }
    /* 全屏、无粗边框：它是一层「视图」，不是弹窗。粗边框 + 88% 尺寸看起来
       既没铺满又糊着一圈。 */
    #docs_dialog { width: 100%; height: 100%; border: none;
                   background: $surface; padding: 0 1; }
    #docs_left { width: 32; border: round $panel; }
    #docs_title { margin: 0 0 0 0; text-style: bold; }
    #docs_sub { color: $text-muted; margin: 0 0 1 0; }
    #docs_list { height: 1fr; }
    #docs_right { width: 1fr; border: none; padding: 0 1; }
    #docs_body { height: 1fr; border: none; background: $surface; }
    #docs_hint { color: $text-muted; }
    /* 动作菜单 / 速查表：居中一块，别铺满（它们是被召唤出来的，铺满会抢主视野） */
    #menu_dialog { align: center middle; width: 56%; height: auto; max-height: 80%;
                   border: round $accent; background: $surface; padding: 1 2; }
    #menu_title { text-style: bold; margin: 0 0 1 0; }
    #menu_list { height: auto; max-height: 16; border: none; }
    #menu_hint { color: $text-muted; margin: 1 0 0 0; }
    #cheat_dialog { align: center middle; width: 62%; height: 82%;
                    border: round $accent; background: $surface; padding: 1 2; }
    #cheat_title { text-style: bold; margin: 0 0 1 0; }
    """

    # 只放「任何时候都该是这个意思」的键。
    # · tab 必须 priority：textual 默认把 Tab 给 focus_next，不抢等于没绑；
    # · q 不放这：焦点在卡片上时按 q 就退板子太容易误触，退出走 Ctrl+Q / Ctrl+C；
    # · j/k/enter 也不放这：它们归 ArchiveList（见下），放 App 级会被聚焦中的
    #   输入框吃掉 —— 人在笔记里打个 j 就变成翻档案。
    # 只放「任何时候都该是这个意思」的键。
    # · tab 必须 priority：textual 默认把 Tab 给 focus_next，不抢的话人按 Tab 只是
    #   光标跳格子。**代价是键盘焦点再也去不了右侧档案列表**，所以下面用 j/k/o/p
    #   把档案操作补回来（它们在笔记区聚焦时会被 TextArea 吃掉，不影响打字）。
    # · q 不放这：焦点在卡片上按 q 就退板子太容易误触，退出走 Ctrl+Q / Ctrl+C。
    BINDINGS = [
        Binding("tab", "cycle_target", "切角色", priority=True),
        Binding("ctrl+q", "quit_board", "退出"),
        Binding("ctrl+r", "refresh_now", "刷新"),
        Binding("j,down", "archive_down", "档案下一片"),
        Binding("k,up", "archive_up", "档案上一片"),
        Binding("o", "open_spec", "翻开这片文档"),
        Binding("p", "open_project_docs", "项目文档/笔记"),
        Binding("question_mark", "cheat_sheet", "键位速查"),
        Binding("m", "global_menu", "全局菜单"),
        Binding("bracket_right", "next_team", "下一组"),
        Binding("bracket_left", "prev_team", "上一组"),
        Binding("O", "object_menu", "对选中片的动作"),
    ]

    def __init__(self, root=None, proto=None, herdr=None, label="",
                 role_map=None, refresh_secs=2.0, collector=None,
                 dispatcher=None, qa_runner=None, rotator=None,
                 note_writer=None, bring_up=None, respawn=None,
                 teams=None, focus_pane=None) -> None:
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
        self._bring_up = bring_up
        self._respawn = respawn
        self._focus_pane = focus_pane
        self.teams = teams or [{"name": "main"}]
        self.active_team = self.teams[0]["name"]
        self.slices_by_team: dict = {}
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
        self._archive_sig: tuple = ()    # 档案内容指纹：没变就不重画（防闪）
        self._archive_n: int = -1        # 上次真的 extend 了几条（防重复重建）
        self.status_text_lines: list = []  # 状态行折行后的文本（可断言）

    # ---- 组装 ----
    def compose(self) -> ComposeResult:
        # 顶部一条：三角色真状态。textual 的 Header 占 3 行，其中时钟和加载图标
        # 没有用 —— 省下的两行给内容。
        yield Static("", id="top")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Label("笔记 / 安排")
                yield TextArea(id="note")
                # 并排的两个只放短标签（30 列里「推给当前角色」六个字就 12 列，
                # 两个并排一定被切），外部评审单独一整行
                with Horizontal(id="row_actions"):
                    yield _flat_button("推给 pm", id="push", variant="primary")
                    yield _flat_button("存 note", id="note_save")
                yield _flat_button("外部评审 :qa", id="qa")
                yield Input(id="cmd",
                            placeholder=":qa <切片> · :respawn <角色> · :q")
            with VerticalScroll(id="middle"):
                pass
            with Vertical(id="right"):
                yield Label("spec 档案（Enter 翻开全文）")
                yield ArchiveList(id="archive")
        with Vertical(id="status"):
            yield Label("", id="alerts")
            yield Label("", id="statusline")

    def on_mount(self) -> None:
        self.title = "xteam Board"
        self.sub_title = str(self.label)
        self._collect(self.active_team)
        self.set_interval(max(0.05, self.refresh_secs),
                          lambda: self._collect(self.active_team))
        # 起始焦点给档案列表，**不是笔记区**：textual 默认聚焦第一个可聚焦控件，
        # 而那是 TextArea —— 它会吞掉所有可打印键，j/k/o/p 就全废了。
        # 要写字点一下笔记区即可，聚焦是显式动作。
        try:
            self.query_one("#archive", ArchiveList).focus()
        except Exception:
            pass
        # 中间区只是卡片的滚动容器，不该抢焦点：点卡里的按钮时，textual 会把焦点
        # 给「最近的可聚焦祖先」，那就是它 —— 于是焦点从档案列表跑掉，j/k 失灵。
        try:
            self.query_one("#middle", VerticalScroll).can_focus = False
        except Exception:
            pass

    # ---- 采集（herdr 调用放到工作线程，不让界面卡） ----
    @work(thread=True, exclusive=True)
    def _collect(self, team: str = "") -> None:
        try:
            st = (self._collector(team) if self._collector else {})
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
        # 在飞切片按 team 归类（task.json/spec 的 team 字段，没有就算 main）
        self.slices_by_team = {}
        for s in self.state.get("slices") or []:
            nm = str(s.get("team") or "main")
            self.slices_by_team.setdefault(nm, []).append(s)
        if len(self.teams) > 1 and self.active_team not in self.slices_by_team:
            for nm in self.slices_by_team:
                self.active_team = nm
                break
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
        # 角色一行概览放顶栏副标题 —— 这是「一眼看到谁在干什么」的地方，
        # 之前算出来了却没往上放，纯属白算。
        self.sub_title = hud
        alerts = self.state.get("alerts") or []
        body = "链路：" + " ；".join(str(a) for a in alerts) if alerts else "链路通畅，没有阻塞"
        # 长句折行，不截断 —— 告警被切掉的半句往往正是要看的半句
        width = max(20, (self.size.width or 100) - 6)
        self.alerts_text = Text("\n".join(wrap_display(body, width)),
                                style="bold yellow" if alerts else "dim")
        status = f"[{self.status_kind}] {self.status_text}"
        self.status_text_lines = wrap_display(status, width)
        try:
            strip = ""
            if len(self.teams) > 1:
                # 多 team：一条组带（每组 working/idle/blocked + 在飞片数），
                # 当前组高亮。单 team 项目连这行都不出。
                parts = []
                for t in self.teams:
                    nm = t["name"]
                    mark = "◀" if nm == self.active_team else " "
                    n_flight = sum(1 for c in self.slices_by_team.get(nm, []))
                    parts.append(f"{mark}{nm}({n_flight})")
                strip = " ┃ team: " + " ".join(parts)
            self.query_one("#top", Static).update(
                Text(f" xteam Board · {self.label} ┃ {hud}{strip}"))
            self.query_one("#alerts", Label).update(self.alerts_text)
            self.query_one("#statusline", Label).update(
                Text("\n".join(self.status_text_lines),
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
            role = str(r.get("role", ""))
            card = Vertical(classes="role_card", id=f"card_{role}")
            middle.mount(card)
        # mount() 不 await：卡片这一刻还没进树，立刻填内容会 query 不到。
        # 等这一次刷新完成再填 —— 首帧才不会是一片空卡片。
        self.call_after_refresh(self._fill_role_cards)

    def _fill_role_cards(self) -> None:
        """给每张卡装上「文本 + 跳到 pane」两个子件（只在卡片空着时装）。"""
        # **装完要把焦点还回去**：Button 是可聚焦的，mount 它会抢焦点 ——
        # 用户正用 j/k 翻右侧档案，一次刷新之后焦点跑到按钮上，按键全线失灵
        # （实测：full-suite 里必现，单独跑不现，因为装配是延迟一帧做的）。
        was_focused = self.focused
        for r in self.state.get("roles") or []:
            role = str(r.get("role", ""))
            try:
                card = self.query_one(f"#card_{role}", Vertical)
            except Exception:
                continue
            if card.children:
                continue
            # 卡片容器也不可聚焦：点卡里的跳转钮时，textual 会把焦点挪到
            # 「最近的可聚焦祖先」，那就是这张卡 —— 焦点一样会跑掉。
            card.can_focus = False
            card.mount(Static(id=f"text_{role}"))
            card.mount(_flat_button("⇥ 跳到 pane", id=f"jump_{role}",
                                    classes="jump"))
        if was_focused is not None:
            try:
                self.set_focus(was_focused)
            except Exception:
                pass

    def _refresh_role_cards(self) -> None:
        target = self._target()
        for r in self.state.get("roles") or []:
            name = str(r.get("role", ""))
            try:
                card = self.query_one(f"#card_{name}", Vertical)
            except Exception:
                continue
            is_target = name == target
            classes = "role_card target" if is_target else "role_card"
            # pane 不在了：卡片换个颜色，副标题直接写「点卡片拉起」
            dead = not str(r.get("pane") or "")
            if dead:
                classes += " dead"
            card.set_classes(classes)
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
            if doing:
                t.append(doing, style="italic")
                t.append("\n")
            t.append("欠 ")
            owes = str(r.get("owes") or "none")
            t.append(owes, style="cyan")
            if r.get("where"):
                t.append(f" @ {r['where']}")
            # 「输入框状态未知」是噪音，不打印 —— 卡片上每一行都要有用
            box = _box_state_text(str(r.get("box") or ""))
            if box and box != "输入框状态未知":
                t.append(f"\n{box}", style="yellow")
            # 一层盒子：边框由 CSS 画（`.role_card`），角色名走 border_title。
            # 之前是 CSS 边框 + Panel 边框各一层 —— 双层边框把每张卡都显小一圈。
            self._card_texts[name] = t
            card.border_title = name
            card.border_subtitle = ("pane 挂了 · 点卡片拉起 · :respawn " + name + " 换 agent"
                                    if dead else "")
            try:
                self.query_one(f"#text_{name}", Static).update(t)
            except Exception:
                pass

    # ---- 右侧：spec 档案 ----
    def _archive_signature(self) -> tuple:
        """档案内容的指纹。用来判断「要不要重画」—— 不判断的话每次状态刷新
        （默认 2 秒一次）都把整个 ListView clear() 再 append() 一遍，右侧就在人眼前
        一直闪。内容没变就一行都不该动。"""
        return tuple((str(c.get("time", "")), str(c.get("verdict", "")),
                      str(c.get("name", "")), str(c.get("title", "")))
                     for c in self.archive)

    def _refresh_archive(self) -> None:
        try:
            lv = self.query_one("#archive", ListView)
        except Exception:
            return                      # 界面还没装完（首次采集就跑进来了）
        sig = self._archive_signature()
        # 用「上次真的 extend 了几条」判断建没建过，**不看 lv.children** ——
        # extend() 也是异步的，children 会 lag，拿它当判据会导致每次刷新都重建，
        # 而重建窗口里落下的按键会被整份吃掉（实测：j 偶发失效）。
        if sig == self._archive_sig and self._archive_n == len(self.archive):
            # 内容没变：一行都不动。**尤其不许把 index 设回 archive_idx** ——
            # ListView 的光标才是用户看到的焦点，强行同步会把刚按的 j/k 吃回去。
            # 反过来：以 ListView 的 index 为准，校正 archive_idx。
            if self.archive:
                try:
                    if 0 <= lv.index < len(self.archive):
                        self.archive_idx = lv.index
                except Exception:
                    pass
            return
        # 重建前记住两样东西：光标位置，和**键盘焦点在不在列表上**。
        # clear() 会移除子节点，焦点跟着丢 —— 用户正在用 j/k 翻档案，等一次刷新
        # （默认 2 秒）之后按键突然全线失灵，直到他再点一下列表。这是真 bug，
        # 不是测试瑕疵：实测复现，focus() 之后一次刷新就静默失效。
        try:
            prev_index = lv.index
        except Exception:
            prev_index = None
        had_focus = bool(lv.has_focus
                         or any(c.has_focus for c in lv.children))
        self._archive_sig = sig
        lv.clear()
        self._archive_lines = []
        items = []
        for c in self.archive:
            verdict = str(c.get("verdict") or "PASS")
            title = str(c.get("title") or "")
            # 不在行文本里画 ▶：选中光标由 ListView 自己画，两份真相必然打架
            line = f"{c.get('time','')}  {verdict}  {c.get('name','')}"
            if title:
                line += f"  {title}"
            self._archive_lines.append(line)
            items.append(ListItem(Label(line)))
        # 一次挂完：逐个 append 每个都是一次 AwaitMount，children 会 lag
        lv.extend(items)
        self._archive_n = len(items)      # 记住这次真的 extend 了几条
        if items:
            want = self.archive_idx
            if prev_index is not None and 0 <= prev_index < len(items):
                want = prev_index
            want = max(0, min(want, len(items) - 1))
            try:
                lv.index = want
            except Exception:
                pass
            self.archive_idx = want
        if had_focus and items:
            lv.focus()          # 把焦点还回去，否则刷新后键盘失灵

    # ---- 动作 ----
    def action_cycle_target(self) -> None:
        if not self.roles:
            return
        self.target_idx = (self.target_idx + 1) % len(self.roles)
        self._refresh_role_cards()
        self._refresh_header()
        self._set_status(f"发送目标 → {self._target()}", "info")

    def action_refresh_now(self) -> None:
        self._collect(self.active_team)

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
        self._open_slice(str(entry.get("name") or ""))

    def action_open_project_docs(self) -> None:
        """项目级文档 + wiki 笔记（「存为 note」落下去的地方，要能翻回来）。"""
        files = [("项目 · " + p.name, p) for p in project_doc_files(self.root)]
        self.push_screen(DocsScreen("项目文档", files))

    def action_next_team(self) -> None:
        self._switch_team(1)

    def action_prev_team(self) -> None:
        self._switch_team(-1)

    def _switch_team(self, step: int) -> None:
        if len(self.teams) < 2:
            return
        i = [t["name"] for t in self.teams].index(self.active_team)
        self.active_team = self.teams[(i + step) % len(self.teams)]["name"]
        self._set_status(f"看 team {self.active_team}（重采中）", "info")
        self._collect(self.active_team)

    def action_cheat_sheet(self) -> None:
        self.push_screen(CheatSheet(self._cheat_rows()))

    def action_global_menu(self) -> None:
        self.push_screen(ActionMenu("菜单（全局）", self._global_items()))

    def action_object_menu(self) -> None:
        self.push_screen(ActionMenu(self._object_title(), self._object_items()))

    def _cheat_rows(self) -> list:
        return [
            ("Tab", "切换发送目标角色（目标卡高亮 + ◀ 发送目标）"),
            ("j / k", "在 spec 档案里选片（在笔记区打字时照常打字）"),
            ("o", "翻开选中这片切片的全部文档"),
            ("p", "项目文档与 wiki 笔记"),
            ("O 或点空行", "对选中这片切片的动作（评审/派活/换 agent）"),
            ("m", "全局菜单（刷新/退出）"),
            (":qa <切片>", "外部冷读，结论门铃 PM"),
            (":reviewer <话>", "派活给独立 reviewer"),
            (":respawn <角色> [agent]", "pane 挂了重拉，可顺便换 agent"),
            (":rotate <角色>", "轮换该角色会话"),
            (":pm/:tl/:dev <话>", "直接派话给某角色"),
            ("左栏按钮", "推给当前角色 / 存 note / 外部评审（鼠标）"),
            ("Ctrl+Q 或 Ctrl+C", "退出看板"),
            ("?", "这一屏"),
        ]

    def _selected_slice(self) -> str:
        if not self.archive:
            return ""
        return str(self.archive[self.archive_idx].get("name") or "")

    def _object_title(self) -> str:
        name = self._selected_slice()
        return f"对这片切片的动作：{name}" if name else "对这片切片的动作（未选中）"

    def _object_items(self) -> list:
        name = self._selected_slice()
        if not name:
            return [("（先在右侧档案里选一片）", lambda: None)]
        return [
            (f"翻开 {name} 的文档", lambda: self._open_slice(name)),
            (f"外部评审 {name}（冷读，结论给 PM）", lambda: self._run_qa(name)),
            (f"派给 dev：开工", lambda: self._dispatch_msg("dev", f"开始实现 {name}，先读 spec.md")),
            (f"派给 tl：验收 {name}", lambda: self._dispatch_msg("tl", f"验收 {name}")),
            (f"轮换 {name} 相关角色会话", lambda: self._rotate(self._target())),
        ]

    def _global_items(self) -> list:
        return [
            ("立即刷新", self.action_refresh_now),
            ("项目文档与 wiki 笔记", self.action_open_project_docs),
            ("键位速查", self.action_cheat_sheet),
            ("退出看板", self.action_quit_board),
        ]

    def _dispatch_msg(self, role: str, msg: str) -> None:
        if self._dispatcher is None:
            self._set_status("这台机器没有投递回调", "warn")
            return
        self._set_status(f"正在推给 {role}…", "info")

        def _do() -> None:
            try:
                ok, reason = self._dispatcher(role, msg)
            except Exception as exc:                  # noqa: BLE001
                ok, reason = False, str(exc)
            self.call_from_thread(
                self._set_status,
                (f"✓ 已推给 {role}: {reason}" if ok else f"⚠ {role} 没收到: {reason}"),
                "success" if ok else "warn")

        self.run_worker(_do, thread=True, exclusive=True, group="dispatch")

    def action_quit_board(self) -> None:
        self.exit()

    def _open_slice(self, name: str) -> None:
        """翻开一片切片的全部文档：spec 在前，其余按序；项目文档跟在后面。"""
        files = [(p.name, p) for p in doc_files_in(self.proto.dir / "tasks" / name)]
        files += [("项目 · " + p.name, p) for p in project_doc_files(self.root)]
        self.push_screen(DocsScreen(f"切片 {name}", files))

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

    def _jump_to_pane(self, role: str) -> None:
        """跳到这个角色的 pane 前面来（herdr agent focus）。"""
        if self._focus_pane is None:
            self._set_status(f"这台机器没有 focus 回调；命令行：herdr agent focus {role}",
                             "warn")
            return
        self._set_status(f"正在把 {role} 的 pane 调到前面…", "info")

        def _do() -> None:
            try:
                ok, msg = self._focus_pane(role)
            except Exception as exc:                  # noqa: BLE001
                ok, msg = False, str(exc)
            self.call_from_thread(self._set_status,
                                  f"{'✓' if ok else '⚠'} {msg}",
                                  "success" if ok else "warn")

        self.run_worker(_do, thread=True, exclusive=True, group="jump")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("jump_"):
            self._jump_to_pane(bid[5:])
            return
        if event.button.id == "push":
            self._push_note()
        elif event.button.id == "note_save":
            self._save_note()
        elif event.button.id == "qa":
            # 外部评审要点名切片：把光标放进底部输入框并预填 :qa
            cmd = self.query_one("#cmd", Input)
            cmd.value = ":qa "
            cmd.focus()
            self._set_status("输入要对它外部评审的切片名，回车开跑", "info")

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
        # 按 class 找卡片，别按类型：卡片是 Vertical 容器（文本 + 跳转钮），
        # 写死 isinstance(Static) 会在重构后静默失效 —— 点卡片没反应。
        card = getattr(event, "widget", None)
        while card is not None and "role_card" not in (card.classes or ()):
            card = card.parent
        if card is not None and card.id and card.id.startswith("card_"):
            name = card.id[5:]
            if name not in self.roles:
                return
            rec = next((r for r in self.state.get("roles") or []
                        if str(r.get("role")) == name), {})
            if not str(rec.get("pane") or ""):
                # pane 挂了：点卡片就是「把它拉回来」，不是切目标 ——
                # 板子上看得见「谁不在了」和「一键拉回来」是同一件事
                self._bring_up_role(name)
                return
            self.target_idx = self.roles.index(name)
            self._refresh_role_cards()
            self._refresh_header()
            self._set_status(f"发送目标 → {name}", "info")

    def _bring_up_role(self, role: str) -> None:
        if self._bring_up is None:
            self._set_status(f"{role} 的 pane 不在了 —— 命令行：xteam up .", "warn")
            return
        self._set_status(f"正在拉起 {role}…（起 pane + 投章程，要一会儿）", "info")

        def _do() -> None:
            try:
                ok, msg = self._bring_up(role)
            except Exception as exc:                  # noqa: BLE001
                ok, msg = False, str(exc)
            self.call_from_thread(self._set_status,
                                  f"{'✓' if ok else '⚠'} {msg}",
                                  "success" if ok else "error")

        self.run_worker(_do, thread=True, exclusive=True, group="bring_up")


def run_board(root: Path, proto, herdr, label: str, role_map: dict,
              refresh_secs: float = 2.0, collector=None, dispatcher=None,
              qa_runner=None, rotator=None, note_writer=None,
              bring_up=None, respawn=None, teams=None, focus_pane=None) -> bool:
    """起板子（textual 已由调用方确认可 import）。"""
    BoardApp(root=root, proto=proto, herdr=herdr, label=label, role_map=role_map,
             refresh_secs=refresh_secs, collector=collector, dispatcher=dispatcher,
             qa_runner=qa_runner, rotator=rotator, note_writer=note_writer,
             bring_up=bring_up, respawn=respawn, teams=teams,
             focus_pane=focus_pane).run()
    return True
