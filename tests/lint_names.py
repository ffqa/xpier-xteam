#!/usr/bin/env python3
"""抓「读了没定义的变量 / 调用了不存在的方法」——这类错误编译期查不出来，
只在运行到那条路径时炸。

**为什么值得单独一个检查。** 写这个文件当天就撞了三次：
  1. `_ensure_xteam_readme` 被调用两次但从未定义 —— `xteam up` 直接不可用，
     而单测不覆盖 up，只有 smoke.sh 第 1 步能抓到
  2. 在函数体中间插入模块级 `def`，把宿主函数的函数体截断，后半段变成不可达代码，
     命令静默不执行守卫（`--probe <不存在的kind>` 不报错）
  3. `stale` 在 `cmd_status` 里只读未定义 —— 同样只在 smoke 第 8 步暴露
  4. `tracker.last_active(role)` —— 方法名凭记忆写成 `last_active`，真实是
     `last_active_at`。**属性名查不出来，只查得了变量名**，所以本文件也做
     属性校验：对本项目自己定义的类，检查 `.method(` 是否真实存在。

共同点：**都是「名字在某处被读了，但那条路径从没被执行过」**。所以要静态查。

用法：python3 tests/lint_names.py [文件...]
"""
from __future__ import annotations

import ast
import builtins
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def module_bindings(tree: ast.Module) -> set[str]:
    """模块级可见的名字：import、赋值、def、class、for/with/except 的目标。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names |= {(a.asname or a.name) for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.Global):
            names |= set(node.names)
    return names


def known_attributes(tree: ast.Module, path: Path) -> dict[str, set[str]]:
    """本项目自己定义的类 → 它们真实有的方法名（不含继承来的）。

+    只覆盖本文件内定义的类；跨文件的基类方法查不到，会漏报而不是误报。
    """
    classes: dict[str, ast.ClassDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes[node.name] = node
    out: dict[str, set[str]] = {}
    for name, node in classes.items():
        out[name] = {f.name for f in node.body
                     if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return out


def class_bases(tree: ast.Module) -> dict[str, list[str]]:
    """类名 → 基类名列表（只取 `ast.Name` 形式的基类）。"""
    out: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            # **合并而不是覆盖**：一个文件里可能有多个同名类（测试里常写
            # 局部 FakeXxx），ast.walk 是 BFS 不保证源码顺序，后写覆盖先写
            # 会把「有基类」的那个抹掉。
            got = out.setdefault(node.name, [])
            got.extend(b.id for b in node.bases if isinstance(b, ast.Name))
    return out


def resolve_inheritance(classes: dict[str, set[str]],
                        bases: dict[str, list[str]]) -> dict[str, set[str]]:
    """把基类的方法传播给子类，直到不动点。

    **必须跨文件做**：测试里的 `class FakeHerdr(Herdr)` 在 test_protocol.py，
    而 `Herdr` 定义在 xteam_lib.py。单文件里解析继承会把基类当成「未知」，
    报出「FakeHerdr 没有 detect_by_cwd」这种假阳性 —— 假阳性会让门禁变成噪声。
    """
    out = {k: set(v) for k, v in classes.items()}
    for _ in range(len(out) + 1):          # 传播轮数上限 = 类数，必定收敛
        changed = False
        for name, blist in bases.items():
            if name not in out:
                continue
            for b in blist:
                if b in out and not out[b] <= out[name]:
                    out[name] |= out[b]
                    changed = True
        if not changed:
            break
    return out
def attr_problems(tree: ast.Module, path: Path,
                  classes: dict[str, set[str]] | None = None) -> list[str]:
    """`x.method(...)` 里的 method 在本项目类上不存在 → 报出来。

    先扫出「哪个变量是被本项目哪个类构造出来的」（`tracker = IdleTracker(...)`
    → tracker 是 IdleTracker 实例），再据此判定方法名。判不出的变量一律跳过：
    宁可漏报，也不要在正常代码上误报 —— 一个天天误报的检查等于没有检查。
    """
    if classes is None:
        classes = known_attributes(tree, path)
    if not classes:
        return []
    # 变量名会被复用（测试里 p 既是 Protocol 又是 Path），所以记录**每一次**赋值，
    # 用「最近那次」判定类型；最近那次不是类构造 → 跳过。
    #
    # **必须按函数作用域收集。** Python 的变量是函数局部的：测试 A 里
    # `f = FakeHerdr(...)`、测试 B 里 `for f in ...: f.read_text()` 是两个不同的
    # f。按文件统一收集会把 A 的类型套到 B 上，报出「Path 没有 read_text」。
    stores: dict[str, list[tuple[int, str]]] = {}     # var -> [(行号, 类名或 "")]
    module_stores: dict[str, list[tuple[int, str]]] = {}
    for node in tree.body:            # 模块级赋值对所有函数可见
        targets: list[ast.expr] = []
        val = ""
        if isinstance(node, ast.Assign):
            targets, v = node.targets, node.value
            if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) \
                    and v.func.id in classes:
                val = v.func.id
        elif isinstance(node, ast.AnnAssign) and node.target:
            targets, v = [node.target], node.value
            if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) \
                    and v.func.id in classes:
                val = v.func.id
        for tgt in targets:
            if isinstance(tgt, ast.Name):
                stores.setdefault(tgt.id, []).append((node.lineno, val))
    problems: list[str] = []

    def func_stores(fn: ast.AST) -> dict[str, list[tuple[int, str]]]:
        out: dict[str, list[tuple[int, str]]] = {}
        for node in ast.walk(fn):
            targets: list[ast.expr] = []
            val = ""
            if isinstance(node, ast.Assign):
                targets, v = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.target:
                targets, v = [node.target], node.value
            else:
                continue
            if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) \
                    and v.func.id in classes:
                val = v.func.id
            for tgt in targets:
                if isinstance(tgt, ast.Name):
                    out.setdefault(tgt.id, []).append((node.lineno, val))
        return out

    # 模块级 + 函数级各自的赋值表；查某个调用点时只看它所在函数
    scopes: list[tuple[ast.AST, dict]] = [(n, func_stores(n)) for n in tree.body
                                           if isinstance(n, ast.FunctionDef)]
    scopes.append((tree, module_stores))
    for scope_node, scope_stores in scopes:
        for node in ast.walk(scope_node):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not isinstance(fn, ast.Attribute) or not isinstance(fn.value, ast.Name):
                continue
            var, meth = fn.value.id, fn.attr
            # 类名 → 实例变量名的约定不可靠（tracker/reg/proto 都可能），所以只检查
            # 「这个变量名确实是被某个本项目类构造出来的」那些 —— 于是
            # IdleTracker(...) 赋给 tracker 能判定为 IdleTracker 实例。
            # 判不出来就跳过 —— 宁可漏报，不要误报。
            if meth.startswith("__"):
                continue
            pool = scope_stores.get(var) or module_stores.get(var) or []
            cands = [(ln, cn) for ln, cn in pool if ln < node.lineno]
            if not cands:
                continue
            _line, cname = max(cands)      # 最近一次赋值决定它是什么类型
            if not cname:
                continue                     # 最近那次不是类构造 → 类型不明，跳过
            owner = classes[cname]
            if meth in owner:
                continue
            problems.append(
                f"{path}:{node.lineno} {var}.{meth}() —— {owner} 没有这个方法"
                f"（现有："
                f"{'、'.join(sorted(m for m in owner if not m.startswith('_'))) or '无'}）")
    return problems
def check_file(path: Path, classes: dict[str, set[str]] | None = None) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    if classes is None:
        classes = known_attributes(tree, path)
    module = module_bindings(tree) | set(dir(builtins)) | {"__file__", "__name__"}
    problems: list[str] = []

    def walk_fn(fn: ast.AST, outer: set[str]) -> None:
        local: set[str] = set(outer)
        args = getattr(fn, "args", None)
        if args:
            for group in (args.posonlyargs, args.args, args.kwonlyargs):
                local |= {a.arg for a in group}
            for extra in (args.vararg, args.kwarg):
                if extra:
                    local.add(extra.arg)
        # 收集本函数内所有绑定（含嵌套 def 的名字，因为它们也在本作用域）
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                local.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                local.add(node.name)
            elif isinstance(node, ast.arg):
                local.add(node.arg)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                local |= {(a.asname or a.name).split(".")[0]
                          for a in node.names}
            elif isinstance(node, ast.ExceptHandler) and node.name:
                local.add(node.name)
            elif isinstance(node, ast.Global):
                local |= set(node.names)
        used = {n.id for n in ast.walk(fn)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        for name in sorted(used - local):
            lines = [n.lineno for n in ast.walk(fn)
                     if isinstance(n, ast.Name) and n.id == name
                     and isinstance(n.ctx, ast.Load)]
            problems.append(
                f"{path}:{lines[0] if lines else '?'} "
                f"{getattr(fn, 'name', '?')}() 读了未定义的 {name!r}")
        # 递归嵌套函数，outer 用本函数的绑定集
        for node in ast.walk(fn):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node is not fn:
                walk_fn(node, local)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            walk_fn(node, module)
    problems += attr_problems(tree, path, classes)
    return problems


def main() -> int:
    # **两种布局都要认**：仓库/解包目录是 bin/，install.sh 装出来是 lib/。
    # 只认 bin/ 的话，装完的那份 tests/ 里的检查器扫不到任何代码，会静默
    # 「通过」—— 比报错更坏。而打包验证（装到临时前缀再跑一遍）就是靠这个
    # 把它抓出来的。
    if sys.argv[1:]:
        files = [Path(a) for a in sys.argv[1:]]
    else:
        code = (ROOT / "lib" if (ROOT / "lib" / "xteam").exists()
                else ROOT / "bin")
        files = sorted([code / "xteam", code / "xteam_lib.py",
                        *ROOT.glob("tests/*.py")])
    missing = [f for f in files if not f.exists()]
    if missing:
        print("✗ 找不到这些文件："
              + "、".join(str(f.relative_to(ROOT)) for f in missing))
        return 2
    # 类可能定义在另一个文件（IdleTracker 在 xteam_lib.py，却在 xteam 里被调用），
    # 所以先把所有文件的类汇总起来，再逐文件校验。
    all_classes: dict[str, set[str]] = {}
    all_bases: dict[str, list[str]] = {}
    trees = {}
    for f in files:
        if f.exists():
            t = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            trees[f] = t
            for cname, methods in known_attributes(t, f).items():
                all_classes.setdefault(cname, set()).update(methods)
            all_bases.update(class_bases(t))
    all_classes = resolve_inheritance(all_classes, all_bases)
    problems: list[str] = []
    for f, t in trees.items():
        problems += check_file(f, all_classes)
    if problems:
        print(f"✗ {len(problems)} 处读了未定义的名字：")
        for p in problems:
            print("  · " + p)
        return 1
    print(f"✓ {len(files)} 个文件：没有「读了没定义」的引用")
    return 0


if __name__ == "__main__":
    sys.exit(main())