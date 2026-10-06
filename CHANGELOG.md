# 变更记录

## 0.1.8

### 发布

- **Homebrew Formula 不再 `depends_on "python@3.12"`**。它会连带 mpdecimal /
  openssl@3 / readline / sqlite / xz / ca-certificates，还会顺带**升级**用户已有的
  openssl@3、readline、pkgconf、ca-certificates —— 为一个 200K 的纯文本工具装一整套
  带 openssl 的 Python，代价不成比例。
  改为不声明依赖，由 `install.sh` 自己体检 python3（≥3.9）；brew install 时若
  PATH 上没有 python3，就地报出可执行的修复指令（`xcode-select --install`
  或 `brew install python@3.12`），而不是装完才在用户面前炸。
  已在「PATH 上只有系统 python3（3.9.6）、无任何 brew Python」的环境实测：
  安装、`--version`、`--help`、skills 资源全部正常。

## 0.1.7

### 结项报告（PR #2）

- PM 新增 `report` 义务：全部切片闭合但 `.xteam/REPORT.md` 缺失时，`status` 显示
  PM 欠 `report`，巡检会催；写完义务消失。空项目不触发。
- 新增 `templates/REPORT-TEMPLATE.md`：目标 / 进度与完成情况 / 部署启动测试 /
  默认账号信息 / 边界与疑问点，五节，每节要有可验证内容。
- 队列还有活时 PM 仍欠 `next-slice`（别停）；队列跑完后 `next-slice` 让位给
  `report`，两条不并存。
- **修：report 义务在真实项目里一次都不会触发。** 判据原为
  `queue_counts() == (0, 0)`（队列里一行都没有），但 `Protocol.ensure()` 一上来
  就建出 `QUEUE.md`、TL 追加切片后 total 永远 > 0 —— 只有「队列从没被用过」的
  项目才会欠 report。改为按 `pending == 0` 判。
  原测试用 `Bench`（不写 `QUEUE.md`，total 恰好为 0），正好落在唯一能过的分支上，
  于是「测试全绿 + 功能是死的」同时成立。已补照真实布局写的断言。

### 修

- `PROTOCOL.md` 被误删一行，导致「解阻塞」流程图变成一个悬空的 `↓`（已恢复）。
- `XTEAM_README.md` 被误删 `memory/<role>-recap.md` 那行表格（已恢复）。
- `make release` 打出过空版本 tag：Makefile 每条配方行是**独立 shell**，
  `NEWV` 传不下去。已合并成一段 `bash -c`，并加本地校验（VERSION 非空且是 X.Y.Z）。

## 0.1.6

首个公开发布版（Homebrew：`brew tap ffqa/tap && brew install ffqa/tap/xteam`）。
包含 0.1.5 全部内容，另加 PR #1 的一批改动。

### agent 与模型

- **`agent_name()` 合法化 agent 名**：herdr 的 agent name 全局唯一且只认
  `^[a-z][a-z0-9_-]{0,31}$`。大写 workspace（如 `Sites`）原样拼成 `pm-Sites`
  会被直接拒（invalid_agent_name）。现在 `agent_slug()` + `agent_name()`
  统一处理并按 32 字符截断，**所有** agent/tab/pane 命名都走它，不再手拼。
- **`swap` 放行 `absent`**：旧 agent 已死（比如用户在 pane 里手退了 omp）时没有
  可丢的上下文，不该因为「读不到状态」就挡住人。
- **超时不再误报**：原来用 `"timeout" in str(exc)` 子串匹配，而 argv 里本来就有
  `--timeout 90000`（被回显进错误串），于是任何快速失败（invalid_agent_name rc=1）
  都被谎报成「90s 内没进入交互态」。改成只认真正的两种超时。
- **门铃不再替用户按回车**：`agent prompt` 按 bracketed-paste 模式发「文本+编码回车」，
  是一次有序提交。之前多补了一个裸 enter，而终端输入缓冲区是共享的 ——
  用户打字打一半时，这个 enter 会把「半句话 + 门铃文本」一起提交。

### 输出契约与任务边界

- 新增 `skills/ste/`（ASD-STE100 中文改写）：STE 只管「对人说的话」，
  spec/request 等工件按各角色章程详写。`doctor` 会打印它的绝对路径。
- **任务边界四环**：spec 范围节 → request 禁区节 → review 先核禁区 →
  dev「多一点都不写」。缺任何一环，超范围改动都只在事后被发现（甚至不被发现）。
- recap 只写四节、**不得加节**；swap 交接同约束。

### 修掉的真缺陷

- **`--model` 被静默丢弃**（PR #1 引入）：`extra = spec.agent_args()` 那行还在，
  `argv += ["--"] + extra` 被删了 —— 于是每个 agent 都跑在**默认模型**上，
  `set-model` 与「默认用 omp」整套设计失效，且没有任何测试会红（所有断言都只在
  单独调 `agent_args()`）。已修，并补了一条**真跑 `start_agent` 抓 argv** 的测试。
- **`install.sh` 少装 README.md**（PR #1 引入）：docs/ 值得装就是为了 README 里的
  相对链接能点开，README 没了那些链接就全断。
- **协议里 `skills/ste/SKILL.md` 是死链**：`PROTOCOL.md` 会被拷进**用户项目的**
  `.xteam/`，而 `skills/` 留在 xteam 安装目录，两者不在一棵树上。改成「安装目录下
  （`doctor` 会打印绝对路径）」，并给 `doctor` 加了一行显示该目录。
- **打包时校验文档相对链接**：抽出所有 `docs/`、`skills/` 链接逐个确认包里真有。
  「文件在包里」和「有人指向它」是两件事，漏装只在用户点开时才发现。
- **`lint_shell` 的 `set -u` 检测漏掉最常见写法**：用 `-u` 子串匹配，而
  `set -euo pipefail` 里 `-u` 不是子串，于是所有 set -u 检查在 `pack.sh` /
  `install.sh` 上静默失效。改成按短选项字母匹配。
- **新增「空数组展开」检查**：`set -u` 下 `"${A[@]}"` 遇空数组在 **bash 3.2**
  （macOS 自带）会报 unbound variable 并中断脚本，而报错指向数组、完全指不到
  真正的原因（真实踩过：`pack.sh` 用空数组传可选参数，有 herdr 那条路径必炸）。
  已在 bash 3.2 上实测确认。
- **`lint_names` 对 `class Fake(_lib.Herdr)` 误报**：基类只认裸 `ast.Name`，
  写成属性访问就查不到继承方法 → 报「不存在的方法」。违反它自己
  「宁可漏报不要误报」的规矩。
## 0.1.5

首个公开提交（未发布，仅仓库内有记录）。

### 修掉的真缺陷

- **`swap` 曾非事务**：先关旧 tab 再起新 agent，新 agent 启动失败时旧 pane 和上下文
  已经没了。改为**两阶段替换** —— 新 agent 活了才关旧的，失败则原样回滚。
- **共享 JSON 非原子写**：`index.json` / `session.json` / `team.json` 在进程被中断的
  瞬间可能被写坏，而 `_read_json()` 把坏文件读成 `{}`，于是「状态损坏」表现为
  「没有状态」。改为 tmp + `os.replace`（4 处），损坏不再静默。
- **`down` 把 workspace 一起关掉**：反复 `up` 会在屏幕上堆一堆同名 workspace。
  改为 `down` **只关角色 pane、保留 workspace**，下次 `up` 直接复用同一个。
- **`up` 不认当前 workspace**：靠 herdr 的 `focused` 字段判断，而它在非交互查询下
  恒为 `False`。改为按 **pane 的 cwd 反查**，并让 `--workspace` 显式指定最优先。

### 工程

- **`tests/lint_names.py`**：抓「读了未定义的变量」和「调用不存在的方法」。
  这类错误编译期查不出、只在跑到那条路径才炸，本项目当天撞了四次
  （`_ensure_xteam_readme` 只调用未定义、`stale` 只读未定义、`tracker.last_active()`
  方法名凭记忆写错 —— 真实是 `last_active_at`）。检查器自身用**注入已知 bug**
  验证过三层都能拦住，正常代码上零误报。
- **`smoke.sh --no-e2e`**：只跑前两层。没有它，CI 上验证包可用性的唯一办法是
  `--no-verify`，那等于把整个自证关掉 —— 用一个绿色的对勾换掉「发出去的包到底
  能不能用」这个判断。
- **打包脚本自证可用**：不只打 tar，而是解包 → 装到临时前缀 → 跑门禁前两层，
  任何一步失败就删掉半成品退出，不留一个看起来正常的坏包。
- **`pack.sh --bump` + 同号不同内容拒绝打包**：漏升版本会让包名恒定、覆盖上一个包，
  brew Release tag 也恒定（「已存在，跳过」→ 改了代码也发不出去）。
- **发布前 provenance 对账**：记录包是哪次提交打的，发布时与 HEAD 比对 ——
  「改完源码忘了重打包」不再静默发出旧包。

### 默认

- 章程指纹比对落地：`status` 主动提醒章程已变，`sync` 只重投**变化的部分**。

### 发布

- **Homebrew Formula 不再 `depends_on "python@3.12"`**。它会连带 mpdecimal /
  openssl@3 / readline / sqlite / xz / ca-certificates，还会顺带**升级**用户已有的
  openssl@3、readline、pkgconf、ca-certificates —— 为一个 200K 的纯文本工具装一整套
  带 openssl 的 Python，代价不成比例。改为不声明依赖、由 `install.sh` 自己体检
  python3（≥3.9），brew install 时若无 python3 就地报出可执行的修复指令
  （`xcode-select --install` 或 `brew install python@3.12`）。
  已在「PATH 上只有系统 python3（3.9.6）、无任何 brew Python」的环境实测：
  安装、`--version`、`--help`、skills 资源全部正常。
- 移交给 GitHub Actions：推 `xteam-v*` tag → 跑门禁 → 建 Release →
  自动更新 `ffqa/homebrew-tap` 的 Formula。本地不再需要发布脚本。
- **`make release`**：一条命令完成升版本 → 门禁 → 提交 → 打 tag → 推，
  CI 接走打包与发布。发布逻辑留在本机就多一次「忘了重打包却发了 tag」的
  机会，而那种失败只在用户机器上才暴露。
- Homebrew tap 用 **SSH 部署密钥**而非 PAT：部署密钥只能写 `homebrew-tap`
  一个仓库，泄露了也只影响 tap 且随时可吊销。
- **`.gitignore` 补 `/Formula/`（锚定顶层）**：原来写 `Formula/`，而 macOS 的
  `core.ignorecase` 默认为 true，于是它把发布模板所在的 `.github/formula/`
  一起吃掉 —— 模板从没进过仓库，CI 从一个不存在的路径读它，发版必炸，而本地
  看不出任何问题。
- **「代码不再硬编码 `.xteam`」那条守卫一直是空转的**：它找的是 `'"\.xteam"'`，
  而 Python 不会吃掉反斜杠，这种字符串全仓库一次都没出现过，于是断言恒过 ——
  真硬编码了也抓不到。改成 raw string，并已用注入验证能拦住。
- **Formula 占位符从 `{{VERSION}}` 改成 `@VERSION@`**：前者和 Ruby 自己的插值
  语法撞车，漏替换时错误只会在用户 `brew install` 时才炸。现在替换后会 grep
  残留 + `ruby -c` 语法自检，两道都在发版时就拦住。

## 0.1.1

用户在实际项目里跑出来的三个问题，根因都不在代码而在**指令**。

### recap 每 60 秒问一次（跑了 696 次）
context 分支把记账写成空串，而切片分支的判据是「recap_asked_for == 切片名」——
空串不等于任何切片名，于是下一轮又命中切片分支；而冷却检查挂在 context 分支
后面，此时 need 已是 True，**根本没被检查**。两个分支各修各的堵不住，因为记账
格式和判据格式本来就对不上。修法：冷却提到入口，涵盖所有触发原因。

### 人变成了瓶颈
两处**指令在教 agent 去问人类**：

- `OBLIGATIONS["next-slice"]` 原文是「别停，**找我问下一个需求**」。PM 忠实
  照做，于是「队列里明明写着切片 8，它却在等确认」。改成：先自己看 QUEUE.md /
  backlog，有就直接开工；只有那里也空了才**一次性**问人类。
- `roles/tl.md` 第 108 行把「两个方案成本差太多」列为阻塞理由，而第 134 行又说
  「只有需求歧义才停」——**章程自相矛盾**。用户看到 TL 正是拿四条实现选择
  （两分支取舍 / Docker 构建缺口归属 / console 双源 / smali 验证）去阻塞。
  现在明确：阻塞是真实的「我无法继续」，不是「我想多听听意见」。

### 队列有活却没人欠账 → PM 欠 start-next
项目里还没有切片目录（或全部闭合）时 `debts()` 什么都不产出，于是
`idle + owes:none` 被巡检判成「合法终态」—— 三个 pane 全闲着、队列里明明有活，
而**人成了唯一的推进力**。新增义务：队列有 pending 项而 PM 无欠账时，PM 欠
`start-next`，巡检会推它。

## 0.1.0

从「能跑」到「能放心交给别人跑」的一轮。首个公开版。

### 修掉的真缺陷
- **跨项目依赖门禁可被 `request.md` 绕过**：TL 一旦先拆解，dev 就会照着一套
  还不存在的 API 去实现。改为对所有阶段生效。
- **API 契约文件名碰撞**：`/user_profiles` 与 `/user/profiles` 曾共用一个文件名，
  两个接口互相覆盖。改成转义可逆编码，新契约在 front-matter 记真实路径。
- **recap 从没工作过**：提示里写 `.pm/memory/`（改名前残留），代码读 `.xteam/memory/`，
  每份 recap 都写进黑洞。agent 们为绕过去改成「双写」，token 成本翻倍。
- **recap 记账崩巡检**：`set_alerted(role, "recap")` 污染 0/1/2 的告警阶梯，
  `alerted < 1` 抛 TypeError 且该行在 working 分支里 —— agent 一转 working，
  **整个看门狗死掉**。改用独立字段 + `alert_level()`。
- **巡检每 60 秒重复问 recap**：context 高时记账写空串而判据是「== 切片名」，
  空串永远不等于它。加冷却。
- **`xteam --project X up .` 里的 X 被静默忽略**：argparse 子解析器的同名位置参数
  覆盖了全局选项，状态全写到当前目录。位置参数改名 `dir`。
- **单应用项目在 `projects` 显示「目录不存在」**：项目名就是根目录名，而根目录
  不是自己的子目录。
- **`xteam down` 缺 `_mark_session_ended` 定义**：最常见路径（workspace 还在时
  down）走到那行就 NameError。
- **损坏的 PID 文件让 `watch start` 崩**，且 `_alive(0)` 恒为真
  （`os.kill(0,0)` 是「发给整个进程组」）。
- **install 重复安装套娃**且**升级静默不生效**（新版被埋进 `roles/roles/`）。
- **安装权限检查只看末位数字**，把 `674`（组可写）判成安全。

### 新增
- 多项目治理：项目注册表、共享契约层、`depends_on` 跨项目切片排序 + 拓扑序
- API 契约：`xteam contracts new|list|check|log`，路径边界校验
- 第三方冷读评审：`xteam qa`，白名单自动放行 + 不可逆操作永不自动
- `xteam swap`：中途换 agent（两阶段替换，新 agent 活了才关旧的）
- workspace 复用：`up` 默认认「你当前所在的那个」，`down` 只关 pane 保留 workspace
- 权限弹窗在开发范围内自动放行（/tmp、项目目录…），`rm -rf`/`sudo` 永不自动
- 章程变更检测：指纹比对，`status` 主动提醒、`sync` 只重投变化的部分
- `doctor --probe all` 批量实测 agent 能力

### 工程
- **三层门禁**（静态 → 单测 → 端到端），失败报出是哪一层
- `tests/lint_names.py`：抓「读了未定义的变量」和「调用不存在的方法」——
  这类错误编译期查不出、只在跑到那条路径才炸，本项目当天撞了四次
- JSON 原子写（11 处）+ 损坏不再静默伪装成「没有状态」
- 路径字面量收敛成常量 + 守卫测试防改名漏改
- 打包脚本自证可用：解包 → 装到临时前缀 → 跑门禁前两层

### 默认
pm / tl / reviewer 改用 **omp**（opencode 的交互 TUI 不认 `--model`，只能吃
`opencode.json` 里那份指定不了也看不见的模型）。dev 仍 devin。

## 0.1.0-beta

内部首个可交付版本：三 pane 编排 + 文件协议状态机 + 巡检 + 恢复。
