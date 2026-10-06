# xteam

三个 pane 的项目协作器：**pm / tl / dev**。你只跟 pm 说话，它把活拆下去、把结果收回来。

在一个项目目录里跑 `xteam up`，得到三个终端 pane 和一套文件协议。之后你只需要
跟 pm 说话。

```
┌─ 你（人类）─────────────────────────────────────────────┐
│  需求、拍板、验收                                        │
└──────────────┬──────────────────────────────────────────┘
               │  xteam say pm "..."
┌──────────────▼──────────────────────────────────────────┐
│  pm  对接你 → grill → 出 spec → 门禁判 PASS/FAIL → 巡检   │
└──────────────┬──────────────────────────────────────────┘
               │  xteam say tl "spec 就绪：…"
┌──────────────▼──────────────────────────────────────────┐
│  tl  拆解 → 细化 → 派活 → 持续 review → 打回 / 放行      │
└──────────────┬──────────────────────────────────────────┘
               │  xteam say dev "新任务：…"
┌──────────────▼──────────────────────────────────────────┐
│  dev 实现 → 自测 → 上报 → 停下等审核 → 修 → 主动要下一件 │
└─────────────────────────────────────────────────────────┘
```

## 安装

```bash
git clone https://github.com/ffqa/xpier-xteam && cd xpier-xteam
bash install.sh                    # 装到 ~/.local
```

装完 `xteam` 就在 PATH 里，任何项目下直接用：

```
~/.local/bin/xteam                  ← 入口（薄壳，只设 PM_TEAM_HOME 后转发）
~/.local/share/xteam/lib/           ← 真实实现
~/.local/share/xteam/roles/         ← 三份角色章程
~/.local/share/xteam/templates/     ← 协议全文
```

**整个包 200K**，纯文本无依赖。运行只需要：

| 依赖 | 必需？ |
|---|---|
| python3 ≥ 3.9 | 必须 |
| herdr（已启动）| 必须 —— 管 pane / agent 状态 / 门铃 |
| 至少一个 agent CLI | 必须 —— devin / opencode / omp / pi / cursor / qodercli 任一 |

其它：

```bash
bash install.sh --check        # 只体检不装
bash install.sh --uninstall    # 卸载
bash install.sh --prefix ~/.local   # 换前缀
PM_TEAM_HOME=/path/to/xteam xteam   # 非标准位置时手动指路
```

想直接拷贝而不装：把 `bin/` `roles/` `templates/` 三个目录保持同级一起拷走，
然后 `PM_TEAM_HOME=<那个目录>`。资源找不到时会明确报错并告诉你缺什么，
不会跑到一半崩。

## 快速开始

```bash
# 1. 先声明这个仓库是什么结构 —— 三项目根目录和单应用的治理方式不同，别搞混
xteam init --repo-kind multi-api --projects frontend,api,admin --shared-api api

# 2. 再起 pane
xteam up . --watch
```

`init` 不带参数会交互式问你（需 TTY）；CI/脚本里用上面的 flag 形式。

### 三种结构

| `--repo-kind` | 场景 | 行为差异 |
|---|---|---|
| `single` | 单个应用仓库 | 不需要声明切片属于哪个项目 |
| `multi-app` | 多个独立应用（前台 / 后台 / 脚本），互不依赖 | 每个切片**必须**声明 `project` |
| `multi-api` | 多项目 + **共享契约层**（api 同时服务多个消费方） | 同上，且门禁契约切片时**自动提示下游影响面** |

声明落在 `.xteam/projects.json`，之后所有行为都读它，不靠猜：

```json
{
  "kind": "multi-api",
  "projects": [
    {"name": "frontend", "kind": "app", "consumers": []},
    {"name": "api", "kind": "shared-api", "consumers": ["frontend", "admin"]},
    {"name": "admin", "kind": "app", "consumers": []}
  ]
}
```

`shared-api` + `consumers` 是关键：动契约层时 PM 门禁会被提醒「下游会受影响：frontend、admin」，
普通项目零噪音。只有存在 shared-api 时才建 `.xteam/contracts/`（跨项目 API 契约记录）——
单应用建个空目录只是噪音。

### `init` 生成了什么

`init` 可以独立成立，不必等 `up`：

```
.xteam/
  projects.json     结构声明（kind + 谁消费谁）—— 之后所有行为都读它
  team.json         配置模板：每个角色用哪个 agent / 模型，可直接改
  README.md         协议说明（agent 启动后会读它）
  QUEUE.md          待办队列
  index.json        状态汇总
  contracts/        跨项目 API 契约  ← 只在存在 shared-api 时才建
```

`.xteam/` 同时会加进项目的 `.gitignore`（**只增不覆盖**你已有的内容）。

改 `team.json` 就能换每个角色的 agent/模型，下次 `up` 自动沿用；跑到一半想换用
`xteam swap`。模板里的 `_howto` / `_see` 字段说明了怎么改、去哪查可用模型。

`up` 会：建 herdr workspace → 起三个 pane（pm/tl/dev）→ 给每个 pane 投角色章程
→ 之后你只跟 pm 说话。
```bash
xteam status              # 谁在干什么、谁欠着活、每个状态从什么时候开始
xteam say pm "加个导出 CSV"   # 门铃
xteam stamp pm "在等人类拍板" --waiting   # 声明「不是卡死」，巡检免打扰
xteam watch status        # 巡检状态
xteam agents              # 本机能编排哪些 agent，各自认不认 --model
xteam sync                # 改了 xteam 的规则后，把新章程重投给正在跑的 pane
xteam projects            # 治理了哪些项目、依赖关系、各自未闭合的活
xteam restore             # 关掉后从这里恢复现场
xteam swap tl devin --model swe-2-max   # 中途把 tl 换成 devin（见下）
```

`sync` 存在的理由：角色章程是 `up` 时一次性投进 pane 的。你之后改了 xteam 的规则，
正在跑的 pane 拿到的还是旧的——`sync` 重投并刷新 `.xteam/PROTOCOL.md`，不必重启整套。

## 升级 xteam 之后，正在跑的 agent 怎么跟上

xteam **不是常驻进程**，所以它不会在任何时刻「知道」自己变了。于是三件事分开做：

| 谁变了 | 怎么让正在跑的 agent 知道 |
|---|---|
| 角色章程 | `sync` **重投全文** —— 规则要重新通读，摘一句摘要等于让它拿旧理解做事 |
| 协议 / 输出规范（`skills/`） | `sync` **只给文件路径**，让它自己重读。全文很长，塞进来只会把真正的变更淹掉 |
| 新增子命令（工具能力） | `sync` **只告知有哪几个新命令**，不重投章程 —— 它跑 `xteam <cmd> --help` 就拿得到 |

`.xteam/rules.json` 记着投进去的是**哪一版**（指纹 + xteam 版本号 + 当时的命令面），
`xteam status` 第一行就把「你跑的哪版 / pane 里投的是哪版」摆出来。门狗每 30 分钟
在事件日志里提醒一次规则过期、每 24 小时查一次上游版本 —— **只记日志不打断 agent**。

`xteam update` 只回答「有没有新的」，**不自动升级**：在你正跑着三个 pane 的时候换掉
底下代码，而 pane 里那份上下文是按旧规则养成的。换成之后跑一次 `xteam sync` 即可。

## 默认跑在 omp 上，不是 opencode

opencode 的交互 TUI **不认 `--model`**（实测 v2.0.20），只能吃它自己
`opencode.json` 里的模型——那台机子上默认落到 deepseek，**既指定不了也看不见**。

omp 实测认 `--model`，所以默认 pm / tl / reviewer 都用它，dev 用 devin。
想换回来：`xteam up --set-agent pm=opencode`，只是模型不可控。

## 生命周期：workspace 复用，pane 开关

**「当前 workspace」怎么定的**（`--workspace` 显式指定永远最优先）：

1. `.xteam/session.json` 里记的上次那个
2. **cwd 反查** —— 本目录落在某个 workspace 的 pane 之内，取最贴切的那个
3. 目录名（兜底，会新建）

用 cwd 反查而不是 herdr 的 `focused`：那个字段在非交互查询下恒为 `False`，
靠不住；pane 的 cwd 才是事实。所以你在 `~/Server/dev` 里 `up`，它会认到
`dev` 那个 workspace，而不是按目录名另开一个。



反复 `up` 会在屏幕上堆一堆同名 workspace，而它们指向同一个项目 —— 既浪费终端
空间，又让人搞不清「我到底在跟哪个说话」。所以：

| 命令 | 对 workspace | 对 pane |
|---|---|---|
| `up . --watch` | **默认就用你当前所在的那个**；已存在就复用 | 起缺的 |
| `down` | **保留** | 关掉角色 pane |
| `swap <角色> <agent>` | 保留 | 只替换那一个角色 |

实测闭环：`up` → `down` → `up`，两次用的是同一个 workspace id。

```console
$ xteam up . --watch
workspace 'lc' → w3K
  启动 pm / tl / dev

$ xteam up . --watch          # 再来一次
workspace 'lc' 已存在（w3K）—— 复用，不新建
  已在跑的角色：dev、pm、tl（不动它们）
  要换掉已有角色用 `xteam swap <角色> <agent>`

$ xteam down
已关闭 3 个角色 pane（dev、pm、tl）
  workspace 'lc' (w3K) **保留着** —— 下次 `xteam up .` 直接复用

$ xteam status                # down 之后
workspace 'lc' 在，但**没有 agent 在跑**（pane 已关，workspace 保留着）。
  重新开跑（复用这个 workspace）：xteam up . --watch
```

真要连 workspace 一起清：`herdr workspace close <id>`（命令输出里会给出 id）。

## 权限弹窗：开发范围内自动放行

agent 干活常停在「要授权吗」的框上（写 /tmp、跑测试、开端口），不答就整条链停摆。
巡检会代答，但**不是无条件 yes** —— `rm -rf`、`git push --force`、`sudo` 同样会弹框，
无脑放行等于给任意代码发通行证。

| 请求内容 | 巡检怎么做 |
|---|---|
| `/tmp`、`/private/tmp`、`/var/folders/` 等临时目录 | ✅ 自动放行（优先选 always，一次授权管到底） |
| 项目目录内的文件 | ✅ 自动放行 |
| `rm -rf` / `force push` / `sudo` / `drop table` 等不可逆操作 | ❌ **永不自动**，保留现场并升级给人 |
| 认不出请求对象 | ❌ 不动 —— 宁可卡住也不乱按 |

选择类问题（非权限）仍按「推荐项优先」代答，与权限框**分开处理**：对权限框按
「第一项」盲答就等于放行了删除。

## 命令速查

| 命令 | 干什么 |
|---|---|
| `xteam init` | 声明这个仓库的结构（单应用 / 多项目 / 共享契约层） |
| `xteam up . --watch` | 起 pm/tl/dev 三个 pane + 巡检；workspace 已存在则**复用**，只补起缺的角色 |
| `xteam say pm "…"` | 门铃。之后只跟 pm 说话 |
| `xteam status` | 谁在干什么、谁欠着活、每个状态从几点开始 |
| `xteam stamp <角色> "…"` | 记一条共享时间线（`--waiting` 声明在等人类） |
| `xteam watch status\|once\|stop` | 巡检：状态 / 手动跑一轮 / 停 |
| `xteam projects` | 治理了哪些项目、依赖关系、各自未闭合的活 |
| `xteam swap <角色> <agent>` | 中途换 agent，不用重启整套 |
| `xteam qa run\|show\|set` | 第三方冷读评审（不是裁判） |
| `xteam contracts list\|new\|check\|log` | 跨项目 API 契约 |
| `xteam search` / `note` | 检索 / 沉淀 wiki 里的决策与教训 |
| `xteam agents` / `models` / `doctor` | agent 能力、可用模型、环境体检（`--probe all` 批量实测） |
| `xteam sync` | 把**改动过的**章程/协议/输出规范重投给正在跑的 pane（没变就不打扰） |
| `xteam update` | 查上游有没有新版本（**不自动升级**） |
| `xteam restore` | 关掉后从 `.xteam` 恢复现场（数据从不删除） |
| `xteam down` | 收摊：**只关角色 pane，workspace 保留**（下次 up 直接复用） |

每个子命令都有 `--help`。下面几篇讲清楚各自的取舍：

- [多项目与 API 契约](docs/多项目与契约.md) —— 单项目怎么迁到多项目、`depends_on` 跨项目排序、契约约定
- [选择 agent 与 model](docs/选择agent与模型.md) —— 每个角色跑在什么上、实测结论、openrouter 与 opencode 的坑
- [第三方评审](docs/第三方评审.md) —— 为什么是「没参与过的冷读」而不是加第四个角色
- [设计取舍](docs/设计取舍.md) —— 状态即文件、义务由协议算、时间戳、为什么不用 kander

## 测试

```bash
bash tests/smoke.sh              # 门禁：静态 → 单测 → 端到端，一次跑完三层
bash tests/smoke.sh --no-e2e     # 只跑前两层（没装 herdr 时用这个）
```

`smoke.sh` 是**门禁**，按「快 → 慢」分三层，**失败就停在那一层**：

| 层 | 耗时 | 抓什么 |
|---|---|---|
| 1/3 静态 | ~3s | 编译期查不出、只在跑到那条路径才炸的错误（`lint_names` + `lint_shell` + `py_compile`） |
| 2/3 单测 | ~10s | 协议状态机：跨项目依赖、契约边界、门禁逻辑、idle 判定 |
| 3/3 端到端 | ~110s | herdr 编排、门铃送达、巡检、换 agent |

```
[门禁 1/3] 静态检查：未定义的名字 / 不存在的方法 / shell 静默错误写法
  ✓ 没有「读了未定义的名字」或「调用了不存在的方法」
  ✓ shell 没有会静默出错的写法
  ✓ 编译 xteam / xteam_lib.py / test_protocol.py
[门禁 2/3] 协议单测：状态机 / 跨项目依赖 / 契约边界 / idle 判定
  ✓ 协议单测全过（480+ 条断言）
[门禁 3/3] 端到端：真起三个 pane，验门铃 / 巡检 / 换 agent
  …
✓ 门禁三层全过：静态 · 单测 480+ 条断言 · 端到端
```

失败时报出**是哪一层**，以及已过的层 —— 最需要定位的时候不能什么都看不到：

```
✗ 失败在【单测】层；已过的层：静态 · 单测 480+ 条断言
```

断言数用「480+」而不是精确值：单测里有几条依赖本机环境（有 `~/.ssh` 才验符号链接越界、
装了 opencode 才验模型列表结构），所以不同机器上真实条数本就不同 ——
写死一个数只会让它下一次加用例时又对不上。真实条数由 `smoke.sh` 现场数给你看。

分层不只是为了省时间：**坏代码上跑出来的结果本身是误导的**。今天就撞过——
`status` 崩在半路，看着像协议状态机有问题，真因只是方法名 `last_active` 写错。
静态层 3 秒就能抓到。

两个 lint 也可以单独跑：

```bash
python3 tests/lint_names.py    # 未定义的名字 / 不存在的方法
python3 tests/lint_shell.py install.sh tests/smoke.sh pack.sh brew-publish.sh
```

CI 只跑前两层（`.github/workflows/ci.yml`）。第 3 层要 herdr + 真 agent 起
三个 pane，那是目标机器上的验收，不该变成一条永远红的流水线。

`lint_names.py` 抓的是**编译期查不出、只在运行到那条路径才炸**的错误。这个项目里
踩过四次：`_ensure_xteam_readme` 只调用未定义（`up` 直接不可用）、在函数体中间插
模块级 `def` 截断宿主函数（命令静默不执行守卫）、`stale` 只读未定义、
`tracker.last_active()` 方法名凭记忆写错（真实是 `last_active_at`）。
检查器本身用**注入已知 bug**验证过三层都能拦住，且在正常代码上零误报。

`test_protocol.py` 覆盖协议正确性（漏派活、误判停滞、round 回退、FAIL 死锁、
跨项目依赖、契约边界、时间戳语义）。`smoke.sh` 的第 3 层验证单测碰不到的部分：
herdr 能不能程序化建 pane、agent 能不能认领名字、门铃能不能真的送达、
巡检有没有落采样行、中途换 agent 能不能续上。

## 文件

```
install.sh               安装脚本（装到 ~/.local，或 --check / --uninstall）
bin/xteam              CLI（16 个子命令，见上方速查表）
bin/xteam_lib.py        herdr 访问、寻址、门铃、状态机、idle 判定、时间戳
roles/pm.md              PM 章程（投给 pm pane）
roles/tl.md              TL 章程
roles/dev.md             dev 章程
templates/PROTOCOL.md   协议全文（`up` 拷进项目 `.xteam/`）
tests/test_protocol.py   状态机、idle 判定、时间戳
tests/smoke.sh           端到端冒烟（真拉起 pane、换 agent、验门铃与巡检）
tests/lint_shell.py      shell 静态检查（抓 bash 3.2 下会静默出错的写法）
docs/                   详细文档（见上方链接）
```
