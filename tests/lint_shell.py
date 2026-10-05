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
SET_U_USAGE = re.compile(r'^set\s+.*-u', re.M)
UNSAFE_VAR = re.compile(r'\$\{?(\w+)\}?(?!:)')

# read 命令：若某次 read 可能无输入，其变量必须在别处预先初始化
READ_CMD = re.compile(r'^\s*read\s+(?:-r\s+)?(\w+(?:\s+\w+)*)', re.M)


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
