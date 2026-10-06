#!/usr/bin/env bash
# xteam-install — 把 xteam 装到当前用户的 PATH 里。
#
#   ~/.local/bin/xteam              可执行入口（薄壳，转发到 lib）
#   ~/.local/share/xteam/lib/       xteam + xteam_lib.py
#   ~/.local/share/xteam/roles/     三份角色章程
#   ~/.local/share/xteam/templates/ 协议全文
#   ~/.local/share/xteam/skills/    输出规范原文（如 ste，协议里只放精简版省 token）
#   ~/.local/share/xteam/tests/     测试（可选，便于新机器上自检）
#
# 用法：
#   bash install.sh                     装到 ~/.local
#   bash install.sh --prefix ~/.local   同上，显式指定
#   bash install.sh --uninstall         卸载
#   bash install.sh --check             只体检不安装
set -euo pipefail

PREFIX="${HOME}/.local"
MODE="install"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --uninstall) MODE="uninstall"; shift ;;
    --check) MODE="check"; shift ;;
    -h|--help) sed -n '2,17p' "$0" | sed 's/^# //'; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 1 ;;
  esac
done

BINDIR="$PREFIX/bin"
SHAREDIR="$PREFIX/share/xteam"
LIBDIR="$SHAREDIR/lib"

say() { printf '  %s\n' "$1"; }
die() { printf 'install: %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------- 环境体检
preflight() {
  say "检查环境…"
  # Homebrew 的**版本化** Python（如 python@3.12）装在 opt/python@X.Y/libexec/bin，
  # brew 刻意不把它 link 进 PATH（见 formulae.brew.sh 的 python@3.12 说明）。
  # 于是「只装了 python@3.12、没装 python@3」的机器上 `command -v python3`
  # 会失败 —— 而 Formula 里就 depends_on "python@3.12"。这里主动去找一次。
  if ! command -v python3 >/dev/null 2>&1; then
    for _pfx in "${HOMEBREW_PREFIX:-}" /opt/homebrew /usr/local; do
      [ -n "$_pfx" ] || continue
      for _d in "$_pfx"/opt/python@3.*/libexec/bin; do
        if [ -x "$_d/python3" ]; then
          PATH="$_d:$PATH"; export PATH
          break 2
        fi
      done
    done
  fi
  command -v python3 >/dev/null 2>&1 \
    || die "找不到 python3（需要 3.9+）。若用 Homebrew 版本化 Python：
     export PATH=\"\$(brew --prefix python@3.12)/libexec/bin:\$PATH\""
  PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' \
    || die "python3 $PYV 太低，需要 3.9+"
  say "  python3 $PYV ✓"

  command -v herdr >/dev/null 2>&1 \
    || say "  ⚠ 找不到 herdr —— xteam 靠它管 pane/agent，装完也跑不起来"
  [ -d "$SRC/roles" ] || die "源目录不完整：$SRC/roles 不存在"
  [ -d "$SRC/templates" ] || die "源目录不完整：$SRC/templates 不存在"
}

check_perms() {
  # 检查安装目录是否**可被不是你写**。
  #
  # 分两级，因为后果不同：
  #   other 可写（末位 2/6/7）—— 任何用户都能替换 xteam 的 python 代码 = 任意代码执行
  #   group 可写（中间位 2/6/7）—— 共享组的人能改。团队机器上同样是风险
  #
  # **必须解析完整 mode，不能只看末位数字。** 原来的模式 `*[2367]|???[2367]` 只匹配
  # 末位，于是 `674`（owner rwx / group rwx / other r--）被判成安全 —— 而 group
  # 明明可写。实测确认。
  say "检查目录权限…"
  local risky=0
  for d in "$PREFIX" "$SHAREDIR"; do
    [ -d "$d" ] || continue
    local mode
    mode=$(stat -f '%Lp' "$d" 2>/dev/null || stat -c '%a' "$d" 2>/dev/null || echo "")
    case "$mode" in
      ""|*[!0-7]*)
        say "  $d 权限读不出来: [$mode] —— 跳过，请自行确认";;
      *)
        # 右起第三位=other，第二位=group（stat 输出的是三位或四位数，补齐便于取位）
        local m
        m=$(printf '%04d' "$mode")
        local other="${m: -1}" group="${m: -2:1}"
        local why=""
        case "$other" in 2|3|6|7) why="其他用户可写";; esac
        if [ -n "$why" ]; then
          say "  ⚠ $d 权限 $mode —— **$why**"
          risky=1
        elif case "$group" in 2|3|6|7) true;; *) false;; esac; then
          say "  ⚠ $d 权限 $mode —— 组可写（共享机器上同组用户能改代码）"
          risky=1
        else
          say "  $d 权限 $mode ✓"
        fi;;
    esac
  done
  if [ "$risky" = "1" ]; then
    echo
    echo "⚠ 安装目录对其他人可写，别人能替换其中的 xteam 代码（等同任意代码执行）。"
    echo "  建议：chmod 755 \"$PREFIX\" \"$SHAREDIR\""
    echo
  fi
}

if [ "$MODE" = "check" ]; then
  echo "xteam 安装体检"
  preflight
  check_perms
  if command -v xteam >/dev/null 2>&1; then
    say "已安装：$(command -v xteam)"
    xteam doctor || true
  else
    say "尚未安装。跑：bash install.sh"
  fi
  exit 0
fi

# ---------------------------------------------------------------- 卸载
if [ "$MODE" = "uninstall" ]; then
  echo "卸载 xteam"
  [ -f "$BINDIR/xteam" ] && { rm -f "$BINDIR/xteam"; say "已删 $BINDIR/xteam"; }
  [ -d "$SHAREDIR" ] && { rm -rf "$SHAREDIR"; say "已删 $SHAREDIR"; }
  # 旧名 pm-team / pmteam 目录：改名之前装的，卸载一并清掉，
  # 否则旧入口会留在 PATH 里继续跑旧实现
  for legacy in "$BINDIR/pm-team" "$PREFIX/share/pm-team"; do
    [ -e "$legacy" ] && { rm -rf "$legacy"; say "已清理旧版 $legacy"; }
  done
  echo "完成。"
  exit 0
fi

# ---------------------------------------------------------------- 安装
echo "安装 xteam → $PREFIX"
preflight

mkdir -p "$LIBDIR" "$SHAREDIR"
# **先看原始状态再收紧**：顺序反了的话，告警永远看不到「它原来是 777」。
check_perms
# 收紧：目录 755（他人可读不可写），文件 644，入口 755。
# 不设 umask 时，共享组可写的 umask(002) 会让同组用户能改代码 —— 那等同任意代码执行。
chmod 755 "$PREFIX" "$BINDIR" "$SHAREDIR" "$LIBDIR" 2>/dev/null || true
cp "$SRC/bin/xteam" "$LIBDIR/xteam"
cp "$SRC/bin/xteam_lib.py" "$LIBDIR/xteam_lib.py"
chmod +x "$LIBDIR/xteam"

# **目录整体替换，不能 `cp -R src dst`。**
# dst 已存在时 `cp -R` 会把 src 拷成 dst/<basename>（roles/roles），顶层旧文件
# 原封不动 —— 于是升级**静默不生效**：新版被埋进 roles/roles/，而 xteam 读的是
# 顶层那份，于是用户重装后仍在跑旧章程。实测复现过。
# 先拷到 .new 再换掉，顺带清掉源里已删除的陈旧文件。
install_dir() {
  _src="$1"; _dst="$2"
  [ -e "$_src" ] || return 0
  rm -rf "$_dst.new"
  cp -R "$_src" "$_dst.new"
  rm -rf "$_dst"
  mv "$_dst.new" "$_dst"
}

install_dir "$SRC/roles" "$SHAREDIR/roles"
install_dir "$SRC/templates" "$SHAREDIR/templates"
install_dir "$SRC/tests" "$SHAREDIR/tests"
# skills 也要装：PROTOCOL 引了 `skills/ste/SKILL.md` 全文，漏了就断。
# 源里没有 skills/ 时 install_dir 直接跳过（return 0），旧包照装不报错。
install_dir "$SRC/skills" "$SHAREDIR/skills"
# docs 也要装：README 里的相对链接（docs/多项目与契约.md 等）在装完的副本里
# 同样要能点开，否则用户读到的 README 是一堆断链。
install_dir "$SRC/docs" "$SHAREDIR/docs"
# VERSION 必须装：xteam --version 在安装布局下从 share/xteam/VERSION 读，
# 漏了它就只能显示「未知」—— 而「这台机器跑的是哪个构建」正是多机部署时
# 第一个要回答的问题。
[ -f "$SRC/VERSION" ] && cp "$SRC/VERSION" "$SHAREDIR/VERSION"
[ -f "$SRC/PROJECT.md" ] && cp "$SRC/PROJECT.md" "$SHAREDIR/PROJECT.md"
# README 也得装：docs/ 之所以值得装，就是为了上面这个 README 里的相对链接
# （docs/多项目与契约.md 等）在装完的副本里能点开。README 本身没了，那些链接
# 就成了断链里的断链。
[ -f "$SRC/README.md" ] && cp "$SRC/README.md" "$SHAREDIR/README.md"

# 入口是薄壳：它只负责把 PM_TEAM_HOME 指到资源目录后转发。
# 这样 xteam 真正的路径解析只有 _find_root() 一处，不会出现两套逻辑。
if [ -x "$BINDIR/xteam" ]; then
  say "检测到已安装，将覆盖为当前版本"
fi
mkdir -p "$BINDIR"
cat > "$BINDIR/xteam" <<EOF
#!/usr/bin/env bash
# xteam 入口（由 install.sh 生成）。真实实现见 $LIBDIR/xteam
export PM_TEAM_HOME="\${PM_TEAM_HOME:-$SHAREDIR}"
exec python3 "$LIBDIR/xteam" "\$@"
EOF
chmod +x "$BINDIR/xteam"
chmod 644 "$LIBDIR/xteam_lib.py" "$SHAREDIR"/roles/*.md "$SHAREDIR"/templates/*.md 2>/dev/null || true

# 旧版 pm-team 若还在，清掉并说明 —— 否则 PATH 里会同时存在两个命令，
# 用户可能继续调用旧入口，以为自己在用新版（却跑着旧实现）。
# 必须放在文件复制**之后**：share/xteam 与 SHAREDIR 同名，先删会被后续步骤重建。
if [ -e "$BINDIR/pm-team" ]; then
  rm -f "$BINDIR/pm-team"
  say "已清理旧版入口 $BINDIR/pm-team（本项目已改名为 xteam）"
fi
if [ "$SHAREDIR" != "$PREFIX/share/pm-team" ] && [ -d "$PREFIX/share/pm-team" ]; then
  rm -rf "$PREFIX/share/pm-team"
  say "已清理旧版 $PREFIX/share/pm-team"
fi

# ---------------------------------------------------------------- 验证
echo
echo "收紧后复查…"
check_perms
echo
echo "验证安装…"
"$BINDIR/xteam" doctor 2>&1 | sed 's/^/  /' || true

case ":$PATH:" in
  *":$BINDIR:"*) ;;
  *) echo; echo "⚠ $BINDIR 不在 PATH 里，加一行到 shell 配置："
     echo "    echo 'export PATH=\"$BINDIR:\$PATH\"' >> ~/.zshrc && source ~/.zshrc" ;;
esac

echo
echo "完成。用法："
echo "  cd /你的/项目 && git init"
echo "  xteam up . --watch"
echo
echo "卸载：bash install.sh --uninstall"
