# 变更记录

## 0.2.5

### 解锁流水线重叠：dev 交付后不等 gate

原来「交付 → 判定」之间**整棵产品树冻结**（F-16：gate 必须判在一棵可归因的树上），
于是 dev 交付完只能干等。本仓 24 片实测：这段每片 3–12m、中位 6.4m，占每片总时长
（中位 17.5m）约三成；24/24 片的冻结窗口里**没有任何其它片在实现** —— 纯串行。

可归因从「树不许动」降级成「这一段提交不许动」：

- **`xteam stats`**（新）—— 每片分段耗时 / 中位 / 吞吐 / 并行度 / 冻结合计 /
  **重叠命中**。读的全是文件 mtime（协议里没有时间戳字段），纯派生、不需要 herdr。
  本仓基线：dev 实现中位 9.1m（占 61%）、PM 门禁 4.0m、TL 复审 2.4m、并行度 0.42。
- **`xteam snap <slug>`**（新）—— 按 `delivered.json.changed[]` 只提交这一片的路径，
  把 `base`/`head` 写回；别人的、人类未提交的改动不卷进来；无差异退出 3（不造空提交）；
  `changed[]` 空退出 2（不能归因的东西不许进历史）。
- **`xteam review-tree <slug>`**（新）—— 把交付提交钉成一棵 `git worktree --detach`
  的只读快照树，TL/PM 在里面 review/gate；片闭合时巡检自动清。
- **`touches.json`**（新工件，TL 写）—— 这一片会碰哪些路径（glob，双向匹配 + 目录前缀）。
  `debts()` 的写树闸门从「有待判片就冻全场」改成「只拦触碰冲突」；**没写 = 未知 =
  冲突**（旧片行为不变）。另有返工优先：别的片有没消费的 FAIL 时不开新片。
- `status` 把冻结理由说清楚：`⏳ 待 gate：a（其它片可并行——触碰不冲突）` /
  `（冻结：b 与它触碰同一批文件）`，并报 `⇄ 重叠中：…`。
- 章程：TL 写 request 时同时写 `touches.json`；dev 交付前 `snap`、自测只跑 request
  里声明的命令、FAIL 未消费时不开新片；TL/PM 在快照树里判。子代理边界写明
  （只读、产物单一作者、结论要带 `文件:行号`），`doctor` 打印 kind 的实测表。

- 文档：单测断言地板 790+ → **770+** —— 地板不能高于任何布局的实际条数
  （仓库布局 794 / 安装布局 780，pack 自证跑的就是后者）。

实测：`bash tests/smoke.sh` 全过（静态 · 单测 · 多项目 e2e · 端到端）。

## 0.2.3

### 身份进系统提示词：`/new`、`/clear` 之后不再是陌生人

长跑必然遇到上下文被重置。原来一旦重置，agent 连自己是 pm/tl/dev 都不知道 ——
章程是 `up` 时投进**对话**的，而重置清掉的就是对话。

拆成两半存：**身份**放系统提示词，**现状**放文件。

- `up` / `swap` / `reopen` 起 omp 时带上
  `--append-system-prompt .xteam/identity/<角色>.md`（章程全文 + 「重置后先
  whoami」的引导）。实测 omp 18.4.9：`/new` 换会话（herdr 的 `agent_session`
  路径随之变化）、`/clear` 丢消息（会话继续），**两者都动不了进程的系统提示词**。
  pi 同一套 TUI，实测同样认这个 flag、同样扛过 `/new`（只有一个证据渠道不同：
  herdr 不暴露 pi 的会话标识，重开的生效判据改用 pane 回执
  `New session started` 的出现次数）。
- 没有这个 flag 的 kind 退回旧路：章程投进对话，且 `say` 的门铃自带一句
  身份与自检提醒（`context_hint`）—— 对它们来说，重置就是全部忘光。
- `sync` 里章程变更时顺带刷新身份文件：正在跑的 pane 改不了自己的系统提示词，
  但下一次 `up` / `swap` / `reopen` 起的进程必须拿到新版。

### 新增

- `xteam whoami` —— agent 的看板视图：你欠什么义务、哪些切片没闭合、你上次的
  recap 尾巴、时间线尾部、该读哪些文件。**不依赖 herdr**，pane 全关着也能看。
  上下文被清空后第一件事跑它就能接回现场，不必重新解释一遍。
- `xteam reopen <角色>` —— 计划内的重开（原地）：先要 recap → 发 `/new` →
  身份与现状投回。**pane 与进程都不换**，比 `swap` 轻；换 kind/model 仍走 `swap`。
  生效判据是 herdr 的 `agent_session` 真的变了，不是「命令投出去了」。
- 巡检在 context ≥ 75% 时建议重开（落事件 + 时间线 + 通知，30 分钟冷却）。
  **不自动重开**：丢掉对话里还没落文件的推理值不值得，由人判断。

### 修

- **omp 的 context 占用一直解析不出来**：原解析器只认带括号的格式
  （opencode `392.8K (37%)` / devin `168k / 262k tokens (64%)`），而 omp 的状态栏是
  `---51%---|----1M-`。于是 `_maybe_request_recap` 的 context 分支在**默认 agent
  上从来没触发过** —— 一个只在别的 kind 上工作的阈值等于没有。顺带支持小数
  （新会话显示 `-0.7%`，`int()` 会直接抛）。
- **`swap` / `reopen` 被自己的安全阀挡在门外**：`done` 没进放行名单，而 herdr 的
  `done` 与 `idle` 语义相同（都表示「可以接受输入」，区别只是 CLI 有没有看到那次
  完成）。刚投完 intro 的 agent 正停在 `done`，于是换人被拦，理由还是「会丢掉它
  正在做的上下文」。

## 0.2.1

### 改：版本号进位阈值 10 → 100

0.2.0 定的是**满 10 进 1**，那条规则在频繁发版下不成立：这个小项目一天可能
发一次，满 10 进位等于每 10 次发版就要么被动升 minor、要么接受 `0.1.10`
这种看着别扭的号。

改成**满 100 进位**，patch 可以一路走到 99：

```
0.2.5 → 0.2.6 → … → 0.2.99 → 0.3.0        0.99.5 → 1.0.0
```

`minor` / `major` 只在**主动升**、或真的攒满 99 次时才动 —— 「这次算不算大变动」
这个判断权交回给人，不被发版频率绑架。

每段上限 99（不出现三位数）是刻意的：三位数版本号会让一部分版本比较器
（含 Homebrew 早期解析）措手不及。已用「连升 200 次」验证全程不出现三位数，
并用注入验证过把阈值退回 9 时断言会红。

## 0.2.0

### 版本号：满 10 进 1（十进制进位）

`patch` 原来是无条件 +1，于是版本会走到 `0.1.10`、`0.1.11`。那是十六进制的直觉，
用十进制版本号上会让人下意识把 10 当成「比 9 小」—— 排序、比较、grep 全都要
额外解释一遍。

现在进位：

```
0.1.8 → 0.1.9 → 0.2.0 → 0.2.1 …
0.9.3 → 1.0.0
```

用 `>= 9` 而不是 `== 9`：万一有人手改成 `0.1.12`，仍然正确进位而不是继续往上数。
测试直接从 `pack.sh` 里抽出**真的** `bump_version` 来跑（重写一遍的话，
测试就只验证了「我以为的规则」，`pack.sh` 改坏了也不会红），并用注入验证过能拦住。

## 0.1.9

### 升级感知：xteam 不是常驻进程，怎么知道该更新

原来只有「章程/协议改了但没投」这一个粗粒度信号，而且它自己还坏着。

**修掉的真缺陷**

- **只改 `PROTOCOL.md` 时 `sync` 修不好它**（永久报警）：重投循环是
  `if stale and role not in stale: continue`，而只改协议时 `stale={"PROTOCOL"}`、
  三个角色都不在里面 → 一个都不重投 → `sent=0` → 指纹不更新 →
  **status 一直报警，而 sync 反复跑也没用**。
  现在协议/输出规范变更一律通知所有人；且只要检测到变更就落状态。
- **指纹漏掉 `skills/`**：STE 输出规范住在 `skills/ste/SKILL.md`，`PROTOCOL.md`
  里只有 8 条精简版。所以某次发版只改输出规范 → `stale` 为空 → sync 说「没变化」
  → agent 继续按旧规范输出，而 status 看不出任何异常。
- **`lstrip("v")` 剥不掉 `xteam-v0.1.8`** → 版本号被当成非数字恒为 0 →
  「上游有新版」永远不显示，而且**不报错**（最难发现的一类）。顺带修了
  字符串比较把 0.1.10 判成比 0.1.8 小的问题。

### 新增

- `xteam update`：查上游最新 release（GitHub API，**只查不升级** —— 在你正跑着
  三个 pane 时换掉底下的代码，而 pane 里的上下文是按旧规则养成的）。
- `status` 头部显示「本机 xteam X · pane 里投的是 Y」—— 不常驻的进程唯一的
  差异暴露点。
- `.xteam/rules.json` 除指纹外，另记 **xteam 版本号**与**当时的命令面**：
  指纹是不透明哈希，答不了「该更新了没有」。
- 门狗低频提醒：规则变更 30 分钟、上游版本 24 小时，**只落事件日志、绝不打断
  agent**（自动重投会烧掉一个门铃回合，还可能正好插在 agent 干活中间）。
  上游检查结果缓存到 `.xteam/update-check.json`，`status` 只读缓存不联网 ——
  一个用来做判断的命令不该是会卡住的东西。

### 分开处理（刻意的）

| 变更 | 处理 | 为什么 |
|---|---|---|
| 角色章程 | 重投全文 | 规则要重新通读 |
| 协议 / 输出规范 | 只给路径 | 全文太长，塞进来会淹掉真正的变更 |
| 新增子命令 | 只告知 | agent 跑 `--help` 就拿得到，不必烧一个门铃回合 |

### 工程

- 门狗提醒的冷却用 `IdleTracker.nag_gate()`，不用 `observe()` 凑 ——
  后者只在 seq 变化时重置 `since`，于是「先 clean 后 stale」这条路上第一次提醒
  会被压到下一个窗口之后，恰好是最该立刻喊的那次被吞掉。

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
