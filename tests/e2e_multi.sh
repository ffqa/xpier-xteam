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
  (cd "$t" && python3 "$REPO/bin/xteam" --project "$t" "$@" </dev/null)
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
mkdir -p "$BASE/bin12"
cat > "$BASE/bin12/herdr" <<'EOF'
#!/bin/sh
case "$1 $2" in
  "workspace list") printf '%s\n' '{"result": {"workspaces": [{"workspace_id":"wS","label":"'"$LABEL"'","pane_count":3,"tab_count":1,"active_tab_id":"wS:t1","agent_status":"idle","focused":false}]}}' ;;
  "agent list")     printf '%s\n' '{"result": {"agents": [{"pane_id":"wS:p1","name":"pm-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wS","tab_id":"wS:t1"},{"pane_id":"wS:p2","name":"tl-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wS","tab_id":"wS:t1"},{"pane_id":"wS:p3","name":"dev-'"$LABEL"'","agent_status":"idle","state_change_seq":1,"cwd":"'"$ROOT"'","workspace_id":"wS","tab_id":"wS:t1"}]}}' ;;
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

# ---------------------------------------------------------------- 收尾
printf '\n'
printf '%s/%s 通过\n' "$PASS" "$TOTAL"
[ "$PASS" -eq "$TOTAL" ]
