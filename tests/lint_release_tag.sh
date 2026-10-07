#!/usr/bin/env bash
# release tag 转义守卫（F-17）。
#
# `make release` 配方里传给 shell 的 ${NEWV} 必须写成 $${NEWV}——写漏了 make
# 会把 ${NEWV} 当自己的变量吃掉，打出来的 tag 是裸 `xteam-v`（版本号为空）。
# 11:02 实测踩过：CI 守卫拦下没脏发布，但本地留了个空 tag 要人工收拾。
#
# `make -n release` 渲染的是 **make 展开之后**的配方，所以这个 bug 静态就能抓到：
#   修复前：裸 "xteam-v" = 3、xteam-v${NEWV} = 0
#   修复后：裸 "xteam-v" = 0、xteam-v${NEWV} ≥ 1
# 两个方向都要判（F-10 教训：只查一边的断言是死的）。
#
# 用法：bash tests/lint_release_tag.sh [Makefile路径]
#   无参默认仓库根 Makefile；传路径可指任意 Makefile（验修复前版本用，
#   例如 `git show <rev>:Makefile > /tmp/old.mk` 后喂进来）。
# 退出码 0 = 通过，非 0 = 有必须修的问题。

set -u

MK="${1:-Makefile}"

if [ ! -f "$MK" ]; then
  echo "lint_release_tag: Makefile 不存在：$MK" >&2
  exit 1
fi

# make -n 只展开不执行；release 依赖 verify，展开输出含两层配方，只看 tag 段。
OUT="$(make -f "$MK" -n release 2>&1)" || {
  echo "lint_release_tag: make -n release 展开失败：$MK" >&2
  printf '%s\n' "$OUT" >&2
  exit 1
}

BARE="$(printf '%s\n' "$OUT" | grep -c '"xteam-v"')"
VAR="$(printf '%s\n' "$OUT" | grep -c 'xteam-v\${NEWV}')"

RC=0
if [ "$BARE" -ne 0 ]; then
  echo "lint_release_tag: 裸 tag 名 \"xteam-v\" ×${BARE} —— \${NEWV} 被 make 吃掉了，" >&2
  echo '  配方里要写成 $${NEWV}（$$ 转义后才轮到 shell 展开）：'"$MK" >&2
  RC=1
fi
if [ "$VAR" -lt 1 ]; then
  echo "lint_release_tag: 未见 xteam-v\${NEWV} —— 版本号根本没拼进 tag 名：$MK" >&2
  RC=1
fi

[ "$RC" -eq 0 ] && echo "lint_release_tag: $MK 通过（裸名 0、xteam-v\${NEWV} ×${VAR}）"
exit "$RC"
