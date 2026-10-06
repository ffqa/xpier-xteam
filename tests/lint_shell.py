#!/usr/bin/env python3
"""shell 脚本的静态体检。

只查**会静默出错**的那一类问题——不是风格，是「跑起来才发现、而且报错信息完全
指不到真正原因」的那种。macOS 自带 bash 3.2 尤其容易踩。

    python3 tests/lint_shell.py tests/smoke.sh

退出码 0 = 通过，非 0 = 有必须修的问题。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 变量后紧跟多字节字符：bash 3.2 会把它当变量名的一部分，于是报
# 「xxx: unbound variable」——而 xxx 明明赋值了，指不出真正原因。
# 真实踩过：`"$after_seq（门铃进去了）"` 直接把整个脚本打断。
VAR_ADJACENT_MULTIBYTE = re.compile(r'\$(?!\{)\w+(?![\w}])([^\x00-\x7F])'
                                    r'|\$\{[^}]+\}([^\x00-\x7F])')

# `set -u` 下引用可能未赋值的变量。允许用 ${var:-默认值} 兜底。
#
# **必须按「短选项里的字母」来匹配，不能找 `-u` 这个子串。**
# 原来写的是 `^set\s+.*-u` —— 而 `set -euo pipefail` 里 `-u` 并不是子串
# （`-euo` 是 -、e、u、o，u 前面是 e 不是 -），于是**最常见的写法反而不算
# set -u**，所有依赖它的检查在 install.sh / pack.sh / brew-publish.sh 上
# 全部静默失效。只有 `set -uo pipefail` 这种把 -u 放最前的才会被认出来。
SET_U_USAGE = re.compile(r'^set\s+[^\n#]*?(?<![a-zA-Z0-9])-[a-zA-Z]*u[a-zA-Z]*(?![a-zA-Z0-9])', re.M)
UNSAFE_VAR = re.compile(r'\$\{?(\w+)\}?(?!:)')

# read 命令：若某次 read 可能无输入，其变量必须在别处预先初始化
READ_CMD = re.compile(r'^\s*read\s+(?:-r\s+)?(\w+(?:\s+\w+)*)', re.M)

# `set -u` 下展开**空**数组：`"${arr[@]}"` 会报 `arr[@]: unbound variable`。
# bash 4.4+ 容忍，bash 3.2（macOS 自带）不容忍 —— 而 macOS 正是本项目的主战场。
# 真实踩过：pack.sh 里 `GATE_ARGS=()` / `GATE_ARGS=(--no-e2e)` 二选一，
# 本机有 herdr 时数组是空的，于是「跑完整三层」那条路径 100% 炸，
# 报错还指向数组展开、完全指不到「你只是想少传一个参数」。
ARRAY_EXPANSION = re.compile(r'\$\{(\w+)\[@\]')
# 两种安全写法（POSIX 惯用 / bash 3.2 都成立）：
#   ${A[@]:-}              —— 空数组展开成空
#   ${A[@]+"${A[@]}"}      —— 数组为空时整个词都不展开
# **不做成「ARRAY_EXPANSION 的后缀判断」**：嵌套写法里 `[^}]*` 会在内层的 `}`
# 处截断（`${A[@]+"${A[@]}"}` 只截到 `+"${A[@]`），于是安全写法被误判成不安全。
# 改成先标出安全写法的**位置**，再跳过与之重叠的展开。
ARRAY_SAFE_EXPANSION = re.compile(
    r'\$\{[A-Za-z_]\w*\[@\]:-'          # ${A[@]:-}
    r'|\$\{[A-Za-z_]\w*\[@\]\+\s*"\$\{[A-Za-z_]\w*\[@\]\}"\}'  # ${A[@]+"${A[@]}"}
)
ARRAY_ASSIGN = re.compile(r'(?<![A-Za-z0-9_$])(?:declare\s+-a\s+)?(\w+)=\(')
# 收集到的「本脚本内被赋过空数组的数组名」—— 只有这些才可能为空。
# **不锚定行首**：实际写法常内嵌在 `if ...; then A=(); else ...` 这种一行里，
# 锚行首就等于只认 `declare -a` 那种写法，于是漏掉最常见的 `A=()`。
ARRAY_NAMES = re.compile(r'(?<![A-Za-z0-9_$])(?:declare\s+-a\s+)?(\w+)=\(\s*\)')


def check_multibyte_adjacency(path: Path, lines: list[str]) -> list[str]:
    problems = []
    for i, line in enumerate(lines, 1):
        if line.lstrip().startswith("#"):
            continue
        if VAR_ADJACENT_MULTIBYTE.search(line):
            problems.append(
                f"{path.name}:{i} 变量紧跟多字节字符 —— bash 3.2 会当成变量名的一部分。"
                f"变量与中文/符号之间加空格或逗号。\n      {line.strip()[:90]}")
    return problems


def check_read_initialized(path: Path, lines: list[str], set_u: bool) -> list[str]:
    """`set -u` 下 read 读到空输入不会赋值，之后引用就是 unbound。

    要求：每个 `read a b` 里的变量，在 read 之前出现过显式赋值。
    """
    if not set_u:
        return []
    problems = []
    for i, line in enumerate(lines, 1):
        m = READ_CMD.match(line)
        if not m:
            continue
        vars_ = m.group(1).split()
        prefix = "\n".join(lines[:i])
        for v in vars_:
            if not re.search(rf'(^|\s|;){re.escape(v)}=', prefix):
                problems.append(
                    f"{path.name}:{i} `read` 读入 {v}，但前面没有预初始化。"
                    f"输入为空时 set -u 会报 unbound variable。"
                    f"在 read 前加 `{v}=-\"。\n      {line.strip()[:80]}")
    return problems


def check_quoted_command_sub(path: Path, lines: list[str]) -> list[str]:
    """`x=$("$BIN" --project "$WORK" ...)` 里 $BIN 若含空格会整体出错。

    这类在本项目里 $BIN 是绝对路径，暂时安全；但如果哪天改成相对路径就会踩。
    这里只提示不报错。
    """
    return []


def check_empty_array_expansion(path: Path, lines: list[str], set_u: bool) -> list[str]:
    """`set -u` 下 `"${arr[@]}"` 在数组为空时是 unbound variable（bash 3.2）。

    只在**本脚本里被赋过空数组**时才报：别的脚本传来的数组是否为空本地无从
    判断，报了就是误报 —— 而这个检查器的规矩是宁可漏报不要误报。
    """
    if not set_u:
        return []
    # **必须用 search 而不是 match**：正则已经不锚行首（`A=()` 常内嵌在
    # `if ...; then A=(); else ...` 里），match 只在行首匹配，于是收集不到
    # 任何数组名，整条规则静默失效 —— 正是这个检查器自己的失败模式。
    empty_names = {m.group(1) for ln in lines for m in ARRAY_NAMES.finditer(ln)}
    if not empty_names:
        return []
    problems = []
    for i, line in enumerate(lines, 1):
        if line.lstrip().startswith("#"):
            continue
        safe_spans = [m.span() for m in ARRAY_SAFE_EXPANSION.finditer(line)]
        for m in ARRAY_EXPANSION.finditer(line):
            name = m.group(1)
            if name not in empty_names:
                continue
            # 落在安全写法的位置范围内 → 放过（含嵌套写法的内外两层）
            if any(s <= m.start() < e for s, e in safe_spans):
                continue
            problems.append(
                f"{path.name}:{i} 展开可能为空的数组 ${{{name}[@]}} —— "
                f"bash 3.2 在 set -u 下报 unbound variable，"
                f"且报错指向数组、不是指向真正的原因。\n"
                f"      写成 ${{{name}[@]:-}} 或 ${{{name}[@]+\"${{{name}[@]}}\"}}，"
                f"或者干脆分两个显式分支。\n      {line.strip()[:80]}")
    return problems


def main(argv: list[str]) -> int:
    targets = [Path(a) for a in argv[1:]] or sorted(
        Path(__file__).resolve().parent.parent.glob("**/*.sh"))
    if not targets:
        print("没有可检查的 .sh 文件")
        return 0

    all_problems: list[str] = []
    for path in targets:
        if not path.exists():
            print(f"跳过不存在的文件：{path}")
            continue
        text = path.read_text(encoding="utf-8")
        lines = text.split("\n")
        set_u = bool(SET_U_USAGE.search(text))
        all_problems += check_multibyte_adjacency(path, lines)
        all_problems += check_read_initialized(path, lines, set_u)
        all_problems += check_empty_array_expansion(path, lines, set_u)

    print(f"检查 {len(targets)} 个 shell 脚本"
          f"{'（含 set -u）' if any(SET_U_USAGE.search(p.read_text(encoding='utf-8')) for p in targets if p.exists()) else ''}")
    if all_problems:
        print(f"\n✗ {len(all_problems)} 个问题：\n")
        for p in all_problems:
            print(f"  {p}")
        return 1
    print("✓ 没有会静默出错的写法")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
