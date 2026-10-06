#!/usr/bin/env bash
# 打包 xteam 并**验证这个包真的能用**。
#
# 「打出来」不等于「能用」。所以这里不是打个 tar 就完事，而是：
#   打 tar → 解到临时目录 → 装到临时前缀 → 跑 doctor → 跑三层门禁前两层
#   → 把包和校验和一起留在 dist/
# 任何一步失败就删掉半成品并退出，不留一个看起来正常的坏包。
#
# 两种打包来源：
#   git archive（默认）—— 只含**已提交**的文件，靠 export-ignore 剔除工具状态。
#                         干净、可复现，但要求有 git 且工作区干净。
#   --worktree       —— 直接打工作树，**不含 .git**。用于「把项目发到另一台
#                         机器上发布 / 接着开发」：不依赖 git，也不依赖提交状态。
#
# 用法：
#   bash pack.sh                # 从 git 打包
#   bash pack.sh --worktree     # 从工作树打包（不含 .git），发布/迁移用
#   bash pack.sh --with-history  # 额外产出 .bundle（压过的完整 git 历史）
#   bash pack.sh --no-verify    # 只打包不验证（不推荐）
#   bash pack.sh --worktree --no-verify
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO"
# 刻意不用 git rev-parse 推仓库根 —— 解包副本里没有 .git，那会直接报错。

say() { printf '  %s\n' "$1"; }
die() { printf 'pack: %s\n' "$1" >&2; exit 1; }

VERSION="$(tr -d '[:space:]' < VERSION)"
NAME="xteam-$VERSION"
DIST="$REPO/dist"
VERIFY=1
WORKTREE=0
WITH_HISTORY=0
NEED_BUMP=0
BUMP=""
# 注意：die/say 必须在参数解析**之前**定义，否则未知参数会报
# 「die: command not found」而不是给出用法提示。
for a in "$@"; do
  case "$a" in
    --no-verify)   VERIFY=0 ;;
    --worktree)    WORKTREE=1 ;;
    --with-history) WITH_HISTORY=1 ;;
    --bump)  NEED_BUMP=1 ;;
    --bump=*) BUMP="${a#--bump=}" ;;
    *) die "未知参数: $a （见脚本头部用法）" ;;
  esac
done

[ -n "$VERSION" ] || die "VERSION 文件是空的"

# ---------------------------------------------------------------- 0.5 升版本
# 以前 VERSION 只能手改，于是永远是 0.1.1。后果不只是数字难看：
#   · 包名恒为 xteam-0.1.1.tar.gz → 每次打包**覆盖**上一个包
#   · brew-publish 的 RELEASE_TAG 恒为 xteam-v0.1.1 → 「Release 已存在，跳过」
#     → 改了代码也永远发不出去（发布链路死锁）
#   · xteam --version 两台机器显示同一个号，却跑着不同的代码 → 无法分辨
# 所以这里提供 --bump，并在同号但内容不同时**拒绝打包**（见下）。
bump_version() {
  # 语义化 X.Y.Z；缺段按 0 补，多余的段原样保留（不擅自丢信息）。
  IFS=. read -r MAJ MIN PAT <<EOF
$1
EOF
  MAJ="${MAJ:-0}"; MIN="${MIN:-0}"; PAT="${PAT:-0}"
  case "$1" in
    *[!0-9.]*|"") die "VERSION '$1' 不是 X.Y.Z 形式，没法自动升" ;;
  esac
  case "$BUMP" in
    patch) echo "$MAJ.$MIN.$((PAT + 1))" ;;
    minor) echo "$MAJ.$((MIN + 1)).0" ;;
    major) echo "$((MAJ + 1)).0.0" ;;
    *)     die "--bump 只接受 patch / minor / major，收到 '$BUMP'" ;;
  esac
}

if [ "$NEED_BUMP" = "1" ] || [ -n "$BUMP" ]; then
  [ "$BUMP" = "1" ] && BUMP="patch"
  NEWV="$(bump_version "$VERSION")"
  say "版本 $VERSION → $NEWV （$BUMP ）"
  printf '%s\n' "$NEWV" > VERSION
  VERSION="$NEWV"
  NAME="xteam-$VERSION"
  # 升了版本就**提交它** —— 否则工作区变脏，接下来 git 模式的干净检查会拦下，
  # 而且 brew-publish 的 provenance 对账也会说「包不是当前 HEAD 打的」。
  if git rev-parse --git-dir >/dev/null 2>&1; then
    git -c user.email=pm-team@local -c user.name=pm-team commit -q -m "release: $VERSION" -- VERSION \
      && say "已提交 VERSION（release: $VERSION ）"
  fi
fi

# 距离上次打包已经有多少提交？**打包这件事容易被忘掉**，所以让脚本自己喊出来，
# 而不是等人来提醒。
# **解包副本里没有 .git**（那正是「发到另一台机器接着开发」的形态），
# 所以所有 git 调用都必须先确认在仓库里 —— 否则 `git rev-list` 报错，
# 打包在最后一步失败，而代码其实好好的。
HAVE_GIT=0
if [ "$WORKTREE" = "0" ] && git rev-parse --git-dir >/dev/null 2>&1; then
  HAVE_GIT=1
fi
LAST_TAG=""
# 注意末尾的 || true：**set -e 下 `[ ... ] && ...` 短路返回 1 会直接杀掉脚本**。
[ "$HAVE_GIT" = "1" ] && LAST_TAG="$(git tag --list 'xteam-v*' --sort=-creatordate | head -1)" || true
if [ -n "$LAST_TAG" ]; then

# **同一版本号打出不同的内容** —— 拒绝。
# 忘了升版本时后果有三：包名不变 → 覆盖上一次的包；brew 的 Release tag 不变 →
# 「已存在，跳过」，改了代码也发不出去；两台机器 xteam --version 显示同一个号
# 却跑着不同代码。
#
# 必须在 `rm -rf "$DIST"` **之前**判断，否则旧包早被清掉了，这里永远看不到。
if [ -f "$DIST/$NAME.provenance" ] && [ "$HAVE_GIT" = "1" ]; then
  OLD_COMMIT="$(sed -n 's/^commit=//p' "$DIST/$NAME.provenance" 2>/dev/null | head -1)"
  NOW_COMMIT="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  OLD_SHORT="$(printf '%s' "$OLD_COMMIT" | cut -c1-7)"
  NOW_SHORT="$(printf '%s' "$NOW_COMMIT" | cut -c1-7)"
  if [ -n "$OLD_COMMIT" ] && [ "$OLD_COMMIT" != unknown ] \
     && [ "$OLD_COMMIT" != "$NOW_COMMIT" ] && [ -f "$DIST/$NAME.tar.gz" ]; then
    die "dist/ 里已有 $NAME.tar.gz，但它打自另一个提交
   旧提交：$OLD_SHORT
   当前  ：$NOW_SHORT
   同号不同内容会覆盖旧包，也让 brew 发布撞上「Release 已存在」。
   先升版本：bash pack.sh --bump=patch"
  fi
fi

  N="$(git rev-list --count "$LAST_TAG"..HEAD 2>/dev/null || echo 0)"
  if [ "$N" -gt 0 ]; then
    say f"距 ${LAST_TAG} 已有 ${N} 个提交；请确认 VERSION 是否该升"
  fi
elif [ "$HAVE_GIT" = "1" ]; then
  N="$(git rev-list --count HEAD)"
  say "首次打包（仓库共 $N 个提交）"
else
  say "工作树模式（当前目录不是 git 仓库，跳过版本新鲜度检查）"
fi

# ---------------------------------------------------------------- 1. 干净？
# 只认「代码/文档有没有未提交改动」。开发工具自己写的状态文件
# （.memsearch/ 索引、review 工具的历史）不算 —— 它们随时在变，拿它们卡住
# 打包只会让人养成「先 stash 再打包」的习惯，然后忘 stash。
if [ "$WORKTREE" = "0" ]; then
  # 只有 git 模式才要求干净 —— 它打的是已提交内容，工作区有改动就说明
  # 你以为打进去的东西其实没进。**工作树模式打的就是工作树本身**，
  # 「有未提交改动」在那条路径上是正常的，不该拦。
  # 按**路径**忽略工具状态，而不是按状态码。
  # 原来只滤掉 `^ M `（修改），于是工具把 .memsearch/.index.pid 删掉时状态是
  # ` D`，没被滤掉 → 报「工作区不干净」，而脚本明明声称忽略开发工具状态。
  # 状态码还有 ?? / A / D / R…，逐个列永远漏；按路径一刀切才干净。
  # 注意 porcelain 的格式是 `XY<空格>path`，删除是 `D ` + **两个**空格，
  # 所以正则要 `^..[ ]`（两个任意字符 + 一个空格），写成 `^.. ` 少一个空格就漏。
  DIRTY="$(git status --porcelain \
    | grep -vE '^..[ ]\.memsearch/' \
    | grep -vE '^..[ ]\.brooks-lint-history\.json' || true)"
  if [ -n "$DIRTY" ]; then
    printf '%s\n' "$DIRTY"
    die "工作区不干净（上面这些未提交）。半成品不该被打成包。"
  fi
  # **纯 gitignored 文件不会出现在 porcelain 里**，但它们仍会被工作树模式打进包。
  # git 模式走 git archive（只含已跟踪文件），所以本来就干净；这里只提示 ——
  # 否则「本地有 Formula/ 所以包不干净」和「源码真的改了」分不清。
  if [ -d "$REPO/Formula" ]; then
    say "  · 本地有 Formula/ —— git 模式不会打进包（它不在 git 里）"
  fi
fi

# ---------------------------------------------------------------- 2. 打 tar
rm -rf "$DIST"
mkdir -p "$DIST"
# 用 git archive 而不是 tar：只打**被跟踪的**文件，且能靠 export-ignore
# 剔除开发工具的状态（.memsearch/、.brooks-lint-history.json、__pycache__）。
if [ "$WORKTREE" = "1" ]; then
  # 工作树模式：**排除 .git**，但保留 tests/ pack.sh 等开发工具，
  # 让目标机器上能接着开发、跑门禁、改完再打包。
  say "从工作树打包（不含 .git，保留开发工具链）"
  # 用符号链接造出顶层目录名，而不是 tar --transform ——
  # **macOS 自带的是 bsdtar，不支持 --transform**（GNU tar 才有）。
  # 符号链接的办法两边都能用。
  # **不能用 `tar -h` 解引用 staging 链接** —— `-h` 会解引用**所有**链接，
  # 包括仓库里指向仓库外的那些，于是本机私��文件会被打进发布包（实测复现：
  # 一个指向 /tmp 的链接，文件内容原样出现在 tar 里）。
  #
  # 改为「先复制、后打包」：rsync/cp -R 默认**保留**符号链接，链接就还是链接；
  # 只有打包 stage 目录本身（不是链接）时不需要任何解引用。
  # **发布包里不该有指向仓库外的链接。** 复制阶段已经不会解引用它们（链接原样
  # 保留，所以不泄漏内容），但发布包里留一个在别人机器上必然悬空的链接，使用者
  # 点开会莫名失败 —— 早说清比让人踩强。
  #
  # 判据：把链接目标解析成绝对路径后，看它是否还在**仓库目录内**。
  # 相对链接（指向仓库里的另一个文件）是正常的，`.pyc` 之类不算越界。
  OUTSIDE="$(python3 - "$REPO" <<'PYCHK'
import os, sys
root = os.path.realpath(sys.argv[1])
for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
    dirnames[:] = [d for d in dirnames
                   if d not in (".git", "dist", ".xteam", ".pm", ".memsearch",
                                "__pycache__")]
    for name in filenames + dirnames:
        p = os.path.join(dirpath, name)
        if not os.path.islink(p):
            continue
        target = os.path.realpath(p)
        if not (target == root or target.startswith(root + os.sep)):
            print(f"{os.path.relpath(p, root)} -> {os.readlink(p)}")
PYCHK
)"
  if [ -n "$OUTSIDE" ]; then
    printf '%s\n' "$OUTSIDE" | sed 's/^/  /'
    die "仓库里有指向仓库外的链接。发布包里它们在别的机器上必然悬空 —— 先处理掉。"
  fi

  STAGE="$DIST/.stage"
  rm -rf "$STAGE"; mkdir -p "$STAGE/$NAME"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --links \
      --exclude='.git/' --exclude='dist/' --exclude='.xteam/' --exclude='.pm/' \
      --exclude='.memsearch/' --exclude='.brooks-lint-history.json' \
      --exclude='__pycache__/' --exclude='*.pyc' --exclude='.DS_Store' \
      --exclude='review-report.md' --exclude='Formula/' \
      --exclude='brew-publish.sh' \
      "$REPO/" "$STAGE/$NAME/"
  else
    # 没有 rsync：cp -R 在 macOS/BSD 下默认保留符号链接（-P）；-L 才会解引用，
    # 所以这里**不能**加 -L。
    # 注意这条路径用 tar 的 --exclude，它**不认 .gitignore** —— 所以 Formula/
    # 和 brew-publish.sh 必须在这里再写一遍，否则本地 gitignore 掉的发布脚本
    # 会被打进包（rsync 那条同理）。
    (cd "$REPO" && tar --exclude='.git' --exclude='dist' --exclude='.xteam' \
      --exclude='.pm' --exclude='.memsearch' --exclude='__pycache__' \
      --exclude='*.pyc' --exclude='.DS_Store' --exclude='review-report.md' \
      --exclude='Formula' --exclude='brew-publish.sh' \
      -cf - .) | (cd "$STAGE/$NAME" && tar -xf -)
  fi
  tar -C "$STAGE" -cf "$DIST/$NAME.tar" "$NAME"
  rm -rf "$STAGE"
else
  say "从 git 打包（只含已提交文件）"
  git archive --format=tar --prefix="$NAME/" HEAD > "$DIST/$NAME.tar"
fi
gzip -9 "$DIST/$NAME.tar"

CONTENTS="$(tar tzf "$DIST/$NAME.tar.gz")"
for must in "$NAME/bin/xteam" "$NAME/bin/xteam_lib.py" "$NAME/install.sh" \
            "$NAME/roles/pm.md" "$NAME/templates/PROTOCOL.md" \
            "$NAME/skills/ste/SKILL.md" \
            "$NAME/README.md" "$NAME/INSTALL.md" "$NAME/VERSION" \
            "$NAME/LICENSE" "$NAME/CHANGELOG.md" "$NAME/tests/smoke.sh"; do
  echo "$CONTENTS" | grep -qx "$must" || die "包缺 $must"
done

# **相对链接不许断。** docs/ 值得打包的唯一理由是 README 里的相对链接能点开。
# 而「文件在包里」和「有人在文档里指向它」是两件事 —— 漏装只在用户点开那一刻
# 才发现，那时已经在别人机器上了。所以抽出所有 docs/、skills/ 链接，逐个确认包里真有。
#
# **必须用 for 而不是 `while ... done < pipe`**：管道会起子 shell，循环里
# 累加的 BROKEN 带不出来，于是断链被静默吞掉 —— 正是这个检查要防的那类失败。
BROKEN=""
# 只查 **README.md**：它和被它指向的 docs/ 一起装进 share/xteam/，相对链接在
# 装完的副本里成立。
# templates/PROTOCOL.md **不查** —— 它会被拷进**用户项目的** .xteam/，而 skills/
# 留在 xteam 自己的安装目录里，两处不在一棵树上，相对链接注定解析不到。
# 已把 PROTOCOL 里那个路径改成「安装目录下的 skills/…（doctor 会打印绝对路径）」。
for doc in README.md; do
  dir="$(dirname "$doc")"
  # 两种写法都算「指向某文件」：markdown 链接 ](docs/x.md)，以及正文里用
  # 反引号直接写路径（`skills/ste/SKILL.md`）。后者不算链接，但同样会在
  # 用户「照着去找」时落空。
  links="$(sed -nE \
    -e 's/.*\]\((docs\/[^)#]+|skills\/[^)#]+)\).*/\1/p' \
    -e 's/.*`(docs\/[^`]+|skills\/[^`]+)`.*/\1/p' \
    "$doc" 2>/dev/null || true)"
  for link in $links; do
    # README 在包根，dirname 给 "."，直接拼会变成 "xteam-0.1.5/./docs/x.md"，
    # 而 tar 清单里是 "xteam-0.1.5/docs/x.md" —— 前缀对不上，于是好包也报断链。
    case "$dir" in
      .) target="$NAME/$link" ;;
      *) target="$NAME/$dir/$link" ;;
    esac
    if ! echo "$CONTENTS" | grep -q "^$target\$"; then
      BROKEN="$BROKEN $doc->$link"
    fi
  done
done
[ -z "$BROKEN" ] || die "文档里指向的文件在包里断了：$BROKEN"
say "文档指向的 docs/ + skills/ 文件都在包里"
say "包内容完整（$(echo "$CONTENTS" | wc -l | tr -d ' ') 项）"

# ---------------------------------------------------------------- 3. 验证
if [ "$VERIFY" = "1" ]; then
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  say "解包到临时目录，用**临时前缀**安装，验证这个包本身能用…"
  tar xzf "$DIST/$NAME.tar.gz" -C "$TMP"
  PREFIX="$TMP/prefix"
  bash "$TMP/$NAME/install.sh" --prefix "$PREFIX" >"$TMP/install.log" 2>&1 \
    || { cat "$TMP/install.log"; die "install.sh 失败"; }
  [ -x "$PREFIX/bin/xteam" ] || die "装完没有可执行的 xteam"

  "$PREFIX/bin/xteam" --help >/dev/null || die "装完的 xteam --help 跑不起来"
  say "  ✓ install.sh + --help"

  # 资源跟着装了吗（docs/ 是相对链接，漏了就断）
  [ -f "$PREFIX/share/xteam/docs/多项目与契约.md" ] \
    || die "docs/ 没装过去 —— README 里的相对链接会全断"
  say "  ✓ 资源（roles/templates/docs）就位"

  # 门禁前两层：静态 + 单测。端到端要 herdr 和真 agent，由使用者在目标机跑。
  python3 "$PREFIX/share/xteam/tests/lint_names.py" >/dev/null \
    || die "装出来的代码过不了 lint_names"
  python3 "$PREFIX/share/xteam/tests/lint_shell.py" \
    "$PREFIX/share/xteam/install.sh" "$PREFIX/share/xteam/tests/smoke.sh" >/dev/null \
    || die "装出来的 shell 过不了 lint_shell"
  N=$(python3 "$PREFIX/share/xteam/tests/test_protocol.py" 2>&1 | grep -c '✓' || true)
  python3 "$PREFIX/share/xteam/tests/test_protocol.py" >/dev/null \
    || die "装出来的代码协议单测不过"
  say "  ✓ 静态层 + 单测层（$N 条断言）"

  # 工作树模式要额外验证「解包副本还能接着开发」：能跑门禁前两层、能再打包。
  if [ "$WORKTREE" = "1" ]; then
    # 失败时**必须把门禁的输出打出来** —— 只说「跑不过」而不说为什么，
    # 等于让打包者再去别处重跑一遍才能定位，白白多花两分钟。
    # CI 上没有 herdr，第 3 层必然失败。用 --no-e2e 跑前两层 ——
    # 比整段 --no-verify 强得多：那样连「解包副本能装能跑单测」都不验了。
    # 有 herdr 时仍然跑完整三层。
    if command -v herdr >/dev/null 2>&1; then
      GATE_MODE="full"
      GATE_LABEL="解包副本能独立跑三层门禁（可在目标机直接开发）"
    else
      GATE_MODE="front2"
      GATE_LABEL="解包副本能跑门前两层（本机无 herdr，端到端在目标机验）"
    fi
    # **不用空数组 + "${arr[@]}" 传可选参数。** bash 3.2（macOS 自带）在
    # `set -u` 下展开**空**数组会报 `arr[@]: unbound variable` —— 而本机有
    # herdr 时数组恰好是空的，于是「有 herdr」那条路径必然炸，且报错完全指不到
    # 真正原因。改成两个显式分支。
    if [ "$GATE_MODE" = "full" ]; then
      _gate() { (cd "$TMP/$NAME" && bash tests/smoke.sh "$@" >"$TMP/gate.log" 2>&1); }
    else
      _gate() { (cd "$TMP/$NAME" && bash tests/smoke.sh --no-e2e >"$TMP/gate.log" 2>&1); }
    fi
    if _gate; then
      say "  ✓ $GATE_LABEL"
    else
      printf '%s\n' "──── 解包副本的门禁输出 ────"
      tail -25 "$TMP/gate.log"
      printf '%s\n' "──────────────────────────"
      die "解包副本跑不过门禁 —— 目标机器上没法接着开发"
    fi
    if (cd "$TMP/$NAME" && bash pack.sh --worktree --no-verify >"$TMP/repack.log" 2>&1); then
      say "  ✓ 解包副本能再次打包（发布链路自洽）"
    else
      printf '%s\n' "──── 副本再打包的输出 ────"; tail -15 "$TMP/repack.log"
      printf '%s\n' "─────────────────────────"
      die "解包副本不能再打包"
    fi
  fi

  if command -v herdr >/dev/null 2>&1; then
    say "  · 检测到 herdr —— 端到端层请在目标机器上跑（会真起三个 pane）"
  else
    say "  · 本机没有 herdr，跳过端到端提示"
  fi
fi

# ------------------------------------------------- 3.5 可选：git 历史（bundle）
if [ "$WITH_HISTORY" = "1" ]; then
  if git rev-parse --git-dir >/dev/null 2>&1; then
    if git bundle create "$DIST/$NAME.bundle" --all >/dev/null 2>&1; then
      say "已产出 git 历史：$NAME.bundle（$(du -h "$DIST/$NAME.bundle"|cut -f1)）"
    else
      say "⚠ git bundle 失败（不影响源码包）"
    fi
  else
    say "⚠ 当前目录不是 git 仓库，跳过 --with-history"
  fi
fi

# ---------------------------------------------------------------- 4. 校验和
cd "$DIST"
if command -v shasum >/dev/null 2>&1; then
  shasum -a 256 "$NAME.tar.gz" > "$NAME.tar.gz.sha256"
elif command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256"
fi

echo
printf '包已就绪：\n'
ls -lh "$DIST"/ | tail -n +2 | awk '{printf "  %-28s %s\n", $9, $5}'
[ "$HAVE_GIT" = "1" ] && [ -n "$LAST_TAG" ] \
  && git tag -a "xteam-v$VERSION" -m "release $VERSION" 2>/dev/null \
  && say "已打 tag xteam-v$VERSION"
true
echo
cat "$DIST/$NAME.tar.gz.sha256" 2>/dev/null | sed 's/^/  /'
echo
echo "  解压：tar xzf $NAME.tar.gz && cd $NAME && bash install.sh"
echo "  验证：xteam doctor && bash tests/smoke.sh"

# 记下这个包是**哪次提交**打的。少了它，brew-publish 只看 tarball 存不存在就发 ——
# 于是改完源码忘了重打包，发布出去的仍是旧包，而且 Release 名字一样（同一个
# VERSION），看都看不出来。
{
  printf 'version=%s\n' "$VERSION"
  if git rev-parse --git-dir >/dev/null 2>&1; then
    printf 'commit=%s\n' "$(git rev-parse HEAD)"
    if [ -n "$(git status --porcelain | grep -vE '^..[ ]\.memsearch/' \
        | grep -vE '^..[ ]\.brooks-lint-history\.json')" ]; then
      printf 'dirty=yes\n'
    fi
  else
    printf 'commit=unknown\n'
  fi
  printf 'built=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$NAME.provenance"
