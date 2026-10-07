#!/usr/bin/env bash
# e2e_multi.sh —— 多项目治理的 hermetic 端到端验证。
#
# PROJECT.md §3.1 里「多项目治理 / 跨项目排序 / API 契约」三行从断言变成可重复
# 运行的机器判据：每个场景在 mktemp 里复刻真实布局（真 git init + 真子目录 +
# 真手写 projects.json），从 CLI 表面一路验到文件落盘。
#
# hermetic 的含义：
#   · 不联网，不用 agent CLI；
#   · 不需要装着的真 herdr —— restore/status 会调它（F-3），脚本自建最小替身
#     放到 PATH 最前；
#   · 每个场景独立 mktemp 仓库，trap 统一清理 —— 幂等，连跑多少次结果一样；
#   · 绝不写 ~/.xteam/ 以外的用户路径。
#
# 调 CLI 一律走「仓库里这份」bin/xteam，不用 PATH 上安装的版本 —— 否则验的是
# 别人机器上那份，不是当前代码。
#
# 用法：bash tests/e2e_multi.sh   （无参数；退出码 0 = 全过，末行 N/N 通过）
set -uo pipefail
# 不留解释器字节码缓存：整套件的所有 python3 调用（xin/xin12/xin13/xin16
# 与内联 python3 -c）都继承它，「不写用户目录」在字节级成立（F-10）。
export PYTHONDONTWRITEBYTECODE=1

if [ $# -gt 0 ]; then
  echo "用法：bash tests/e2e_multi.sh（无参数）" >&2
  exit 2
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"

TOTAL=0
PASS=0
ok()  { TOTAL=$((TOTAL+1)); PASS=$((PASS+1)); printf '  ✓ %s\n' "$1"; }
bad() { TOTAL=$((TOTAL+1)); printf '  ✗ %s\n     %s\n' "$1" "${2:-}"; }
step() { printf '\n[%s] %s\n' "$1" "$2"; }

# ---------------------------------------------------------------- 环境
# 所有临时产物都挂在 BASE 下，trap 一次清干净。
BASE="$(mktemp -d)"
cleanup() { rm -rf "$BASE"; }
trap cleanup EXIT

# herdr 最小替身：restore/status 只需要一个不报错、返回空表的 herdr。
# 这是**外部进程边界**的替身（herdr 是另一个程序），断言的仍是 xteam 自己的输出。
mkdir -p "$BASE/bin"
cat > "$BASE/bin/herdr" <<'EOF'
#!/bin/sh
printf '{"result": {"workspaces": [], "agents": [], "tabs": []}}\n'
EOF
chmod +x "$BASE/bin/herdr"
PATH="$BASE/bin:$PATH"
export PATH

# ------------------------------------------------------------ 工具函数

# 在临时仓根下跑仓库版 xteam；stdin 封死，防止意外交互把脚本挂住。
xin() {
  local t="$1"; shift
  (cd "$t" && PYTHONDONTWRITEBYTECODE=1 python3 "$REPO/bin/xteam" --project "$t" "$@" </dev/null)
}

# 建一个 git 仓 + 若干空的直接子目录（init 只要求直接子目录存在）。
mkrepo() {
  local d="$1"; shift
  mkdir -p "$d"
  (cd "$d" && git init -q . \
    && printf '# tmp\n' > README.md \
    && git add -A \
    && git -c user.email=t@t -c user.name=t commit -qm init >/dev/null)
  local s
  for s in "$@"; do mkdir -p "$d/$s"; done
}

# 建一个共识阶段的切片骨架：spec.md + spec.json(round=1) + 给定 task.json。
mk_task() {
  local d="$1/.xteam/tasks/$2"
  mkdir -p "$d"
  printf '# spec\n' > "$d/spec.md"
  printf '{"round": 1}\n' > "$d/spec.json"
  printf '%s\n' "$3" > "$d/task.json"
}

LAST_OUT=""
LAST_RC=0
run() { LAST_OUT="$("$@" 2>&1)"; LAST_RC=$?; }

# assert_rc <描述> <0|nz>
assert_rc() {
  if [ "$2" = "0" ]; then
    [ "$LAST_RC" -eq 0 ] && ok "$1" \
      || bad "$1" "$(printf 'rc=%s（期望 0）：%s' "$LAST_RC" "$LAST_OUT")"
  else
    [ "$LAST_RC" -ne 0 ] && ok "$1" \
      || bad "$1" "$(printf 'rc=0（期望非 0）：%s' "$LAST_OUT")"
  fi
}

# assert_has <描述> <LAST_OUT 里应含的固定子串>
assert_has() {
  printf '%s' "$LAST_OUT" | grep -qF -- "$2" && ok "$1" \
    || bad "$1" "$(printf '输出缺「%s」：%s' "$2" "$LAST_OUT")"
}

# assert_line <描述> <行定位子串> <该行应含子串> [该行不应含的子串]
# 用于 restore/status 这种一行一个切片的输出：先定位行，再查内容。
assert_line() {
  local line
  line="$(printf '%s\n' "$LAST_OUT" | grep -F -- "$2" | head -3)"
  if [ -z "$line" ]; then
    bad "$1" "$(printf '找不到含「%s」的行：%s' "$2" "$LAST_OUT")"; return
  fi
  if [ -n "$3" ] && ! printf '%s' "$line" | grep -qF -- "$3"; then
    bad "$1" "$(printf '该行不含「%s」：%s' "$3" "$line")"; return
  fi
  if [ -n "${4:-}" ] && printf '%s' "$line" | grep -qF -- "$4"; then
    bad "$1" "$(printf '该行不该含「%s」：%s' "$4" "$line")"; return
  fi
  ok "$1"
}

assert_exists() { [ -e "$2" ] && ok "$1" || bad "$1" "不存在：${2}"; }
assert_absent() { [ ! -e "$2" ] && ok "$1" || bad "$1" "不该存在：${2}"; }

# assert_file_has <描述> <文件> <应含的固定子串>
assert_file_has() {
  if [ -f "$2" ] && grep -qF -- "$3" "$2"; then
    ok "$1"
  else
    bad "$1" "$(printf '%s 缺「%s」（或文件不存在）' "$2" "$3")"
  fi
}

# ================================================================ 场景

step "MP-01" "多项目声明与注册表"
T="$BASE/mp01"; mkrepo "$T" fe api admin
run xin "$T" init --repo-kind multi-api --projects fe,api,admin --shared-api api
assert_rc  "MP-01.1 init multi-api rc=0" 0
assert_has "MP-01.1 输出：共享契约层提示下游" \
  "共享契约层：api（改它会提醒下游：fe, admin）"
run python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
p={x["name"]:x for x in d["projects"]}
sys.exit(0 if (p["api"]["kind"]=="shared-api"
               and p["api"]["consumers"]==["fe","admin"]
               and p["fe"]["kind"]=="app" and p["admin"]["kind"]=="app") else 1)
' "$T/.xteam/projects.json"
assert_rc "MP-01.2 projects.json：api=shared-api consumers=[fe,admin]；fe/admin=app" 0
run xin "$T" projects
assert_rc  "MP-01.3 projects rc=0" 0
assert_has "MP-01.3 projects：api 标共享契约+消费方" "共享契约 被 fe、admin 消费"
assert_exists "MP-01.4 .xteam/contracts/ 已建" "$T/.xteam/contracts"
run git -C "$T" status --porcelain
[ "$LAST_OUT" = "?? .gitignore" ] \
  && ok "MP-01.5 git status 恰好只有「?? .gitignore」（.xteam 已忽略）" \
  || bad "MP-01.5 git status 不符" "实测：$LAST_OUT"
run xin "$T" init --repo-kind multi-api --projects fe,api,admin --shared-api api
assert_rc  "MP-01.6 重复 init（无 --force）被拒" nz
assert_has "MP-01.6 stderr：projects.json 已存在" ".xteam/projects.json 已存在"

step "MP-02" "无共享契约层的多项目"
T="$BASE/mp02"; mkrepo "$T" a b
run xin "$T" init --repo-kind multi-app --projects a,b
assert_rc     "MP-02.1 init multi-app rc=0" 0
assert_absent "MP-02.2 不建 .xteam/contracts/" "$T/.xteam/contracts"

step "MP-03" "声明校验：不存在的项目被拒"
run xin "$T" init --force --repo-kind multi-api --projects nope
assert_rc  "MP-03.1 init --projects nope 被拒" nz
assert_has "MP-03.1 stderr：项目不存在" "项目 'nope' 在"
assert_has "MP-03.1 stderr：只允许直接子目录" "只允许直接子目录"

step "MP-04" "单项目 → 多项目迁移不丢东西"
T="$BASE/mp04"; mkrepo "$T" fe api admin
run xin "$T" init --repo-kind single
assert_rc "MP-04.1 init single rc=0" 0
mk_task "$T" keep '{"project": "fe"}'
B_QUEUE="$(cat "$T/.xteam/QUEUE.md")"
B_LIST="$(ls "$T/.xteam/tasks")"
run xin "$T" init --force --repo-kind multi-api --projects fe,api,admin --shared-api api
assert_rc "MP-04.2 --force 迁移 multi-api rc=0" 0
[ "$(cat "$T/.xteam/QUEUE.md")" = "$B_QUEUE" ] \
  && ok "MP-04.3 QUEUE.md 逐字节相同" \
  || bad "MP-04.3 QUEUE.md 被改动" ""
[ "$(ls "$T/.xteam/tasks")" = "$B_LIST" ] \
  && ok "MP-04.3 tasks/ 目录清单相同" \
  || bad "MP-04.3 tasks/ 清单变了" "前：$B_LIST 后：$(ls "$T/.xteam/tasks")"
assert_exists "MP-04.3 既有切片 keep 仍在" "$T/.xteam/tasks/keep"

step "MP-05" "多项目下切片必须声明项目"
T="$BASE/mp05"; mkrepo "$T" fe api admin
run xin "$T" init --repo-kind multi-api --projects fe,api,admin --shared-api api
assert_rc "布景 init multi-api rc=0" 0
mk_task "$T" s1 '{"depends_on": []}'
run xin "$T" restore
assert_line "MP-05.1 未声明 project → pm:name-project" "s1  →" "pm:name-project"
printf '{"project": "fe", "depends_on": []}\n' > "$T/.xteam/tasks/s1/task.json"
run xin "$T" restore
assert_line "MP-05.2 补上 project → 变 tl:assess" "s1  →" "tl:assess"

step "MP-06" "跨项目排序：环 / 拼错 / 合法等待三类可分辨"
T="$BASE/mp06"; mkrepo "$T" fe api admin
run xin "$T" init --repo-kind multi-api --projects fe,api,admin --shared-api api
assert_rc "布景 init multi-api rc=0" 0
mk_task "$T" a   '{"project": "fe",  "depends_on": ["b"]}'
mk_task "$T" b   '{"project": "api", "depends_on": ["a"]}'
mk_task "$T" bad '{"project": "fe",  "depends_on": ["ap-side"]}'
run xin "$T" contracts check
assert_rc  "MP-06.1 check 检出环+拼错 rc=1" nz
assert_has "MP-06.1 报依赖成环" "依赖成环：a → b → a"
assert_has "MP-06.1 报拼错前置" "指向不存在的 'ap-side'"

T="$BASE/mp06b"; mkrepo "$T" fe api admin
run xin "$T" init --repo-kind multi-api --projects fe,api,admin --shared-api api
assert_rc "布景 init multi-api rc=0" 0
mk_task "$T" api-side '{"project": "api"}'
mk_task "$T" fe-side  '{"project": "fe", "depends_on": ["api-side"]}'
run xin "$T" contracts check
assert_rc  "MP-06.2 合法未闭合前置 rc=0" 0
assert_has "MP-06.2 输出：依赖与契约自洽" "依赖与契约自洽"
run xin "$T" restore
assert_line "MP-06.3 前置未闭合 → fe-side 等 tl:wait-dep" "fe-side  →" "tl:wait-dep"
printf 'done\n' > "$T/.xteam/tasks/api-side/closed.md"
run xin "$T" restore
assert_line "MP-06.3 前置闭合 → fe-side 放行变 tl:assess" "fe-side  →" "tl:assess"

step "MP-07" "契约门禁只拦共享契约项目"
T="$BASE/mp07"; mkrepo "$T" fe api admin
run xin "$T" init --repo-kind multi-api --projects fe,api,admin --shared-api api
assert_rc "布景 init multi-api rc=0" 0
mk_task "$T" api-side '{"project": "api", "contracts": ["POST _orders.md"]}'
mk_task "$T" fe-nope  '{"project": "fe",  "contracts": ["GET _nope.md"]}'
run xin "$T" restore
assert_line "MP-07.1 api 切片缺契约文件 → pm:contracts-missing" \
  "api-side  →" "pm:contracts-missing"
assert_line "MP-07.2 fe 切片（非共享）不拦 → tl:assess" \
  "fe-nope  →" "tl:assess" "contracts-missing"

step "MP-08" "契约文件名与路径安全"
T="$BASE/mp08"; mkrepo "$T" fe api admin
run xin "$T" init --repo-kind multi-api --projects fe,api,admin --shared-api api
assert_rc "布景 init multi-api rc=0" 0
# api-side 先声明引用 POST _orders.md（此时文件未建）：给 MP-10.2 / MP-10.1
# 预备「被引用但缺失」的状态。spec 断言的文件名写死是 POST _orders.md。
mk_task "$T" api-side '{"project": "api", "contracts": ["POST _orders.md"]}'
run xin "$T" contracts new "POST /orders/{id}"
assert_rc "MP-08.1 contracts new rc=0" 0
F="$T/.xteam/contracts/POST _orders_{id}.md"
assert_exists   "MP-08.1 生成 POST _orders_{id}.md" "$F"
assert_file_has "MP-08.1 头：xteam-contract 标记" "$F" "<!-- xteam-contract"
assert_file_has "MP-08.1 头：method: POST"        "$F" "method: POST"
assert_file_has "MP-08.1 头：path: /orders/{id}"  "$F" "path: /orders/{id}"
assert_file_has "MP-08.1 正文含「## 请求」"       "$F" "## 请求"
assert_file_has "MP-08.1 正文含「## 响应」"       "$F" "## 响应"
run xin "$T" contracts new "POST ../../evil"
assert_rc "MP-08.2 越界路径不炸 rc=0" 0
EVIL_IN="$(find "$T/.xteam/contracts" -name '*evil*' | wc -l | tr -d ' ')"
[ "$EVIL_IN" = "1" ] \
  && ok "MP-08.2 生成物文件名消毒后留在 contracts/ 内" \
  || bad "$(printf 'MP-08.2 contracts/ 内 evil 文件数=%s（期望 1）' "$EVIL_IN")" \
       "$(find "$T/.xteam/contracts" -name '*evil*')"
EVIL_OUT="$(find "$T" -name '*evil*' -not -path '*/.xteam/contracts/*')"
[ -z "$EVIL_OUT" ] \
  && ok "MP-08.2 contracts/ 之外（含临时根其余位置）无 evil 文件" \
  || bad "MP-08.2 越界写出：" "$EVIL_OUT"

step "MP-10" "退出码语义 + 引用缺失显示（先于 MP-09：此时 _orders.md 还没建）"
run xin "$T" contracts list
assert_has "MP-10.2 list 标出被引用但缺失的契约" \
  "缺文件 POST _orders.md —— 被切片引用：api-side"
run xin "$T" contracts check
assert_rc "MP-10.1 有问题时 check rc=1" nz
run xin "$T" contracts new "POST /orders"
assert_rc "布景：建出被引用的 POST _orders.md" 0
run xin "$T" contracts check
assert_rc  "MP-10.1 干净时 check rc=0" 0
assert_has "MP-10.1 输出：依赖与契约自洽" "依赖与契约自洽"

step "MP-09" "契约留痕"
run xin "$T" contracts log "POST _orders.md" "total 改为字符串"
assert_rc  "MP-09.1 log rc=0" 0
assert_has "MP-09.1 输出：下游受影响" "下游会受影响：fe、admin"
assert_file_has "MP-09.2 契约文件记入变更" \
  "$T/.xteam/contracts/POST _orders.md" "total 改为字符串"
assert_file_has "MP-09.3 时间线留痕" \
  "$T/.xteam/watch/timeline.log" "POST _orders.md：total 改为字符串"

step "MP-12" "restore / status 对「等前置·拼错·成环」同一口径"
T="$BASE/mp12"; mkrepo "$T" fe api admin
run xin "$T" init --repo-kind multi-api --projects fe,api,admin --shared-api api
assert_rc "布景 init multi-api rc=0" 0
mk_task "$T" a        '{"project": "fe",  "depends_on": ["b"]}'
mk_task "$T" b        '{"project": "api", "depends_on": ["a"]}'
mk_task "$T" bad      '{"project": "fe",  "depends_on": ["ap-side"]}'
mk_task "$T" fe-side  '{"project": "fe",  "depends_on": ["api-side"]}'
mk_task "$T" api-side '{"project": "api"}'
run xin "$T" restore
assert_rc   "MP-12.0 restore rc=0" 0
assert_has  "MP-12.1 restore 报依赖成环（status 同措辞）" \
  "⚠ 依赖成环：a → b → a"
assert_has  "MP-12.2 restore 报拼错前置（status 同措辞）" \
  "⚠ bad 的 depends_on 指向不存在的切片：ap-side"
assert_line "MP-12.3 bad 行不再谎报等前置" "bad  →" "" "tl:wait-dep"
assert_line "MP-12.4 a 行不再谎报等前置"   "a  →" "" "tl:wait-dep"
assert_line "MP-12.4 b 行不再谎报等前置"   "b  →" "" "tl:wait-dep"
assert_line "MP-12.5 fe-side 行仍是等前置" "fe-side  →" "tl:wait-dep"
assert_line "MP-12.6 api-side 行照常推进"  "api-side  →" "tl:assess"

# status 要读到 workspace 才走到依赖段：替身升级为同时回答
# workspace list 与 agent list（spec AC-S 注；agent 名必须是 pm-/tl-/dev-<label>）。
# F-2 起归属判据还查 pane list 的 panes[].cwd —— 这里让 pane 的 cwd 落在仓内，
# 否则正例会被当成外来 workspace 拒掉。
mkdir -p "$BASE/bin12"
cat > "$BASE/bin12/herdr" <<'EOF'
#!/bin/sh
case "$1 $2" in
  "workspace list") printf '%s\n' '{"result": {"workspaces": [{"workspace_id":"wS","label":"'"$LABEL"'","pane_count":3,"tab_count":1,"active_tab_id":"wS:t1","agent_status":"idle","focused":false}]}}' ;;
  "agent list")     printf '%s\n' '{"result": {"agents": [{"pane_id":"wS:p1","name":"pm-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wS","tab_id":"wS:t1"},{"pane_id":"wS:p2","name":"tl-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wS","tab_id":"wS:t1"},{"pane_id":"wS:p3","name":"dev-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wS","tab_id":"wS:t1"}]}}' ;;
  "pane list")      printf '%s\n' '{"result": {"panes": [{"pane_id":"wS:p1","cwd":"'"$ROOT"'"},{"pane_id":"wS:p2","cwd":"'"$ROOT"'"},{"pane_id":"wS:p3","cwd":"'"$ROOT"'"}]}}' ;;
  *) printf '%s\n' '{"result": {}}' ;;
esac
EOF
chmod +x "$BASE/bin12/herdr"
# 同 xin，但换成 MP-12 的完整替身并显式指定 workspace（省掉 cwd 反查）。
xin12() {
  local t="$1"; shift
  (cd "$t" && PATH="$BASE/bin12:$PATH" LABEL=e2ews ROOT="$t" \
    python3 "$REPO/bin/xteam" --project "$t" --workspace e2ews "$@" </dev/null)
}
run xin12 "$T" status
assert_rc   "MP-12.0 status rc=0" 0
assert_line "MP-12.7 等前置行含 fe-side←api-side" \
  "等前置（不是卡住）" "fe-side←api-side"
assert_line "MP-12.7 等前置行不含 bad←" "等前置（不是卡住）" "" "bad←"
assert_line "MP-12.7 等前置行不含 a←"   "等前置（不是卡住）" "" "a←"
assert_line "MP-12.7 等前置行不含 b←"   "等前置（不是卡住）" "" "b←"
assert_has  "MP-12.8 status 仍报依赖成环" "⚠ 依赖成环：a → b → a"
assert_has  "MP-12.8 status 仍报拼错前置" \
  "⚠ bad 的 depends_on 指向不存在的切片：ap-side"
LINE9="$(printf '%s\n' "$LAST_OUT" | grep -F '未闭合切片：' | head -1)"
for n in a b bad fe-side api-side; do
  printf '%s' "$LINE9" | grep -qE "(：|, )${n}(,|$)" \
    && ok "MP-12.9 未闭合清单含 $n" \
    || bad "MP-12.9 未闭合清单缺 $n" "$LINE9"
done

step "MP-13" "显式 --workspace 不属于本项目时 status 拒绝"
# 复用 mp12 fixture（a↔b / bad→ap-side / fe-side→api-side / api-side 仍在）。
T="$BASE/mp12"
# 归属判据走 pane list 的 panes[].cwd（spec §2.2 注），替身必须答三个子命令；
# PCWD 是三个 pane 的 cwd：反例指向别的目录，正例指向 A 自己。
mkdir -p "$BASE/bin13" "$BASE/other-proj"
cat > "$BASE/bin13/herdr" <<'EOF'
#!/bin/sh
case "$1 $2" in
  "workspace list") printf '%s\n' '{"result": {"workspaces": [{"workspace_id":"wX","label":"'"$LABEL"'","pane_count":3,"tab_count":1,"active_tab_id":"wX:t1","agent_status":"idle","focused":false}]}}' ;;
  "agent list")     printf '%s\n' '{"result": {"agents": [{"pane_id":"wX:p1","name":"pm-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$PCWD"'","workspace_id":"wX","tab_id":"wX:t1"},{"pane_id":"wX:p2","name":"tl-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$PCWD"'","workspace_id":"wX","tab_id":"wX:t1"},{"pane_id":"wX:p3","name":"dev-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$PCWD"'","workspace_id":"wX","tab_id":"wX:t1"}]}}' ;;
  "pane list")      printf '%s\n' '{"result": {"panes": [{"pane_id":"wX:p1","cwd":"'"$PCWD"'"},{"pane_id":"wX:p2","cwd":"'"$PCWD"'"},{"pane_id":"wX:p3","cwd":"'"$PCWD"'"}]}}' ;;
  *) printf '%s\n' '{"result": {}}' ;;
esac
EOF
chmod +x "$BASE/bin13/herdr"
# 同 xin12，但 LABEL 与 pane 的 cwd 都可参数化（反例/正例共用一份替身）。
xin13() {
  local t="$1" lbl="$2" pcwd="$3"; shift 3
  (cd "$t" && PATH="$BASE/bin13:$PATH" LABEL="$lbl" PCWD="$pcwd" \
    python3 "$REPO/bin/xteam" --project "$t" --workspace "$lbl" "$@" </dev/null)
}
# macOS 上 /var → /private/var，断言「含 A 路径」要用解析后的真路径。
ROOT13="$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$T")"
run xin13 "$T" other "$BASE/other-proj" status
assert_rc   "MP-13.1 外来 workspace 被拒 rc≠0" nz
assert_has  "MP-13.2 输出含 workspace 名 other" "other"
assert_has  "MP-13.2 输出含项目 A 路径" "$ROOT13"
printf '%s' "$LAST_OUT" | grep -qF 'xteam say' \
  && bad "MP-13.3 输出不该含 xteam say（会叫错 pane）" "$LAST_OUT" \
  || ok  "MP-13.3 输出不含 xteam say"
printf '%s' "$LAST_OUT" | grep -qF '欠 ' \
  && bad "MP-13.4 输出不该含「欠 」义务行" "$LAST_OUT" \
  || ok  "MP-13.4 输出不含「欠 」义务行"
printf '%s' "$LAST_OUT" | grep -qE 'restore|projects' \
  && ok  "MP-13.5 输出给出下一步命令（restore/projects）" \
  || bad "MP-13.5 输出缺下一步命令" "$LAST_OUT"
run xin13 "$T" inside "$T" status
assert_rc   "MP-13.6 属于本项目的 workspace 放行 rc=0" 0
assert_line "MP-13.6 角色表照常：tl 欠 wait-dep" "tl " "wait-dep"
assert_line "MP-13.6 等前置行含 fe-side←api-side" \
  "等前置（不是卡住）" "fe-side←api-side"
assert_line "MP-13.6 等前置行不含 bad←" "等前置（不是卡住）" "" "bad←"
assert_line "MP-13.6 等前置行不含 a←"   "等前置（不是卡住）" "" "a←"
assert_line "MP-13.6 等前置行不含 b←"   "等前置（不是卡住）" "" "b←"
run xin "$T" status
assert_rc  "MP-13.7 不显式给 --workspace 时行为不变 rc≠0" nz
assert_has "MP-13.7 仍走「还没有在跑的 workspace」提示" "还没有在跑的 workspace"

# ---------------------------------------------------------------- MP-14
step "MP-14" "herdr 不在 PATH：六条命令给地基提示（rc≠0，无 traceback）"

# 布景必须是「已 init」的项目：空目录走的是 .xteam-缺失路径，跟本片无关。
T="$BASE/mp14"
mkrepo "$T" fe api admin
run xin "$T" init --repo-kind multi-api --projects fe,api,admin --shared-api api
assert_rc "MP-14.0 init 布景 rc=0" 0

# env -i 把环境清空再显式给回 PATH/HOME：PATH 里既没有真 herdr 也没有
# 上面的替身；HOME 留着是因为 python/xteam 会读它（spec AC-1 布景要求）。
# PYTHONDONTWRITEBYTECODE 也得显式给回（env -i 会清掉脚本级 export）——
# 这组调用解析到 /usr/bin/python3（3.9），不给变量就会把 pyc 落进
# ~/Library/Caches/com.apple.python/，「不写用户目录」的字节级声明破功。
# down / swap 只许打临时仓 —— T 正是 BASE 下的 mktemp 仓，脚本结束随 trap 清掉。
for c in "restore" "status" "watch once" "sync" "down" "swap pm pi"; do
  run env -i PATH=/usr/bin:/bin HOME="$HOME" PYTHONDONTWRITEBYTECODE=1 \
    python3 "$REPO/bin/xteam" --project "$T" $c
  assert_rc "MP-14.1 $c rc≠0" nz
  printf '%s' "$LAST_OUT" | grep -qF 'Traceback (most recent call last)' \
    && bad "MP-14.2 $c 抛了 traceback" "$LAST_OUT" \
    || ok  "MP-14.2 $c 不含 traceback"
  assert_has "MP-14.3 $c 含地基提示" "找不到 herdr"
done

# AC-2 旁路：同环境下不依赖 herdr 的命令照常。
run env -i PATH=/usr/bin:/bin HOME="$HOME" PYTHONDONTWRITEBYTECODE=1 \
  python3 "$REPO/bin/xteam" --project "$T" projects
assert_rc "MP-14.4 projects rc=0" 0
run env -i PATH=/usr/bin:/bin HOME="$HOME" PYTHONDONTWRITEBYTECODE=1 \
  python3 "$REPO/bin/xteam" --project "$T" doctor
assert_rc "MP-14.5 doctor rc=0" 0
run env -i PATH=/usr/bin:/bin HOME="$HOME" PYTHONDONTWRITEBYTECODE=1 \
  python3 "$REPO/bin/xteam" --project "$T" agents
assert_rc "MP-14.6 agents rc=0" 0
run env -i PATH=/usr/bin:/bin HOME="$HOME" PYTHONDONTWRITEBYTECODE=1 \
  python3 "$REPO/bin/xteam" --project "$T" stamp pm x
assert_rc "MP-14.7 stamp pm x rc=0" 0

# ---------------------------------------------------------------- MP-15
step "MP-15" "status：pane 停在权限框上的角色显示 blocked（不是 done）"

T="$BASE/mp15"
mkrepo "$T" fe api admin
run xin "$T" init --repo-kind multi-api --projects fe,api,admin --shared-api api
assert_rc "MP-15.0 init 布景 rc=0" 0
# request.md 落盘 → 阶段 implement → dev 欠 implement（布景三场景共用这笔欠账）。
mk_task "$T" cur '{}'
printf '# request\n' > "$T/.xteam/tasks/cur/request.md"

# 三段尾巴原文逐字抄自 spec §2.1（PM 实抓）。检测器只认完整形态：
# 只留两行选项会返回 False，替身必须带 footer 或命令上下文（spec 已警示）。
cat > "$BASE/tailA.txt" <<'EOF'
 ● Running command
 │ $ cd /Users/gouki/server/wwwroot/xpier/xteam && grep -n "lint_shell\|e2e\|smoke" pack.sh | head -20; echo "=== wc ==="; wc -l pack.sh tests/
 └   lint_shell.py bin/xteam bin/xteam_lib.py PROJECT.md

❭ 1 Yes  (Approve once)
  2 Yes, allow `wc` commands
  3 Yes, always allow `wc` commands in `xteam`
  4 Yes, always allow `wc` commands in all projects
  5 Yes, switch to bypass mode
  6 Edit command
  7 Describe change to command
  8 No
↑↓ select · ↵ confirm · esc cancel
EOF
cat > "$BASE/tailB.txt" <<'EOF'
 ● Read lines 1095-1244 in ./bin/xteam_lib.py
 └ 150 lines
⢠⡀ Thinking · 15m 29s (esc twice to interrupt)
❭ Guide Devin while it works
EOF
cat > "$BASE/tailC.txt" <<'EOF'
────────────────────────────── (bypass permissions on) ─
❭ Press Enter to send queued messages now
────────────────────────────────────────────────────────
SWE-2 Max
EOF

# 替身答四个子命令（spec §2.1）：workspace/agent/pane list + pane read。
# pane read 返回纯文本（read_pane 取 stdout 原样）；dev 的尾巴由 TAIL 环境变量
# 切换，三个布景共用同一替身。pm/tl 回一行中性文本（不含权限标记）。
mkdir -p "$BASE/bin15"
cat > "$BASE/bin15/herdr" <<'EOF'
#!/bin/sh
case "$1 $2" in
  "workspace list") printf '%s\n' '{"result": {"workspaces": [{"workspace_id":"wS","label":"'"$LABEL"'","pane_count":3,"tab_count":1,"active_tab_id":"wS:t1","agent_status":"idle","focused":false}]}}' ;;
  "agent list")     printf '%s\n' '{"result": {"agents": [{"pane_id":"wS:p1","name":"pm-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wS","tab_id":"wS:t1"},{"pane_id":"wS:p2","name":"tl-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wS","tab_id":"wS:t1"},{"pane_id":"wS:p3","name":"dev-'"$LABEL"'","agent_status":"done","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wS","tab_id":"wS:t1"}]}}' ;;
  "pane list")      printf '%s\n' '{"result": {"panes": [{"pane_id":"wS:p1","cwd":"'"$ROOT"'"},{"pane_id":"wS:p2","cwd":"'"$ROOT"'"},{"pane_id":"wS:p3","cwd":"'"$ROOT"'"}]}}' ;;
  "pane read")      case "$3" in
                      "wS:p3") cat "$TAIL" ;;
                      *) printf ' ● Thinking · 1m 0s\n' ;;
                    esac ;;
  *) printf '%s\n' '{"result": {}}' ;;
esac
EOF
chmod +x "$BASE/bin15/herdr"
xin15() {
  local t="$1"; local tail="$2"; shift 2
  (cd "$t" && PATH="$BASE/bin15:$PATH" LABEL=e2ews ROOT="$t" TAIL="$tail" \
    python3 "$REPO/bin/xteam" --project "$t" --workspace e2ews "$@" </dev/null)
}

# 布景 A：dev agent_status=done + 欠 implement + pane 尾巴是权限框。
run xin15 "$T" "$BASE/tailA.txt" status
assert_rc "MP-15.A status rc=0" 0
DEVLINE="$(printf '%s\n' "$LAST_OUT" | grep -E '^dev ' | head -1)"
printf '%s' "$DEVLINE" | grep -qE '(^|[[:space:]])blocked([[:space:]]|$)' \
  && ok  "MP-15.1 dev 行状态列为整词 blocked" \
  || bad "MP-15.1 dev 行未见整词 blocked" "$DEVLINE"
assert_has "MP-15.2 输出含「停在」说明" "停在"
printf '%s' "$LAST_OUT" | grep -qF 'dev (idle 自' \
  && bad "MP-15.3 仍把 dev 报成普通空闲" "$LAST_OUT" \
  || ok  "MP-15.3 不含 dev (idle 自"
printf '%s' "$LAST_OUT" | grep -qF 'dev (done 自' \
  && bad "MP-15.3 仍把 dev 报成已完成" "$LAST_OUT" \
  || ok  "MP-15.3 不含 dev (done 自"

# 布景 B：正常干活的尾巴 → 不许误报。
run xin15 "$T" "$BASE/tailB.txt" status
assert_rc "MP-15.B status rc=0" 0
DEVLINE="$(printf '%s\n' "$LAST_OUT" | grep -E '^dev ' | head -1)"
printf '%s' "$DEVLINE" | grep -q 'blocked' \
  && bad "MP-15.4 正常干活被误报 blocked" "$DEVLINE" \
  || ok  "MP-15.4 dev 行不含 blocked"
printf '%s' "$DEVLINE" | grep -q 'done' \
  && ok  "MP-15.4 dev 行仍是 done（渲染其余内容不变）" \
  || bad "MP-15.4 dev 行丢了 done" "$DEVLINE"

# 布景 C：排队消息框 → 边界断言，本片不许覆盖（F-5 另做）。
run xin15 "$T" "$BASE/tailC.txt" status
assert_rc "MP-15.C status rc=0" 0
DEVLINE="$(printf '%s\n' "$LAST_OUT" | grep -E '^dev ' | head -1)"
printf '%s' "$DEVLINE" | grep -q 'blocked' \
  && bad "MP-15.6 排队消息框被误报 blocked（越界进了 F-5）" "$DEVLINE" \
  || ok  "MP-15.6 dev 行不含 blocked"
printf '%s' "$LAST_OUT" | grep -qF '停在' \
  && bad "MP-15.6 排队消息框不该产生「停在」说明" "$LAST_OUT" \
  || ok  "MP-15.6 输出不含「停在」说明"

# MP-15.5 回归：本文件 112 条既有断言全部继续通过即自动满足（末行 N/N）。

# ---------------------------------------------------------------- MP-16
step "MP-16" "say 的送达判据是目标转 working；排队占位符才补 enter"

T="$BASE/mp16"
mkrepo "$T" fe api admin
run xin "$T" init --repo-kind multi-api --projects fe,api,admin --shared-api api
assert_rc "MP-16.0 init 布景 rc=0" 0

# 有状态替身（spec §2.1 钉的形状）：dev 的 agent_status 由 STATE 文件决定；
# pane read 无标记回 TAIL_BEFORE、有标记回 TAIL_AFTER；pane send-keys 落标记
# 并把 STATE 拨成 working（模拟「补 enter 后消息真的被提交」）；
# 每次调用把「子命令 第3参 第4参」追加进 LOGF，供序列断言核对。
mkdir -p "$BASE/bin16"
cat > "$BASE/bin16/herdr" <<'EOF'
#!/bin/sh
# 一次调用一行日志：子命令+第3参；send-keys 追加第4参（要能看到 enter）。
{ printf '%s %s %s' "$1" "$2" "$3"; [ "$2" = "send-keys" ] && printf ' %s' "$4"; printf '\n'; } >> "$LOGF"
ST="$(cat "$STATE" 2>/dev/null || printf 'done')"
case "$1 $2" in
  "workspace list") printf '%s\n' '{"result": {"workspaces": [{"workspace_id":"wS","label":"'"$LABEL"'","pane_count":3,"tab_count":1,"active_tab_id":"wS:t1","agent_status":"idle","focused":false}]}}' ;;
  "agent list")     printf '%s\n' '{"result": {"agents": [{"pane_id":"wS:p1","name":"pm-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wS","tab_id":"wS:t1"},{"pane_id":"wS:p2","name":"tl-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wS","tab_id":"wS:t1"},{"pane_id":"wS:p3","name":"dev-'"$LABEL"'","agent_status":"'"$ST"'","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wS","tab_id":"wS:t1"}]}}' ;;
  "pane list")      printf '%s\n' '{"result": {"panes": [{"pane_id":"wS:p1","cwd":"'"$ROOT"'"},{"pane_id":"wS:p2","cwd":"'"$ROOT"'"},{"pane_id":"wS:p3","cwd":"'"$ROOT"'"}]}}' ;;
  "agent prompt")   : ;;
  "pane read")      if [ -f "$MARK" ]; then cat "$TAIL_AFTER"; else cat "$TAIL_BEFORE"; fi ;;
  "pane send-keys") touch "$MARK"; printf 'working' > "$STATE"; printf '%s\n' '{"result": {}}' ;;
  *) printf '%s\n' '{"result": {}}' ;;
esac
EOF
chmod +x "$BASE/bin16/herdr"
xin16() {
  local t="$1" tb="$2" ta="$3"; shift 3
  (cd "$t" && PATH="$BASE/bin16:$PATH" LABEL=e2ews ROOT="$t" \
    STATE="$BASE/mp16.state" MARK="$BASE/mp16.mark" LOGF="$BASE/mp16.log" \
    TAIL_BEFORE="$tb" TAIL_AFTER="$ta" \
    python3 "$REPO/bin/xteam" --project "$t" --workspace e2ews "$@" </dev/null)
}
reset16() { printf '%s' "$1" > "$BASE/mp16.state"; rm -f "$BASE/mp16.mark" "$BASE/mp16.log"; }

# 布景 A：dev done + 尾巴 C（有排队占位符）→ 补一次 enter → 复核转 working。
reset16 done
run xin16 "$T" "$BASE/tailC.txt" "$BASE/tailB.txt" say dev "x"
assert_rc  "MP-16.0 布景A say rc=0" 0
LOG="$(cat "$BASE/mp16.log")"
printf '%s' "$LOG" | grep -qF 'pane send-keys wS:p3 enter' \
  && ok  "MP-16.1 日志有 pane send-keys enter（真补了一次）" \
  || bad "MP-16.1 日志缺 pane send-keys" "$LOG"
SK="$(printf '%s\n' "$LOG" | grep -nF 'pane send-keys' | head -1 | cut -d: -f1)"
AL="$(printf '%s\n' "$LOG" | grep -nF 'agent list' | tail -1 | cut -d: -f1)"
{ [ -n "$SK" ] && [ -n "$AL" ] && [ "$AL" -gt "$SK" ]; } \
  && ok  "MP-16.2 send-keys 之后还有一次 agent list（补完复核送达）" \
  || bad "MP-16.2 补 enter 后缺复核" "$LOG"
assert_has "MP-16.3 主行含 已送达（" "已送达（"

# 布景 B：dev 一开始就 working（mid-turn）+ 尾巴有排队占位符 → v3/AC-8：
# 已 working 也要读一次尾巴，占位符在就补一次 enter 再复核。
reset16 working
run xin16 "$T" "$BASE/tailC.txt" "$BASE/tailB.txt" say dev "x"
assert_rc  "MP-16.4 布景B rc=0" 0
assert_has "MP-16.4 主行含 已送达（" "已送达（"
[ "$(grep -c 'pane send-keys' "$BASE/mp16.log")" -eq 1 ] \
  && ok  "MP-16.4 mid-turn+占位符恰补一次 enter" \
  || bad "MP-16.4 mid-turn 门铃排队没补 enter" "$(cat "$BASE/mp16.log")"

# 布景 C：dev done + 尾巴 B（无占位符=输入行可能有内容）→ 不补；
# prompt 已成功 → 「已投递·未确认」rc=0（F-19：不再报未送达假阴性）。
reset16 done
# 需要分开看 stdout/stderr，不能用 run()（它把 2>&1 合并了）。
# LAST_OUT=$(...) 这条赋值语句本身的 $? 就是命令的 rc——必须紧接着取。
LAST_OUT="$(xin16 "$T" "$BASE/tailB.txt" "$BASE/tailB.txt" say dev "x" 2>"$BASE/mp16.err")"; LAST_RC=$?
assert_rc  "MP-16.5 布景C rc=0（已投递非失败）" 0
assert_has "MP-16.5 主行含 已投递" "已投递"
assert_has "MP-16.5 主行含 未确认" "未确认"
printf '%s' "$LAST_OUT" | grep -qF '未送达' \
  && bad "MP-16.5 还在报未送达" "$LAST_OUT" \
  || ok  "MP-16.5 stdout 不含未送达"
grep -qF '未送达' "$BASE/mp16.err" \
  && bad "MP-16.5 stderr 含未送达" "$(cat "$BASE/mp16.err")" \
  || ok  "MP-16.5 stderr 不含未送达"
printf '%s' "$(cat "$BASE/mp16.log")" | grep -qF 'pane send-keys' \
  && bad "MP-16.5 无占位符还抢了回车" "$(cat "$BASE/mp16.log")" \
  || ok  "MP-16.5 无 send-keys（不抢按回车）"

# MP-16.6 回归：MP-12~MP-15 断言继续全过即自动满足（末行 N/N）。

# ---------------------------------------------------------------- MP-17
step "MP-17" "CLI 面快照经 main() 定型：rules.json 落真实子命令集"

T="$BASE/mp17"
mkrepo "$T" fe api admin
run xin "$T" init --repo-kind multi-api --projects fe,api,admin --shared-api api
assert_rc "MP-17.0 init 布景 rc=0" 0

# spec §2.1 手法：exec 整份 bin/xteam 源码、argv=['xteam','--version']，
# __name__=__main__ 触发 sys.exit(main()) → 快照经过 main() 的 add_parser
# 全程；再调 _save_sync_state(root) 落 rules.json 读回。绕过 main() 直接给
# _CLI_SURFACE 赋值不算数——MP-17.1 就是防这个作弊。
run python3 - "$REPO/bin/xteam" "$T" <<'PYEOF'
import json, sys
from pathlib import Path
src, root = sys.argv[1], sys.argv[2]
g = {"__name__": "__main__", "__file__": src}
sys.argv = ["xteam", "--version"]
try:
    exec(compile(open(src, encoding="utf-8").read(), src, "exec"), g)
except SystemExit:
    pass
g["_save_sync_state"](Path(root))
print("SURFACE=" + json.dumps(g["_cli_surface"]()))
d = json.load(open(Path(root) / ".xteam" / "rules.json"))
print("RULESCLI=" + json.dumps(d.get("cli")))
PYEOF
assert_rc "MP-17.0 exec+save_sync_state rc=0" 0
SURF="$(printf '%s\n' "$LAST_OUT" | grep -F 'SURFACE=' | cut -d= -f2-)"
RCLI="$(printf '%s\n' "$LAST_OUT" | grep -F 'RULESCLI=' | cut -d= -f2-)"
{ [ -n "$SURF" ] && [ "$SURF" != "[]" ]; } \
  && ok  "MP-17.1 经 main() 后 _cli_surface() 非空" \
  || bad "MP-17.1 _cli_surface() 仍是空" "$SURF"
for c in say status watch contracts up; do
  printf '%s' "$SURF" | grep -qF "\"$c\"" \
    && ok  "MP-17.1 CLI 面含 $c" \
    || bad "MP-17.1 CLI 面缺 $c" "$SURF"
done
{ [ "$RCLI" = "$SURF" ] && [ -n "$RCLI" ] && [ "$RCLI" != "[]" ]; } \
  && ok  "MP-17.2 rules.json 的 cli 与 surface 相同且非空" \
  || bad "MP-17.2 rules.json cli 不符" "$RCLI"

# MP-17.3：rules.json 的 cli 去掉 contracts → status 应报新增告知。
# 替身复用 MP-12 的 bin12（答 workspace/agent/pane list；pane read 落空 JSON，
# 检测器返回 False —— 不会触发 F-4 的 blocked 判定）。
python3 -c '
import json, sys
p = sys.argv[1]
d = json.load(open(p, encoding="utf-8"))
d["cli"] = [c for c in d["cli"] if c != "contracts"]
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
' "$T/.xteam/rules.json"
run xin12 "$T" status
assert_rc  "MP-17.3 status rc=0" 0
assert_has "MP-17.3 输出含新增告知" "新增了这些子命令"
assert_has "MP-17.3 告知点名 contracts" "contracts"

# MP-17.4：cli 写回当前全集 → 同一路径不许常驻误报。
python3 -c '
import json, sys
p = sys.argv[1]
d = json.load(open(p, encoding="utf-8"))
d["cli"] = json.loads(sys.argv[2])
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
' "$T/.xteam/rules.json" "$SURF"
run xin12 "$T" status
assert_rc  "MP-17.4 status rc=0" 0
printf '%s' "$LAST_OUT" | grep -qF '新增了这些子命令' \
  && bad "MP-17.4 cli 一致仍报新增（常驻误报）" "$LAST_OUT" \
  || ok  "MP-17.4 cli 一致不误报"

# MP-17.5 回归：MP-12~MP-16 断言继续全过即自动满足（末行 N/N）。

# ---------------------------------------------------------------- MP-18
step "MP-18" "巡检自比指纹：自身代码变了报 SELF-STALE（带冷却，不自动重启）"

T="$BASE/mp18"
mkrepo "$T" fe api admin
run xin "$T" init --repo-kind multi-api --projects fe,api,admin --shared-api api
assert_rc "MP-18.0 init 布景 rc=0" 0

# _watch_cycle 的 workspace 名取自 session.json（没有则回退目录名），不看
# --workspace —— 写一条绑定记录模拟「up 过的 workspace」，替身按 e2ews 应答。
printf '{"workspace":"e2ews","panes":["pm","tl","dev"]}\n' \
  > "$T/.xteam/session.json"
# 替身复用 MP-12 的 bin12（答 workspace/agent/pane list）——_watch_cycle 里
# ws 判空在 nag 段之前，没有替身 workspace 走不到比对。
EVLOG="$T/.xteam/watch/events.log"

# MP-18.1：无 self.json（旧版本启动的巡检就没有这条记录）→ 无基准不误报。
rm -f "$T/.xteam/watch/self.json"
run xin12 "$T" watch once
assert_rc "MP-18.1 once rc=0" 0
{ [ -f "$EVLOG" ] && grep -qF 'SELF-STALE' "$EVLOG"; } \
  && bad "MP-18.1 无基准却报了 SELF-STALE" "$(tail -5 "$EVLOG")" \
  || ok  "MP-18.1 无 self.json 不报 SELF-STALE"

# MP-18.2：写一个不同的指纹 → events.log 新增 SELF-STALE 且提到「重启」。
mkdir -p "$T/.xteam/watch"
printf '{"bin_xteam_sha256": "%064d"}\n' 0 > "$T/.xteam/watch/self.json"
run xin12 "$T" watch once
assert_rc "MP-18.2 once rc=0" 0
STALE_LINE="$(grep -F 'SELF-STALE' "$EVLOG" | tail -1)"
[ -n "$STALE_LINE" ] \
  && ok  "MP-18.2 events.log 有 SELF-STALE 行" \
  || bad "MP-18.2 指纹不一致却没报 SELF-STALE" "$(tail -5 "$EVLOG")"
printf '%s' "$STALE_LINE" | grep -qF '重启' \
  && ok  "MP-18.2 行内提到重启" \
  || bad "MP-18.2 SELF-STALE 行没提重启" "$STALE_LINE"

# MP-18.3：写当前指纹（bin/xteam 的 sha256，realpath 解析符号链接）→
# 新一轮不再新增 SELF-STALE（不许常驻误报）。
# **先撤冷却武装**：MP-18.2 刚为 nag:self-stale 关闸 30min，不删 idle.state
# 的话本轮无论指纹一致与否都 0 新增——断言恒真、测了个寂寞（F-10 的
# 修复前失败证据）。与 MP-19 的 zero_recap_ts 同一个「主动绕冷却」套路。
CUR="$(python3 -c 'import hashlib,os,sys
print(hashlib.sha256(open(os.path.realpath(sys.argv[1]),"rb").read()).hexdigest())' \
  "$REPO/bin/xteam")"
printf '{"bin_xteam_sha256": "%s"}\n' "$CUR" > "$T/.xteam/watch/self.json"
rm -f "$T/.xteam/watch/idle.state"
N_BEFORE=""; N_AFTER=""
{ [ -f "$EVLOG" ] && N_BEFORE="$(grep -cF 'SELF-STALE' "$EVLOG" || true)"; }
run xin12 "$T" watch once
assert_rc "MP-18.3 once rc=0" 0
{ [ -f "$EVLOG" ] && N_AFTER="$(grep -cF 'SELF-STALE' "$EVLOG" || true)"; }
{ [ -n "$N_BEFORE" ] && [ -n "$N_AFTER" ] && [ "$N_AFTER" = "$N_BEFORE" ]; } \
  && ok  "MP-18.3 指纹一致不新增 SELF-STALE" \
  || bad "MP-18.3 指纹一致仍报（常驻误报）" \
       "EVLOG存在=$([ -f "$EVLOG" ] && echo y || echo n) N_BEFORE=$N_BEFORE N_AFTER=$N_AFTER"

# MP-18.4 回归：MP-12~MP-17 断言继续全过即自动满足（末行 N/N）。

# ---------------------------------------------------------------- MP-19
step "MP-19" "recap 问询收敛：每片各问一次，问完静默（不再 s1→s2 震荡）"

T="$BASE/mp19"
mkrepo "$T" fe api admin
run xin "$T" init --repo-kind multi-api --projects fe,api,admin --shared-api api
assert_rc "MP-19.0 init 布景 rc=0" 0

# 三个已闭合切片：spec.md/spec.json → pm 参与、request.md → tl 参与、
# closed.md = 闭合标记。**不写 session.json**：_read_label 回落目录名
# （spec §2.1 实测走的就是这条路），替身 label 必须等于它 → xin13 传 mp19。
for s in s1 s2 s3; do
  d="$T/.xteam/tasks/$s"; mkdir -p "$d"
  printf '# spec\n'     > "$d/spec.md"
  printf '{"round":1}\n' > "$d/spec.json"
  printf '# request\n'  > "$d/request.md"
  printf 'closed\n'     > "$d/closed.md"
done
EVLOG="$T/.xteam/watch/events.log"
IDLE="$T/.xteam/watch/idle.state"

# 每轮跑前把 pm.recap_asked_at 归零：绕 30min 冷却，否则测不到多轮
# （spec §2.1 的手法；冷却入口位置不许动，是 [57] 钉的）。
zero_recap_ts() {
  mkdir -p "$(dirname "$1")"
  python3 -c '
import json, sys
p = sys.argv[1]
try:
    d = json.load(open(p, encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    d = {}
d.setdefault("pm", {})["recap_asked_at"] = 0
json.dump(d, open(p, "w", encoding="utf-8"))
' "$1"
}
npc() { grep -cF 'RECAP-ASK pm ' "$EVLOG" 2>/dev/null || true; }

zero_recap_ts "$IDLE"
run xin13 "$T" mp19 "$T" watch once
assert_rc "MP-19.1 第1轮 rc=0" 0
grep -qF 'SAMPLE' "$EVLOG" \
  && ok  "MP-19.0 events.log 有 SAMPLE（布景没空转）" \
  || bad "MP-19.0 无 SAMPLE——布景空转" "$(tail -5 "$EVLOG" 2>/dev/null)"
[ "$(npc)" = "1" ] \
  && ok  "MP-19.1 第1轮恰新增 1 条 RECAP-ASK pm" \
  || bad "MP-19.1 第1轮计数不对" "$(npc)"

for round in 2 3; do
  zero_recap_ts "$IDLE"
  run xin13 "$T" mp19 "$T" watch once
  assert_rc "MP-19.2 第 ${round} 轮 rc=0" 0
done
[ "$(npc)" = "3" ] \
  && ok  "MP-19.2 三轮累计恰 3 条（每轮各问一片）" \
  || bad "MP-19.2 三轮累计不对" "$(npc)"
for s in s1 s2 s3; do
  grep -F 'RECAP-ASK pm ' "$EVLOG" | grep -qF "切片 $s 闭合" \
    && ok  "MP-19.2 reasons 覆盖 $s" \
    || bad "MP-19.2 没问过 $s" "$(grep -F 'RECAP-ASK' "$EVLOG")"
done

zero_recap_ts "$IDLE"
run xin13 "$T" mp19 "$T" watch once
assert_rc "MP-19.3 第4轮 rc=0" 0
[ "$(npc)" = "3" ] \
  && ok  "MP-19.3 第4轮零新增（收敛）" \
  || bad "MP-19.3 第4轮还在问（不收敛）" "$(grep -F 'RECAP-ASK' "$EVLOG")"

# MP-19.4 回归：MP-12~MP-18 断言继续全过即自动满足（末行 N/N）。

# ---------------------------------------------------------------- MP-20
step "MP-20" "队列项已开工不催 PM；未开工（无 spec.md / 目录缺失）仍催（双向）"

T="$BASE/mp20"
mkrepo "$T" fe api admin
run xin "$T" init --repo-kind multi-api --projects fe,api,admin --shared-api api
assert_rc "MP-20.0 init 布景 rc=0" 0

# 布景对齐 spec §1.1 采样：1 个已闭合切片 f-done + 队列 1 项 q1（state=doing）。
# q1 的 spec.md 在 = 已开工（PM 的第一件产物已出）；request.md 在 = dev 正在实现
# → dev 欠 implement，替身把它报成 working（链条在跑），idle_hits 才为空。
d="$T/.xteam/tasks/f-done"; mkdir -p "$d"
printf '# spec\n'   > "$d/spec.md"
printf 'closed\n'   > "$d/closed.md"
d="$T/.xteam/tasks/q1"; mkdir -p "$d"
printf '# spec\n'    > "$d/spec.md"
printf '# request\n' > "$d/request.md"
printf '| 1 | q1 | doing | |\n' >> "$T/.xteam/QUEUE.md"

# 替身：pm/tl idle，dev working（spec §1.1 的同型：下游在跑、PM 闲着）。
mkdir -p "$BASE/bin20"
cat > "$BASE/bin20/herdr" <<'EOF'
#!/bin/sh
case "$1 $2" in
  "workspace list") printf '%s\n' '{"result": {"workspaces": [{"workspace_id":"wQ","label":"'"$LABEL"'","pane_count":3,"tab_count":1,"active_tab_id":"wQ:t1","agent_status":"idle","focused":false}]}}' ;;
  "agent list")     printf '%s\n' '{"result": {"agents": [{"pane_id":"wQ:p1","name":"pm-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wQ","tab_id":"wQ:t1"},{"pane_id":"wQ:p2","name":"tl-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wQ","tab_id":"wQ:t1"},{"pane_id":"wQ:p3","name":"dev-'"$LABEL"'","agent_status":"working","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wQ","tab_id":"wQ:t1"}]}}' ;;
  "pane list")      printf '%s\n' '{"result": {"panes": [{"pane_id":"wQ:p1","cwd":"'"$ROOT"'"},{"pane_id":"wQ:p2","cwd":"'"$ROOT"'"},{"pane_id":"wQ:p3","cwd":"'"$ROOT"'"}]}}' ;;
  *) printf '%s\n' '{"result": {}}' ;;
esac
EOF
chmod +x "$BASE/bin20/herdr"
xin20() {
  local t="$1"; shift
  (cd "$t" && PATH="$BASE/bin20:$PATH" LABEL=mp20 ROOT="$t" \
    python3 "$REPO/bin/xteam" --project "$t" --workspace mp20 "$@" </dev/null)
}

# MP-20.1：spec.md 在（已开工）→ PM 不欠；无 idle 欠账 → 打「✓ 无欠账角色」。
run xin20 "$T" status
assert_rc   "MP-20.1 status rc=0" 0
assert_line "MP-20.1 pm 行 owes=none（已开工不催）" "pm " "none"
assert_has  "MP-20.1 打印 ✓ 无欠账角色" "✓ 无欠账角色"

# MP-20.2：删 spec.md → q1 变未开工 → PM 欠 next-slice（或 start-next）。
rm "$T/.xteam/tasks/q1/spec.md"
run xin20 "$T" status
assert_rc "MP-20.2 status rc=0" 0
printf '%s' "$LAST_OUT" | grep -qE 'pm.*(next-slice|start-next)' \
  && ok  "MP-20.2 删 spec.md 后 PM 欠 next-slice/start-next" \
  || bad "MP-20.2 删 spec.md 后 PM 仍不欠（未开工没催）" "$LAST_OUT"

# MP-20.3：删掉整个 tasks/q1（未开工的另一种形态）→ 同样催。
rm -rf "$T/.xteam/tasks/q1"
run xin20 "$T" status
assert_rc "MP-20.3 status rc=0" 0
printf '%s' "$LAST_OUT" | grep -qE 'pm.*(next-slice|start-next)' \
  && ok  "MP-20.3 目录缺失 PM 欠 next-slice/start-next" \
  || bad "MP-20.3 目录缺失 PM 仍不欠" "$LAST_OUT"

# MP-20.4 防回归：队列清空 + 有闭合切片 + 无 REPORT → PM 欠 report；写完清空。
# report 是 overall 层义务：status/巡检读的是 obligations()（只聚合 per-task），
# 结构上不含它；机读面是 watch once 每轮写的 index.json 的 owes（=overall_debts）。
printf '# 队列\n\n| 序 | 切片 | 状态 | 备注 |\n|---|---|---|---|\n' \
  > "$T/.xteam/QUEUE.md"
run xin20 "$T" watch once
assert_rc "MP-20.4 watch once rc=0" 0
run python3 -c '
import json, sys
pm = (json.load(open(sys.argv[1]))["owes"].get("pm") or [])
sys.exit(0 if any("report" in o for o in pm)
           and not any("next-slice" in o for o in pm) else 1)
' "$T/.xteam/index.json"
assert_rc "MP-20.4 队列空+闭合 → PM 欠 report（且不含 next-slice）" 0
printf '# 结项\n' > "$T/.xteam/REPORT.md"
run xin20 "$T" watch once
assert_rc "MP-20.4b watch once rc=0" 0
run python3 -c '
import json, sys
sys.exit(0 if not json.load(open(sys.argv[1]))["owes"].get("pm") else 1)
' "$T/.xteam/index.json"
assert_rc "MP-20.4b 写完 REPORT 后 PM 清空" 0

# MP-20.5 回归：MP-12~MP-19 断言继续全过即自动满足（末行 N/N）。

# ---------------------------------------------------------------- MP-21
step "MP-21" "report 义务进 status 角色表：看得见 + 不误报 + 不挤掉 next-slice"

T="$BASE/mp21"
mkrepo "$T" fe api admin
run xin "$T" init --repo-kind multi-api --projects fe,api,admin --shared-api api
assert_rc "MP-21.0 init 布景 rc=0" 0

# 布景：1 个已闭合切片 + 队列空（init 默认无 pending 行）+ 无 REPORT.md。
# (项目):report 是 overall_debts() 层义务——修复前 status 角色表显示 pm=none。
d="$T/.xteam/tasks/f-done"; mkdir -p "$d"
printf '# spec\n' > "$d/spec.md"
printf 'closed\n'  > "$d/closed.md"

# 替身同 MP-20 的形状（pm/tl idle、dev working），label 参数化。
mkdir -p "$BASE/bin21"
cat > "$BASE/bin21/herdr" <<'EOF'
#!/bin/sh
case "$1 $2" in
  "workspace list") printf '%s\n' '{"result": {"workspaces": [{"workspace_id":"wR","label":"'"$LABEL"'","pane_count":3,"tab_count":1,"active_tab_id":"wR:t1","agent_status":"idle","focused":false}]}}' ;;
  "agent list")     printf '%s\n' '{"result": {"agents": [{"pane_id":"wR:p1","name":"pm-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wR","tab_id":"wR:t1"},{"pane_id":"wR:p2","name":"tl-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wR","tab_id":"wR:t1"},{"pane_id":"wR:p3","name":"dev-'"$LABEL"'","agent_status":"working","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wR","tab_id":"wR:t1"}]}}' ;;
  "pane list")      printf '%s\n' '{"result": {"panes": [{"pane_id":"wR:p1","cwd":"'"$ROOT"'"},{"pane_id":"wR:p2","cwd":"'"$ROOT"'"},{"pane_id":"wR:p3","cwd":"'"$ROOT"'"}]}}' ;;
  *) printf '%s\n' '{"result": {}}' ;;
esac
EOF
chmod +x "$BASE/bin21/herdr"
xin21() {
  local t="$1"; shift
  (cd "$t" && PATH="$BASE/bin21:$PATH" LABEL=mp21 ROOT="$t" \
    python3 "$REPO/bin/xteam" --project "$t" --workspace mp21 "$@" </dev/null)
}

# MP-21.1：pm 行含 report，且「该动没动」段有 pm 提示行。
run xin21 "$T" status
assert_rc   "MP-21.1 status rc=0" 0
assert_line "MP-21.1 pm 行含 report" "pm " "report"
assert_has  "MP-21.1 该动没动段有 pm 欠 report" "欠 report"

# MP-21.2：写 REPORT.md → pm 行不再含 report（义务消失，不误报）。
printf '# 结项\n' > "$T/.xteam/REPORT.md"
run xin21 "$T" status
assert_rc   "MP-21.2 status rc=0" 0
assert_line "MP-21.2 pm 行不含 report（不误报）" "pm " "none" "report"
assert_has  "MP-21.2 恢复 ✓ 无欠账角色" "✓ 无欠账角色"

# MP-21.3：队列有未开工项（tasks/q9 无 spec.md）→ pm 行是 next-slice 不是 report。
rm -f "$T/.xteam/REPORT.md"
printf '| 1 | q9 | todo | |\n' >> "$T/.xteam/QUEUE.md"
run xin21 "$T" status
assert_rc   "MP-21.3 status rc=0" 0
assert_line "MP-21.3 pm 行是 next-slice" "pm " "next-slice"
assert_line "MP-21.3 pm 行不含 report（不挤掉）" "pm " "" "report"

# MP-21.4 回归：MP-12~MP-20 断言继续全过即自动满足（末行 N/N）。

# ---------------------------------------------------------------- MP-22
step "MP-22" "say 复核窗口：无占位符两轮复核，mid-turn 排队不误报未送达"

T="$BASE/mp22"
mkrepo "$T" fe api admin
run xin "$T" init --repo-kind multi-api --projects fe,api,admin --shared-api api
assert_rc "MP-22.0 init 布景 rc=0" 0

# 有状态替身（spec §2.1 钉的形状）：dev 的 agent_status 由 agent list 调用
# 次数翻转——CNT>=FLIP_AT 才 working（第 2 轮复核才转 = mid-turn 排队提交）；
# pane send-keys 落 MARK 也让 dev 转 working（补 enter 后真提交）；
# PROMPT_FAIL=1 时 agent prompt 退出码非 0。
mkdir -p "$BASE/bin22"
cat > "$BASE/bin22/herdr" <<'EOF'
#!/bin/sh
{ printf '%s %s %s\n' "$1" "$2" "$3"; } >> "$LOGF"
N=$(cat "$CNT" 2>/dev/null || echo 0)
if [ "$1 $2" = "agent list" ]; then N=$((N+1)); echo "$N" > "$CNT"; fi
if [ -f "$MARK" ] || [ "$N" -ge "${FLIP_AT:-999}" ]; then ST=working; else ST=done; fi
case "$1 $2" in
  "workspace list") printf '%s\n' '{"result": {"workspaces": [{"workspace_id":"wQ","label":"'"$LABEL"'","pane_count":3,"tab_count":1,"active_tab_id":"wQ:t1","agent_status":"idle","focused":false}]}}' ;;
  "agent list")     printf '%s\n' '{"result": {"agents": [{"pane_id":"wQ:p1","name":"pm-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wQ","tab_id":"wQ:t1"},{"pane_id":"wQ:p2","name":"tl-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wQ","tab_id":"wQ:t1"},{"pane_id":"wQ:p3","name":"dev-'"$LABEL"'","agent_status":"'"$ST"'","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wQ","tab_id":"wQ:t1"}]}}' ;;
  "pane list")      printf '%s\n' '{"result": {"panes": [{"pane_id":"wQ:p1","cwd":"'"$ROOT"'"},{"pane_id":"wQ:p2","cwd":"'"$ROOT"'"},{"pane_id":"wQ:p3","cwd":"'"$ROOT"'"}]}}' ;;
  "agent prompt")   if [ -n "$PROMPT_FAIL" ]; then printf 'boom\n' >&2; exit 1; fi; : ;;
  "pane read")      cat "$TAIL" ;;
  "pane send-keys") touch "$MARK"; printf '%s\n' '{"result": {}}' ;;
  *) printf '%s\n' '{"result": {}}' ;;
esac
EOF
chmod +x "$BASE/bin22/herdr"
xin22() {
  local t="$1" tail="$2"; shift 2
  (cd "$t" && PATH="$BASE/bin22:$PATH" LABEL=mp22 ROOT="$t" \
    CNT="$BASE/mp22.cnt" MARK="$BASE/mp22.mark" LOGF="$BASE/mp22.log" \
    TAIL="$tail" FLIP_AT="${FLIP_AT:-999}" PROMPT_FAIL="${PROMPT_FAIL:-}" \
    python3 "$REPO/bin/xteam" --project "$t" --workspace mp22 "$@" </dev/null)
}
reset22() { rm -f "$BASE/mp22.cnt" "$BASE/mp22.mark" "$BASE/mp22.log"; }

# MP-22.1：mid-turn——prompt 成功、尾巴无占位符、第 2 轮复核才转 working。
# 调用序列钉死：role_map(#1)/doorbell prev(#2)/复核轮1(#3) 都 done，
# 轮2(#4) 才 working → 不得报未送达。
reset22
FLIP_AT=4 run xin22 "$T" "$BASE/tailB.txt" say dev "x"
assert_rc "MP-22.1 mid-turn say rc=0（不报假失败）" 0
printf '%s' "$LAST_OUT" | grep -qF '未送达' \
  && bad "MP-22.1 第2轮转working仍报未送达" "$LAST_OUT" \
  || ok  "MP-22.1 输出不含未送达"
assert_has "MP-22.1 主行含 已送达（" "已送达（"
[ "$(grep -c 'agent list' "$BASE/mp22.log")" -ge 4 ] \
  && ok  "MP-22.1 第 2 轮复核真的查了 agent list" \
  || bad "MP-22.1 缺第 2 轮复核" "$(cat "$BASE/mp22.log")"

# MP-22.2：真失败——agent prompt 直接失败。
# 注：spec 表写「含 未送达」，但 prompt-failed 是 F-5 §2.2 冻结的第三种渲染
# （主行 prompt-failed:… + stderr 投递失败），禁区不许改其逐字格式——
# 按冻结格式断言，实质（真失败必须报 + rc≠0）保留。
reset22
LAST_OUT="$(PROMPT_FAIL=1 xin22 "$T" "$BASE/tailB.txt" say dev "x" 2>"$BASE/mp22.err")"; LAST_RC=$?
assert_rc "MP-22.2 prompt 失败 rc≠0" nz
printf '%s' "$LAST_OUT" | grep -qF 'prompt-failed' \
  && ok  "MP-22.2 主行含 prompt-failed（冻结格式）" \
  || bad "MP-22.2 主行缺 prompt-failed" "$LAST_OUT"
grep -qF '投递失败' "$BASE/mp22.err" \
  && ok  "MP-22.2 stderr 含投递失败" \
  || bad "MP-22.2 stderr 缺投递失败" "$(cat "$BASE/mp22.err")"

# MP-22.3：prompt 成功但两轮都不动（恒 done）、无占位符 → F-19 新态：
# 「已投递·未确认」rc=0（消息进了 pane，没看到它开始跑≠失败）。
reset22
LAST_OUT="$(FLIP_AT=999 xin22 "$T" "$BASE/tailB.txt" say dev "x" 2>"$BASE/mp22.err")"; LAST_RC=$?
assert_rc "MP-22.3 两轮不动 rc=0" 0
assert_has "MP-22.3 主行含 已投递·未确认" "已投递·未确认"
printf '%s' "$LAST_OUT" | grep -qF '未送达' \
  && bad "MP-22.3 还在报未送达" "$LAST_OUT" \
  || ok  "MP-22.3 stdout 不含未送达"
grep -qF '未送达' "$BASE/mp22.err" \
  && bad "MP-22.3 stderr 含未送达" "$(cat "$BASE/mp22.err")" \
  || ok  "MP-22.3 stderr 不含未送达"
printf '%s' "$(cat "$BASE/mp22.log")" | grep -qF 'pane send-keys' \
  && bad "MP-22.3 无占位符还抢了回车" "$(cat "$BASE/mp22.log")" \
  || ok  "MP-22.3 无 send-keys（不抢按回车）"

# MP-22.4：F-5 不回退——有占位符仍恰补一次 enter 并报已送达。
reset22
FLIP_AT=999 run xin22 "$T" "$BASE/tailC.txt" say dev "x"
assert_rc "MP-22.4 占位符 say rc=0" 0
assert_has "MP-22.4 主行含 已送达（" "已送达（"
[ "$(grep -c 'pane send-keys' "$BASE/mp22.log")" -eq 1 ] \
  && ok  "MP-22.4 恰补一次 enter" \
  || bad "MP-22.4 send-keys 次数异常" "$(cat "$BASE/mp22.log")"

# MP-22.5 回归：MP-12~MP-21 断言继续全过即自动满足（末行 N/N）。

# ---------------------------------------------------------------- MP-24
step "MP-24" "已交付未判窗口冻结其它 implement；verdict 对准后恢复"

T="$BASE/mp24"
mkrepo "$T" fe api admin
run xin "$T" init --repo-kind multi-api --projects fe,api,admin --shared-api api
assert_rc "MP-24.0 init 布景 rc=0" 0

# 布景（spec §2.1）：a = 已交付未判（delivered+ready、无 verdict）→ 待 gate；
# b = 只有 request.md → 修复前 dev 欠它的 implement。
d="$T/.xteam/tasks/a"; mkdir -p "$d"
printf '# spec\n'       > "$d/spec.md"
printf '# request\n'    > "$d/request.md"
printf '{"round":1}\n'    > "$d/delivered.json"
printf '{"delivery":1}\n' > "$d/ready.json"
d="$T/.xteam/tasks/b"; mkdir -p "$d"
printf '# spec\n'    > "$d/spec.md"
printf '# request\n' > "$d/request.md"

# 替身：三 pane 恒 idle（同 MP-21 的静态形状），label 参数化。
mkdir -p "$BASE/bin24"
cat > "$BASE/bin24/herdr" <<'EOF'
#!/bin/sh
case "$1 $2" in
  "workspace list") printf '%s\n' '{"result": {"workspaces": [{"workspace_id":"wG","label":"'"$LABEL"'","pane_count":3,"tab_count":1,"active_tab_id":"wG:t1","agent_status":"idle","focused":false}]}}' ;;
  "agent list")     printf '%s\n' '{"result": {"agents": [{"pane_id":"wG:p1","name":"pm-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wG","tab_id":"wG:t1"},{"pane_id":"wG:p2","name":"tl-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wG","tab_id":"wG:t1"},{"pane_id":"wG:p3","name":"dev-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wG","tab_id":"wG:t1"}]}}' ;;
  "pane list")      printf '%s\n' '{"result": {"panes": [{"pane_id":"wG:p1","cwd":"'"$ROOT"'"},{"pane_id":"wG:p2","cwd":"'"$ROOT"'"},{"pane_id":"wG:p3","cwd":"'"$ROOT"'"}]}}' ;;
  *) printf '%s\n' '{"result": {}}' ;;
esac
EOF
chmod +x "$BASE/bin24/herdr"
xin24() {
  local t="$1"; shift
  (cd "$t" && PATH="$BASE/bin24:$PATH" LABEL=mp24 ROOT="$t" \
    python3 "$REPO/bin/xteam" --project "$t" --workspace mp24 "$@" </dev/null)
}

# MP-24.1：窗口内 dev 不欠 B 的 implement + 提示行点名待判片；
#         pm 的 gate 义务不动（它是关窗口的人）。
run xin24 "$T" status
assert_rc   "MP-24.1 status rc=0" 0
assert_line "MP-24.1 dev 行 owes=none（冻结）" "dev " "none"
assert_has  "MP-24.1 提示行含「待 gate」" "待 gate"
assert_has  "MP-24.1 提示行点名 a" "待 gate：a"
assert_line "MP-24.1 pm 行仍欠 gate a（窗口要有人关）" "pm " "gate"

# MP-24.2：verdict 对准这次交付（+ consumed 静默该片）→ 窗口关，义务恢复。
printf '{"round":1,"delivery":1,"verdict":"PASS"}\n' > "$T/.xteam/tasks/a/verdict.json"
printf '{"round":1}\n' > "$T/.xteam/tasks/a/consumed.json"
run xin24 "$T" status
assert_rc   "MP-24.2 status rc=0" 0
assert_line "MP-24.2 dev 行恢复 implement b" "dev " "implement"
assert_line "MP-24.2 dev 行指向 b" "dev " "b"
printf '%s' "$LAST_OUT" | grep -qF '待 gate' \
  && bad "MP-24.2 窗口关了还提示待 gate" "$LAST_OUT" \
  || ok  "MP-24.2 无待 gate 提示（不误挂）"

# MP-24.3：没有待判片（只有 b 的 request.md）→ dev 欠 implement b（现状不变）。
rm -rf "$T/.xteam/tasks/a"
run xin24 "$T" status
assert_rc   "MP-24.3 status rc=0" 0
assert_line "MP-24.3 dev 行是 implement b（防回归）" "dev " "implement"
printf '%s' "$LAST_OUT" | grep -qF '待 gate' \
  && bad "MP-24.3 无待判片还提示待 gate" "$LAST_OUT" \
  || ok  "MP-24.3 无待 gate 提示"

# MP-24.4 回归：MP-12~MP-22 断言继续全过即自动满足（末行 N/N）。

# ---------------------------------------------------------------- MP-25
step "MP-25" "doctor 报 PATH 上的 xteam 全命中；遮蔽警告+--version 提示（F-18）"

T="$BASE/mp25"
mkrepo "$T" fe api admin
run xin "$T" init --repo-kind multi-api --projects fe,api,admin --shared-api api
assert_rc "MP-25.0 init 布景 rc=0" 0

# 布景（spec §3 替身必须是 install.sh 生成格式，否则判定被带偏）：
#   bin25a/xteam = 指向「另一份安装」的启动器（exec python3 <other>/lib/xteam）
#   bin25b/xteam = 指向本仓库 bin/xteam 的软链（同一份安装的另一个入口）
mkdir -p "$BASE/mp25_other/lib"
printf '# impl of OTHER install\n' > "$BASE/mp25_other/lib/xteam"
mkdir -p "$BASE/bin25a" "$BASE/bin25b" "$BASE/bin25empty"
cat > "$BASE/bin25a/xteam" <<EOF
#!/usr/bin/env bash
export PM_TEAM_HOME="\${PM_TEAM_HOME:-$BASE/mp25_other/share/xteam}"
exec python3 "$BASE/mp25_other/lib/xteam" "\$@"
EOF
chmod +x "$BASE/bin25a/xteam"
ln -s "$REPO/bin/xteam" "$BASE/bin25b/xteam"

# MP-25.1 遮蔽：PATH 第 1 份是另一份安装 → 两路径+警告+修复命令，rc 仍 0
run env PATH="$BASE/bin25a:$BASE/bin25b:/usr/bin:/bin" \
  python3 "$REPO/bin/xteam" --project "$T" doctor
assert_rc  "MP-25.1 doctor rc=0（提示不是致命项）" 0
assert_has "MP-25.1 有「PATH 上的 xteam」节" "PATH 上的 xteam"
assert_line "MP-25.1 第 1 份=遮蔽源（顺序=优先级）" "1. " "bin25a/xteam"
assert_has "MP-25.1 列出第 2 份（按优先级顺序）" "bin25b/xteam"
assert_has "MP-25.1 报本次运行份" "$REPO/bin/xteam"
assert_has "MP-25.1 警告含遮蔽路径" "遮蔽"
assert_has "MP-25.1 警告含 mv 修复命令" 'mv "'

run env PATH="$BASE/bin25a:$BASE/bin25b:/usr/bin:/bin" \
  python3 "$REPO/bin/xteam" --version
assert_rc  "MP-25.5 --version rc=0" 0
assert_has "MP-25.5 遮蔽时多一行提示" "PATH 上的 xteam 是"
assert_has "MP-25.5 提示含遮蔽路径" "bin25a/xteam"

# MP-25.2 同一份安装：软链在前 → 不报遮蔽（不误报）
run env PATH="$BASE/bin25b:/usr/bin:/bin" \
  python3 "$REPO/bin/xteam" --project "$T" doctor
assert_rc  "MP-25.2 doctor rc=0" 0
assert_has "MP-25.2 节仍在（命中如实列出）" "bin25b/xteam"
printf '%s' "$LAST_OUT" | grep -qF '遮蔽' \
  && bad "MP-25.2 同一份安装误报遮蔽" "$LAST_OUT" \
  || ok  "MP-25.2 同一份安装不报遮蔽"

# MP-25.3 PATH 上只有一份=本次运行这份 → 不报
run env PATH="$BASE/bin25empty:/usr/bin:/bin" \
  python3 "$REPO/bin/xteam" --project "$T" doctor
assert_rc  "MP-25.3 doctor rc=0" 0
assert_has "MP-25.3 如实说 PATH 上没有" "PATH 上没有 xteam"
printf '%s' "$LAST_OUT" | grep -qF '遮蔽' \
  && bad "MP-25.3 无命中误报遮蔽" "$LAST_OUT" \
  || ok  "MP-25.3 无命中不报遮蔽"

# MP-25.4 --version 不遮蔽时逐字形状=改前 5 行且不含 ⚠
run env PATH="$BASE/bin25b:/usr/bin:/bin" \
  python3 "$REPO/bin/xteam" --version
assert_rc "MP-25.4 --version rc=0" 0
[ "$(printf '%s\n' "$LAST_OUT" | wc -l | tr -d ' ')" -eq 5 ] \
  && ok "MP-25.4 仍是 5 行" \
  || bad "MP-25.4 行数变了" "$LAST_OUT"
printf '%s' "$LAST_OUT" | grep -qF '⚠' \
  && bad "MP-25.4 不遮蔽却有 ⚠" "$LAST_OUT" \
  || ok "MP-25.4 不含 ⚠"
printf '%s\n' "$LAST_OUT" | grep -qE '^xteam [0-9]+\.[0-9]+\.[0-9]+' \
  && ok "MP-25.4 首行版本号形状不变" \
  || bad "MP-25.4 首行变了" "$LAST_OUT"

# MP-25.5 回归：MP-12~MP-24 断言继续全过即自动满足（末行 N/N）。

# ---------------------------------------------------------------- MP-26
step "MP-26" "say 三态单值（已送达/已投递·未确认/投递失败）+ 墙钟与 T 无关（F-19）"

T="$BASE/mp26"
mkrepo "$T" fe api admin
run xin "$T" init --repo-kind multi-api --projects fe,api,admin --shared-api api
assert_rc "MP-26.0 init 布景 rc=0" 0

# 替身（三态可切）：agent prompt 记时刻 T0；dev 的 agent_status =
#   FORCE_ST（恒 working/idle 预设）优先，否则 T0 起过 FLIP_S 秒才 working
#   （默认 999 = 恒 idle）；MARK 由 send-keys 落；pane read 回 $TAIL。
mkdir -p "$BASE/bin26"
cat > "$BASE/bin26/herdr" <<'EOF'
#!/bin/sh
{ printf '%s %s %s' "$1" "$2" "$3"; [ "$2" = "send-keys" ] && printf ' %s' "$4"; printf '\n'; } >> "$LOGF"
T0=$(cat "$T0F" 2>/dev/null || echo 0); NOW=$(date +%s)
if [ -f "$MARK" ]; then ST=working
elif [ -n "$FORCE_ST" ]; then ST="$FORCE_ST"
elif [ "$T0" -gt 0 ] && [ $((NOW-T0)) -ge "${FLIP_S:-999}" ]; then ST=working
else ST=idle; fi
case "$1 $2" in
  "workspace list") printf '%s\n' '{"result": {"workspaces": [{"workspace_id":"wV","label":"'"$LABEL"'","pane_count":3,"tab_count":1,"active_tab_id":"wV:t1","agent_status":"idle","focused":false}]}}' ;;
  "agent list")     printf '%s\n' '{"result": {"agents": [{"pane_id":"wV:p1","name":"pm-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wV","tab_id":"wV:t1"},{"pane_id":"wV:p2","name":"tl-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wV","tab_id":"wV:t1"},{"pane_id":"wV:p3","name":"dev-'"$LABEL"'","agent_status":"'"$ST"'","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wV","tab_id":"wV:t1"}]}}' ;;
  "pane list")      printf '%s\n' '{"result": {"panes": [{"pane_id":"wV:p1","cwd":"'"$ROOT"'"},{"pane_id":"wV:p2","cwd":"'"$ROOT"'"},{"pane_id":"wV:p3","cwd":"'"$ROOT"'"}]}}' ;;
  "agent prompt")   date +%s > "$T0F"; if [ -n "$PROMPT_FAIL" ]; then printf 'boom\n' >&2; exit 1; fi; : ;;
  "pane read")      cat "$TAIL" ;;
  "pane send-keys") touch "$MARK"; printf '%s\n' '{"result": {}}' ;;
  *) printf '%s\n' '{"result": {}}' ;;
esac
EOF
chmod +x "$BASE/bin26/herdr"
xin26() {
  local t="$1" tail="$2"; shift 2
  (cd "$t" && PATH="$BASE/bin26:$PATH" LABEL=mp26 ROOT="$t" \
    T0F="$BASE/mp26.t0" MARK="$BASE/mp26.mark" LOGF="$BASE/mp26.log" \
    TAIL="$tail" FLIP_S="${FLIP_S:-999}" FORCE_ST="${FORCE_ST:-}" \
    PROMPT_FAIL="${PROMPT_FAIL:-}" \
    python3 "$REPO/bin/xteam" --project "$t" --workspace mp26 "$@" </dev/null)
}
reset26() { rm -f "$BASE/mp26.t0" "$BASE/mp26.mark" "$BASE/mp26.log" "$BASE/mp26.err"; }
wall_s() { python3 -c 'import time,sys; t=float(sys.argv[1]); print(f"{time.time()-t:.1f}")' "$1"; }
now_s()  { python3 -c 'import time; print(time.time())'; }

# MP-26.1（AC-1）：目标立刻 working（FORCE_ST=working、尾巴无占位符）→ 已送达 rc0。
reset26
FORCE_ST=working run xin26 "$T" "$BASE/tailB.txt" say dev "x"
assert_rc  "MP-26.1 立刻 working rc=0" 0
assert_has "MP-26.1 主行含 已送达" "已送达（"
printf '%s' "$(cat "$BASE/mp26.log")" | grep -qF 'pane send-keys' \
  && bad "MP-26.1 输入行有内容还按了 enter（F-6 边界破了）" "$(cat "$BASE/mp26.log")" \
  || ok  "MP-26.1(AC-9) 输入行有内容→无 send-keys"

# MP-26.2（AC-2）：目标恒 idle → 「已投递·未确认」rc0，stdout/stderr 均无未送达。
reset26
T0=$(now_s)
LAST_OUT="$(FLIP_S=999 xin26 "$T" "$BASE/tailB.txt" say dev "x" 2>"$BASE/mp26.err")"; LAST_RC=$?
E2=$(wall_s "$T0")
assert_rc  "MP-26.2 恒 idle rc=0" 0
assert_has "MP-26.2 含 已投递" "已投递"
assert_has "MP-26.2 含 未确认" "未确认"
printf '%s' "$LAST_OUT" | grep -qE '未送达|已送达' \
  && bad "MP-26.2 误报 未送达/已送达" "$LAST_OUT" \
  || ok  "MP-26.2 单值措辞（不含未送达/已送达）"
grep -qF '未送达' "$BASE/mp26.err" \
  && bad "MP-26.2 stderr 含未送达" "$(cat "$BASE/mp26.err")" \
  || ok  "MP-26.2 stderr 不含未送达"

# MP-26.3（AC-3+AC-5）：延迟 3s 才 working（超出 1s 复核窗口）→ 已投递·未确认 rc0，
# 墙钟 ≤6s 且与 T 无关（不睡等）。
reset26
T0=$(now_s)
LAST_OUT="$(FLIP_S=3 xin26 "$T" "$BASE/tailB.txt" say dev "x" 2>/dev/null)"; LAST_RC=$?
E3=$(wall_s "$T0")
assert_rc  "MP-26.3 延迟3s rc=0" 0
assert_has "MP-26.3 延迟3s 是已投递·未确认" "已投递·未确认"
printf '%s' "$LAST_OUT" | grep -qF '已送达' \
  && bad "MP-26.3 睡等目标翻状态了" "$LAST_OUT" \
  || ok  "MP-26.3 不含已送达（没睡等）"

reset26
T0=$(now_s)
LAST_OUT="$(FLIP_S=30 xin26 "$T" "$BASE/tailB.txt" say dev "x" 2>/dev/null)"; LAST_RC=$?
E30=$(wall_s "$T0")
assert_rc  "MP-26.3 T=30s rc=0" 0
assert_has "MP-26.3 T=30s 同措辞" "已投递·未确认"
DIFF=$(python3 -c "print(abs(float('$E3')-float('$E30')))")
[ "$(python3 -c "print(float('$E3')<=6 and float('$E30')<=6)")" = "True" ] \
  && ok  "MP-26.5 墙钟 T=3s:${E3}s / T=30s:${E30}s 均 ≤6s" \
  || bad "MP-26.5 墙钟超 6s" "T=3s:${E3}s T=30s:${E30}s"
[ "$(python3 -c "print(float('$DIFF')<=2)")" = "True" ] \
  && ok  "MP-26.5 墙钟与 T 无关（差 ${DIFF}s ≤2s）" \
  || bad "MP-26.5 墙钟随 T 变大" "T=3s:${E3}s T=30s:${E30}s"

# MP-26.4（AC-4）：agent prompt 非 0 → 投递失败 rc=1（唯一真失败，不许降级）。
reset26
LAST_OUT="$(PROMPT_FAIL=1 xin26 "$T" "$BASE/tailB.txt" say dev "x" 2>"$BASE/mp26.err")"; LAST_RC=$?
assert_rc "MP-26.4 prompt 失败 rc≠0" nz
grep -qF '投递失败' "$BASE/mp26.err" \
  && ok  "MP-26.4 stderr 含投递失败" \
  || bad "MP-26.4 stderr 缺投递失败" "$(cat "$BASE/mp26.err")"

# MP-26.6（AC-8）：目标 mid-turn（恒 working）+ 尾巴有占位符 → 必须读 pane
# 补一次 enter 再复核。
reset26
FORCE_ST=working run xin26 "$T" "$BASE/tailC.txt" say dev "x"
assert_rc  "MP-26.6 mid-turn+占位符 rc=0" 0
assert_has "MP-26.6 主行含 已送达" "已送达（"
[ "$(grep -c 'pane send-keys' "$BASE/mp26.log")" -eq 1 ] \
  && ok  "MP-26.6(AC-8) mid-turn 占位符恰补一次 enter" \
  || bad "MP-26.6(AC-8) mid-turn 排队门铃没补 enter" "$(cat "$BASE/mp26.log")"

# MP-26.7 回归：MP-12~MP-25 断言继续全过即自动满足（末行 N/N）。

# ---------------------------------------------------------------- MP-27
step "MP-27" "义务名按 QUEUE 序挑片 + next-slice 标签取首个 todo（F-20）"

T="$BASE/mp27"
mkrepo "$T" app
run xin "$T" init --repo-kind single
assert_rc "MP-27.0 init 布景 rc=0" 0

# 布景（spec §3）：目录名与 QUEUE 序故意错开——aa-early 目录序在前、
# zz-late 队列序在前；两片同处 spec v1（tl:assess），义务动词不是判据。
for s in zz-late aa-early; do
  d="$T/.xteam/tasks/$s"; mkdir -p "$d"
  printf '# spec\n'       > "$d/spec.md"
  printf '{"round":1}\n'  > "$d/spec.json"
done
printf '| 1 | zz-late | doing | |\n| 2 | aa-early | doing | |\n' \
  >> "$T/.xteam/QUEUE.md"

# 替身：三 pane 恒 idle（同 MP-24 的静态形状）。
mkdir -p "$BASE/bin27"
cat > "$BASE/bin27/herdr" <<'EOF'
#!/bin/sh
case "$1 $2" in
  "workspace list") printf '%s\n' '{"result": {"workspaces": [{"workspace_id":"wH","label":"'"$LABEL"'","pane_count":3,"tab_count":1,"active_tab_id":"wH:t1","agent_status":"idle","focused":false}]}}' ;;
  "agent list")     printf '%s\n' '{"result": {"agents": [{"pane_id":"wH:p1","name":"pm-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wH","tab_id":"wH:t1"},{"pane_id":"wH:p2","name":"tl-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wH","tab_id":"wH:t1"},{"pane_id":"wH:p3","name":"dev-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wH","tab_id":"wH:t1"}]}}' ;;
  "pane list")      printf '%s\n' '{"result": {"panes": [{"pane_id":"wH:p1","cwd":"'"$ROOT"'"},{"pane_id":"wH:p2","cwd":"'"$ROOT"'"},{"pane_id":"wH:p3","cwd":"'"$ROOT"'"}]}}' ;;
  *) printf '%s\n' '{"result": {}}' ;;
esac
EOF
chmod +x "$BASE/bin27/herdr"
xin27() {
  local t="$1"; shift
  (cd "$t" && PATH="$BASE/bin27:$PATH" LABEL=mp27 ROOT="$t" \
    python3 "$REPO/bin/xteam" --project "$t" --workspace mp27 "$@" </dev/null)
}

# MP-27.1（AC-1/证据A）：错开序 → tl pending[0] = 队列序 1 的 zz-late，
# 不是目录序在前的 aa-early。
run xin27 "$T" status
assert_rc   "MP-27.1 status rc=0" 0
assert_line "MP-27.1 tl 行指向 zz-late（队列序）" "tl " "zz-late"
assert_line "MP-27.1 tl 行不含 aa-early（目录序让位）" "tl " "" "aa-early"

# MP-27.2（AC-2）：序与目录序一致 → 仍取 aa-early（行为不变，双向钉死）。
printf '# 队列\n\n| 序 | 切片 | 状态 | 备注 |\n|---|---|---|---|\n| 1 | aa-early | doing | |\n| 2 | zz-late | doing | |\n' \
  > "$T/.xteam/QUEUE.md"
run xin27 "$T" status
assert_rc   "MP-27.2 status rc=0" 0
assert_line "MP-27.2 tl 行指向 aa-early（一致序不变）" "tl " "aa-early"
assert_line "MP-27.2 tl 行不含 zz-late" "tl " "" "zz-late"

# MP-27.3（AC-3/证据B）：闭合片 done-old + 队列 todo 项 fresh-next（无目录
# = 未开工）→ pm 的 next-slice 标签 = fresh-next；输出逐名 grep 无闭合片名。
d="$T/.xteam/tasks/done-old"; mkdir -p "$d"
printf '# spec\n'  > "$d/spec.md"
printf 'closed\n'  > "$d/closed.md"
printf '# 队列\n\n| 序 | 切片 | 状态 | 备注 |\n|---|---|---|---|\n| 1 | done-old | done | |\n| 2 | fresh-next | todo | |\n' \
  > "$T/.xteam/QUEUE.md"
run xin27 "$T" status
assert_rc   "MP-27.3 status rc=0" 0
assert_line "MP-27.3 pm 行含 fresh-next（首个 todo 项）" "pm " "fresh-next"
assert_line "MP-27.3 pm 行是 next-slice" "pm " "next-slice"
printf '%s' "$LAST_OUT" | grep -qF 'done-old' \
  && bad "MP-27.3 输出出现闭合片名 done-old" "$LAST_OUT" \
  || ok  "MP-27.3 输出无闭合片名（逐名 grep）"

# MP-27.4（AC-4）：队列全 done → 无未开工项 → next-slice 让位，PM 欠 report
#（现状路径不变）。
printf '# 队列\n\n| 序 | 切片 | 状态 | 备注 |\n|---|---|---|---|\n| 1 | done-old | done | |\n' \
  > "$T/.xteam/QUEUE.md"
run xin27 "$T" status
assert_rc   "MP-27.4 status rc=0" 0
assert_line "MP-27.4 pm 行欠 report（让位路径不变）" "pm " "report"

# MP-27.5（AC-5）：QUEUE 缺失 → 不崩，退回目录序；坏格式同样。
rm "$T/.xteam/QUEUE.md"
run xin27 "$T" status
assert_rc   "MP-27.5 QUEUE 缺失 status rc=0" 0
assert_line "MP-27.5 缺失时 tl 回退目录序 aa-early" "tl " "aa-early"
printf '这不是表格\n???\n' > "$T/.xteam/QUEUE.md"
run xin27 "$T" status
assert_rc   "MP-27.5 坏 QUEUE status rc=0" 0
assert_line "MP-27.5 坏格式 tl 仍回退目录序 aa-early" "tl " "aa-early"

# MP-27.6 回归：MP-12~MP-26 断言继续全过即自动满足（末行 N/N）。

# ---------------------------------------------------------------- MP-28
step "MP-28" "status 头部版本漂移警告：运行版 ≠ pane 部署版时报（F-21）"

T="$BASE/mp28"
mkrepo "$T" app
run xin "$T" init --repo-kind single
assert_rc "MP-28.0 init 布景 rc=0" 0

# 替身：三 pane 恒 idle（同 MP-24/MP-27 的静态形状）。
mkdir -p "$BASE/bin28"
cat > "$BASE/bin28/herdr" <<'EOF'
#!/bin/sh
case "$1 $2" in
  "workspace list") printf '%s\n' '{"result": {"workspaces": [{"workspace_id":"wI","label":"'"$LABEL"'","pane_count":3,"tab_count":1,"active_tab_id":"wI:t1","agent_status":"idle","focused":false}]}}' ;;
  "agent list")     printf '%s\n' '{"result": {"agents": [{"pane_id":"wI:p1","name":"pm-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wI","tab_id":"wI:t1"},{"pane_id":"wI:p2","name":"tl-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wI","tab_id":"wI:t1"},{"pane_id":"wI:p3","name":"dev-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wI","tab_id":"wI:t1"}]}}' ;;
  "pane list")      printf '%s\n' '{"result": {"panes": [{"pane_id":"wI:p1","cwd":"'"$ROOT"'"},{"pane_id":"wI:p2","cwd":"'"$ROOT"'"},{"pane_id":"wI:p3","cwd":"'"$ROOT"'"}]}}' ;;
  *) printf '%s\n' '{"result": {}}' ;;
esac
EOF
chmod +x "$BASE/bin28/herdr"
xin28() {
  local t="$1"; shift
  (cd "$t" && PATH="$BASE/bin28:$PATH" LABEL=mp28 ROOT="$t" \
    python3 "$REPO/bin/xteam" --project "$t" --workspace mp28 "$@" </dev/null)
}
REPOV="$(cat "$REPO/VERSION")"

# MP-28.1（AC-1）：session.json 记 pane 版 0.0.1 → 警告行含两版本+修复指引。
printf '{"workspace":"mp28","panes":{},"xteam_version":"0.0.1"}\n' \
  > "$T/.xteam/session.json"
run xin28 "$T" status
assert_rc   "MP-28.1 status rc=0" 0
assert_has  "MP-28.1 警告行含 pane 版 0.0.1" "pane 里投的是 0.0.1"
assert_has  "MP-28.1 警告行含运行版 $REPOV" "xteam $REPOV"
assert_has  "MP-28.1 警告行含修复指引" "xteam up"

# MP-28.2（真实记录位）：session.json 无键、rules.json 记 0.0.1 → 同样警告
#（up 实际把版本写在 rules.json——spec 写 session.json 是名义口径，两个都认）。
printf '{"workspace":"mp28","panes":{}}\n' > "$T/.xteam/session.json"
printf '{"xteam_version":"0.0.1"}\n'    > "$T/.xteam/rules.json"
run xin28 "$T" status
assert_rc   "MP-28.2 status rc=0" 0
assert_has  "MP-28.2 警告行含 pane 版 0.0.1" "pane 里投的是 0.0.1"
assert_has  "MP-28.2 警告行含运行版 $REPOV" "xteam $REPOV"
assert_has  "MP-28.2 警告行含修复指引" "xteam up"

# MP-28.3（AC-2）：两版本一致 → 无警告行（不制造噪音）。
printf "{\"xteam_version\":\"$REPOV\"}\n" > "$T/.xteam/rules.json"
run xin28 "$T" status
assert_rc   "MP-28.3 status rc=0" 0
printf '%s' "$LAST_OUT" | grep -qF '版本漂移' \
  && bad "MP-28.3 版本一致还打警告" "$LAST_OUT" \
  || ok  "MP-28.3 一致→无警告行"

# MP-28.4（AC-3）：session.json 无键 + rules.json 缺 → 不崩不警告。
rm -f "$T/.xteam/rules.json"
run xin28 "$T" status
assert_rc   "MP-28.4 status rc=0（无版本记录）" 0
printf '%s' "$LAST_OUT" | grep -qF '版本漂移' \
  && bad "MP-28.4 无记录还打警告" "$LAST_OUT" \
  || ok  "MP-28.4 无记录→无警告行"
# session.json 整个缺失同样静默
rm -f "$T/.xteam/session.json"
run xin28 "$T" status
assert_rc   "MP-28.4 session.json 缺失 rc=0" 0
printf '%s' "$LAST_OUT" | grep -qF '版本漂移' \
  && bad "MP-28.4 session 缺失还打警告" "$LAST_OUT" \
  || ok  "MP-28.4 session 缺失→无警告行"

# MP-28.5 回归：MP-12~MP-27 断言继续全过即自动满足（末行 N/N）。

# ---------------------------------------------------------------- MP-29
step "MP-29" "request 落地超阈值未交付 → status 点名 TL 派发（F-23）"

T="$BASE/mp29"
mkrepo "$T" app
run xin "$T" init --repo-kind single
assert_rc "MP-29.0 init 布景 rc=0" 0

# 布景（spec §3）：stale-req 有 request.md、无 delivered.json；mtime 用
# os.utime 拨回 601s 前模拟超阈值（不真实睡眠）。
d="$T/.xteam/tasks/stale-req"; mkdir -p "$d"
printf '# spec\n'    > "$d/spec.md"
printf '# request\n' > "$d/request.md"
python3 - "$d/request.md" <<'PY'
import os, sys, time
t = time.time() - 601
os.utime(sys.argv[1], (t, t))
PY

# 替身：三 pane 恒 idle（同 MP-24/MP-28 的静态形状）。
mkdir -p "$BASE/bin29"
cat > "$BASE/bin29/herdr" <<'EOF'
#!/bin/sh
case "$1 $2" in
  "workspace list") printf '%s\n' '{"result": {"workspaces": [{"workspace_id":"wJ","label":"'"$LABEL"'","pane_count":3,"tab_count":1,"active_tab_id":"wJ:t1","agent_status":"idle","focused":false}]}}' ;;
  "agent list")     printf '%s\n' '{"result": {"agents": [{"pane_id":"wJ:p1","name":"pm-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wJ","tab_id":"wJ:t1"},{"pane_id":"wJ:p2","name":"tl-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wJ","tab_id":"wJ:t1"},{"pane_id":"wJ:p3","name":"dev-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wJ","tab_id":"wJ:t1"}]}}' ;;
  "pane list")      printf '%s\n' '{"result": {"panes": [{"pane_id":"wJ:p1","cwd":"'"$ROOT"'"},{"pane_id":"wJ:p2","cwd":"'"$ROOT"'"},{"pane_id":"wJ:p3","cwd":"'"$ROOT"'"}]}}' ;;
  *) printf '%s\n' '{"result": {}}' ;;
esac
EOF
chmod +x "$BASE/bin29/herdr"
xin29() {
  local t="$1"; shift
  (cd "$t" && PATH="$BASE/bin29:$PATH" LABEL=mp29 ROOT="$t" \
    python3 "$REPO/bin/xteam" --project "$t" --workspace mp29 "$@" </dev/null)
}

# MP-29.1（AC-1）：超阈值 → status 有提示行，点名 stale-req 去「派发」，
# 且措辞是给 TL 的（「门铃 dev」），不是只把 implement 挂 dev 头上。
run xin29 "$T" status
assert_rc   "MP-29.1 status rc=0" 0
assert_has  "MP-29.1 提示行在（派发超时）" "request 派发超时"
assert_has  "MP-29.1 点名该片 stale-req" "stale-req"
assert_has  "MP-29.1 含「派发」字样" "派发"
assert_has  "MP-29.1 指向 TL 门铃 dev" "门铃 dev"

# MP-29.2（AC-2）：mtime 拨回当前 → 未超阈值，提示行消失（不误报）。
python3 - "$d/request.md" <<'PY'
import os, sys, time
t = time.time()
os.utime(sys.argv[1], (t, t))
PY
run xin29 "$T" status
assert_rc   "MP-29.2 status rc=0" 0
printf '%s' "$LAST_OUT" | grep -qF 'request 派发超时' \
  && bad "MP-29.2 未超阈值还点名派发" "$LAST_OUT" \
  || ok  "MP-29.2 刚落地不催（不误报）"

# MP-29.3：已交付（delivered.json 在）→ 即使 mtime 老也不点名。
python3 - "$d/request.md" <<'PY'
import os, sys, time
t = time.time() - 3600
os.utime(sys.argv[1], (t, t))
PY
printf '{"round":1}\n' > "$d/delivered.json"
run xin29 "$T" status
assert_rc   "MP-29.3 status rc=0" 0
printf '%s' "$LAST_OUT" | grep -qF 'request 派发超时' \
  && bad "MP-29.3 已交付还点名派发" "$LAST_OUT" \
  || ok  "MP-29.3 已交付不点名"

# MP-29.4 回归：MP-12~MP-28 断言继续全过即自动满足（末行 N/N）。

# ---------------------------------------------------------------- 收尾
printf '\n'
printf '%s/%s 通过\n' "$PASS" "$TOTAL"
[ "$PASS" -eq "$TOTAL" ]
