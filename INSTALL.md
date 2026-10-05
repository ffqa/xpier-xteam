# 在新机器上安装与验证

包是纯文本 + 一个安装脚本，没有构建步骤。**唯一的前置是运行时依赖**，不是编译工具链。

## 1. 前置

| 依赖 | 必需 | 说明 |
|---|---|---|
| python3 ≥ 3.9 | 是 | `python3 -V` |
| [herdr](https://herdr.dev) 且在运行 | 是 | 管 pane / agent 状态 / 门铃。`herdr workspace list` 不报错才算起来 |
| 至少一个 agent CLI | 是 | devin / opencode / omp / pi / cursor / qodercli 任一 |

**没有编译步骤、不需要 pip install、没有 node_modules。** `bin/` 下两个文件靠
`PM_TEAM_HOME` 找资源目录，所以拷到哪都能用。

## 2. 安装

```bash
tar xzf xteam-<版本>.tar.gz
cd xteam-<版本>
bash install.sh                 # 装到 ~/.local
# 或装到别处：bash install.sh --prefix /opt/xteam
# 只体检不装：bash install.sh --check
```

装完 `xteam` 就在 PATH 里。布局：

```
~/.local/bin/xteam                  入口（薄壳，只设 PM_TEAM_HOME 后转发）
~/.local/share/xteam/lib/           真实实现
~/.local/share/xteam/roles/         三份角色章程
~/.local/share/xteam/templates/     协议全文
~/.local/share/xteam/docs/          详细文档
```

不想装也可以直接用：

```bash
PM_TEAM_HOME=/path/to/xteam-<版本> /path/to/xteam-<版本>/bin/xteam doctor
```

## 3. 验证（**在目标机器上跑，别信打包机器的结果**）

```bash
xteam doctor                       # 环境体检
xteam doctor --probe all --model <名字>   # 批量实测哪些 agent 认 --model
python3 <解包目录>/tests/test_protocol.py   # 协议状态机 470+ 条断言
bash    <解包目录>/tests/smoke.sh          # 三层门禁（需要 herdr + agent）
```

`smoke.sh` 分三层，任何一层不过就停在那里并报出是哪一层：

| 层 | 耗时 | 需要 |
|---|---|---|
| 1/3 静态 | ~3s | 只要 python3 |
| 2/3 单测 | ~10s | 只要 python3 |
| 3/3 端到端 | ~110s | **要 herdr + 真 agent**（会真起三个 pane） |

**没装 agent 时**前两层照样能跑，可以先用它们验证代码本身没问题。

## 4. 第一次开跑

```bash
cd 你的项目
git init -q . && git commit --allow-empty -m init   # 文件协议没有后悔药，git 必须
xteam init          # 声明结构：单应用 / 多项目 / 含共享契约层（有 TTY 会问）
xteam up . --watch  # 起 pm/tl/dev 三个 pane + 巡检
xteam say pm "你的需求……"
```

之后只跟 pm 说话。`xteam status` 看谁欠什么活，`xteam restore` 从关掉的状态恢复。

## 5. 换机器后最可能遇到的三个问题

**a. `xteam doctor` 说找不到某个 agent**
`doctor` 报的是本机 PATH 里的 agent。缺的那些不会被编排，
`xteam agents` 列出的是「本机真正装了哪些」。

**b. `--set-model` 被拒绝**
实测 **opencode v2.0.20 的交互 TUI 不支持 `--model`**（只有 `opencode run` 有）。
用 `devin` / `omp` / `pi` / `cursor` / `qodercli`，或让 opencode 用它自己的
`opencode.json`。跑 `xteam doctor --probe <kind> --model X` 可以把「推断」变成
「实测」，结果跨进程有效。

**c. openrouter 模型**
默认**只提示、不拦**（名字里往往看不出走的哪条线路）。确实要用加
`--allow-openrouter` 消掉提示。

## 6. 本版本已知的两个问题

诚实起见，这两条还没修：

1. **`--probe` 对未登录的 CLI 不可靠** —— 有些 CLI 会把错误信息写进 stdout 且退出码
   仍是 0。xteam 已校验输出内容来拦截，但仍需人工确认。
2. **`mcode` / `qoderclicn` 无法编排** —— 它们不在 herdr 的 agent kind 列表里。

（`swap` 非事务、共享 JSON 非原子写这两条已在 0.1.5 修掉：`swap` 改为两阶段替换，
新 agent 活了才关旧的；JSON 写改为 tmp + `os.replace`。）

除此之外，`install.sh` 会检查安装目录权限（other 可写 / group 可写分别提示），
并在重复安装时整体替换而不是套娃。

## 7. 在这台机器上接着开发

这个包**不含 `.git`**，但含完整开发工具链，所以解包后可以直接改、直接跑门禁、直接再打包：

```bash
tar xzf xteam-<版本>.tar.gz && cd xteam-<版本>

python3 tests/test_protocol.py     # 协议单测（约 10s，只要 python3）
python3 tests/lint_names.py        # 未定义的名字 / 不存在的方法
bash tests/smoke.sh                # 三层门禁；第 3 层要 herdr + agent
bash tests/smoke.sh --no-e2e       # 只跑前两层（这台机器没 herdr 时用）
bash install.sh                    # 装完 xteam 就能在任意项目里用

# 改完重新发布
#   bash pack.sh            走 git（需要这是仓库且工作区干净）
#   bash pack.sh --worktree 直接打工作树，不含 .git —— 非 git 环境也能用
```

两个注意点：

- **`--worktree` 模式不要求 git，也不要求工作区干净** —— 它打的就是工作树本身，
  「有未提交改动」在那条路径上是正常的。想让「有未提交改动」拦住你，用默认的
  git 模式。
- **包内没有版本历史**。想要完整 git 历史，在有仓库的机器上
  `git bundle create xteam.bundle --all`，把 `.bundle` 一起传过去，
  目标机 `git clone xteam.bundle xteam` 即得完整历史。

## 8. 反馈

跑出问题请带上这三样，定位会快很多：

```bash
xteam doctor                      # 环境
xteam --project <项目> status     # 协议状态
tail -50 .xteam/watch/events.log # 巡检事件
```