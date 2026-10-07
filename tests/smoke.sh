#!/usr/bin/env bash
# 端到端冒烟：真的拉起三个 pane，跑通一轮协议，然后收摊。
#
# 这不是单元测试的重复——它验证的是单元测试碰不到的部分：herdr 能不能程序化建
# workspace/tab/pane、agent 能不能被拉起并认领名字、门铃能不能真的送达。
#
# 用法：bash tests/smoke.sh [工作目录]
#       bash tests/smoke.sh --no-e2e [工作目录]   # 只跑前两层，不起 pane
#
# --no-e2e 存在的理由：第 3 层要 herdr + 真 agent 起三个 pane。CI 上跑它必然失败
# （「找不到 herdr —— 它是 xteam 的地基」），而 pack.sh --worktree 的自证会调本脚本。
# 于是 CI 只能退而用 --no-verify 跳过整个自证 —— 那就等于**发版时不再验证包可用**。
# 有了这个开关，CI 能验证「解包副本能装、能编译、单测全过」，只跳过真正需要
# 人和机器的那一层。
set -uo pipefail

E2E=1
WORK=""
for a in "$@"; do
  case "$a" in
    --no-e2e) E2E=0 ;;
    -*) echo "未知参数：$a （可用：--no-e2e）" >&2; exit 1 ;;
    *)  [ -z "$WORK" ] && WORK="$a" || { echo "只能给一个工作目录" >&2; exit 1; } ;;
  esac
done

BIN="$(cd "$(dirname "$0")/.." && pwd)/bin/xteam"
WORK="${WORK:-$(mktemp -d)}"
WS="pm-smoke-$$"
FAIL=0

ok()   { printf '  ✓ %s\n' "$1"; }
bad()  { printf '  ✗ %s\n     %s\n' "$1" "${2:-}"; FAIL=1; }
step() { printf '\n[%s] %s\n' "$1" "$2"; }

# 门禁在起 pane 之前就要能退出，而 WORK 是 mktemp 立即建出来的 —— 所以 trap
# 必须**先于门禁**注册，否则门禁一失败就留下一个没人清的临时目录。
cleanup() {
  if [ -d "$WORK/.xteam" ]; then
    "$BIN" --project "$WORK" --workspace "$WS" watch stop >/dev/null 2>&1
    # down 现在**只关 pane、保留 workspace**（可复用是设计意图），所以这里
    # 得自己把 workspace 关掉，否则冒烟跑一次就在屏幕上留一个。
    "$BIN" --project "$WORK" --workspace "$WS" down >/dev/null 2>&1
    wsid=$(herdr workspace list 2>/dev/null | python3 -c "
import json,sys
try: rows=json.load(sys.stdin)['result']['workspaces']
except Exception: rows=[]
print(next((x['workspace_id'] for x in rows if x.get('label')=='$WS'), ''))" 2>/dev/null)
    [ -n "$wsid" ] && herdr workspace close "$wsid" >/dev/null 2>&1
  fi
  [ "${KEEP_WORK:-0}" = "1" ] || rm -rf "$WORK"
}
trap cleanup EXIT

# 收尾摘要抽成函数：中途 abort 的路径也要能报「失败在哪一层」，
# 否则最需要定位的时候反而什么都看不到。
finish() {
  printf '\n'
  if [ $FAIL -eq 0 ]; then
    # 摘要必须区分「三层全过」和「按要求只跑了两层」—— 否则 --no-e2e 的绿灯
    # 会被读成「端到端也验过了」，而它恰恰没验。
    if [ "${E2E:-1}" = "0" ]; then
      printf '✓ 门禁全过（端到端按 --no-e2e 跳过）：静态 · 单测 %s 条断言 · 多项目 e2e\n' "${N_ASSERT:-0}"
    else
      printf '✓ 门禁全过：静态 · 单测 %s 条断言 · 多项目 e2e · 端到端\n' "${N_ASSERT:-0}"
    fi
    printf '  工作目录 %s 即将删除\n' "$WORK"
  else
    printf '✗ 失败在【%s】层；已过的层：静态 · 单测 %s 条断言\n' \
      "${LAYER:-端到端}" "${N_ASSERT:-0}"
    printf '  工作目录保留在 %s 供排查。\n' "$WORK"
    KEEP_WORK=1
  fi
  exit $FAIL
}

# ---------------------------------------------------------------- 门禁（3 层）
# 按「快 → 慢」分层，**失败就停在那一层**。理由不只是省时间：坏代码上跑出来的
# 结果本身是误导的（status 崩在半路，看着像协议有问题，真因只是方法名写错）。
#
#   1/3 静态   约 3s   编译期查不出、只在跑到那条路径才炸的错误
#   2/3 单测   约 10s  协议状态机（跨项目依赖 / 契约边界 / 门禁逻辑）——唯一覆盖它的地方
#   3/3 端到端 约 110s herdr 编排、门铃送达、巡检、swap
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LAYER=""
layer_fail() { LAYER="$1"; }

step "门禁 1/3" "静态检查：未定义的名字 / 不存在的方法 / shell 静默错误写法"
if out=$(python3 "$REPO/tests/lint_names.py" 2>&1); then
  ok "没有「读了未定义的名字」或「调用了不存在的方法」"
else
  bad "未定义检查不通过" "$out"; layer_fail "静态"
fi
if out=$(python3 "$REPO/tests/lint_shell.py" "$REPO/install.sh" "$REPO/tests/smoke.sh" "$REPO/tests/e2e_multi.sh" 2>&1); then
  ok "shell 没有会静默出错的写法"
else
  bad "shell 检查不通过" "$out"; layer_fail "静态"
fi
for f in "$REPO/bin/xteam" "$REPO/bin/xteam_lib.py" "$REPO/tests/test_protocol.py"; do
  if out=$(python3 -m py_compile "$f" 2>&1); then
    ok "编译 $(basename "$f")"
  else
    bad "编译失败 $(basename "$f")" "$out"; layer_fail "静态"
  fi
done

step "门禁 2/3" "协议单测：状态机 / 跨项目依赖 / 契约边界 / idle 判定"
UNIT_OUT=$(python3 "$REPO/tests/test_protocol.py" 2>&1)
UNIT_RC=$?
N_ASSERT=$(printf '%s\n' "$UNIT_OUT" | grep -c '✓')
if [ "$UNIT_RC" -eq 0 ]; then
  ok "协议单测全过（$N_ASSERT 条断言）"
else
  bad "协议单测失败" "$(printf '%s\n' "$UNIT_OUT" | grep '✗' | head -3)"
  layer_fail "单测"
fi

if [ -n "$LAYER" ]; then
  printf '\n✗ 门禁未过（%s层），不起 pane —— 省下两分钟，' "$LAYER"
  printf '也避免在坏代码上跑出误导结果\n'
  exit 1
fi

# 多项目治理的端到端层：mktemp 里真 git 仓 + herdr 替身，不需要真 herdr，
# 所以 --no-e2e 与完整路径都必须跑到（AC-7：只许跳过真要 herdr 的第 3 层）。
step "门禁 2.5/3" "多项目治理 e2e：tests/e2e_multi.sh（hermetic）"
if out=$(bash "$REPO/tests/e2e_multi.sh" 2>&1); then
  ok "e2e_multi 全过（$(printf '%s\n' "$out" | tail -1)）"
else
  bad "e2e_multi 失败" "$(printf '%s\n' "$out" | tail -8)"
  layer_fail "e2e_multi"
fi
if [ -n "$LAYER" ]; then
  printf '\n✗ 门禁未过（%s层），不起 pane\n' "$LAYER"
  exit 1
fi

# --no-e2e 在这里返回，**不是**在第 2 层之后 —— 因为第 3 层的准备动作
# （mkdir 工作目录、git init、装两次 xteam）本身不需要 herdr，白跑没有意义，
# 而它们又是「解包副本能不能接着开发」的一部分证据，保留。
if [ "$E2E" = "0" ]; then
  step "门禁 3/3" "端到端：--no-e2e 已跳过（需要 herdr + 真 agent）"
  finish
fi

step "门禁 3/3" "端到端：真起三个 pane，验门铃 / 巡检 / 换 agent"


mkdir -p "$WORK"
cd "$WORK" || exit 1
git init -q . 2>/dev/null
printf '# smoke\n' > README.md
git add -A && git -c user.email=t@t -c user.name=t commit -qm init

# ------------------------------------------------- 0. 装两次（升级必须真生效）
step 0 "install 幂等：装两次后结构一致，且升级真的替换掉旧文件"
IPRE="$WORK/.install"
SRCC="$WORK/.src"
cp -R "$(dirname "$BIN")/.." "$SRCC" 2>/dev/null
rm -rf "$SRCC/.git"
bash "$SRCC/install.sh" --prefix "$IPRE" >/dev/null 2>&1
# 模拟升级：改源里的角色章程，再装一次
printf '\n<!-- smoke upgrade marker -->\n' >> "$SRCC/roles/pm.md"
bash "$SRCC/install.sh" --prefix "$IPRE" >/dev/null 2>&1
if [ -d "$IPRE/share/xteam/roles/roles" ]; then
  bad "第二次安装套娃出 roles/roles" "cp -R 到已存在目录的经典坑"
else
  ok "两次安装后目录结构一致"
fi
if grep -q "smoke upgrade marker" "$IPRE/share/xteam/roles/pm.md" 2>/dev/null; then
  ok "升级真的替换了顶层章程（不是埋进嵌套目录）"
else
  bad "升级静默不生效" "用户重装后仍在跑旧章程"
fi
if "$IPRE/bin/xteam" --help >/dev/null 2>&1; then
  ok "装完的入口可执行"
else
  bad "装完的入口跑不起来" ""
fi
rm -rf "$SRCC" "$IPRE"


# ---------------------------------------------------------------- 1. 协议骨架
step 1 "up：建 .xteam/ 骨架"
out=$("$BIN" --project "$WORK" --workspace "$WS" up "$WORK" 2>&1)
rc=$?
if [ $rc -eq 0 ]; then
  ok ".xteam/ 建出，workspace=$WS"
else
  bad "up 失败" "$out"
  layer_fail "端到端/up"
  printf '  workspace 没建起来，后续无从验证\n'
  finish
fi
for f in .xteam/QUEUE.md .xteam/PROTOCOL.md .xteam/tasks; do
  [ -e "$f" ] && ok "存在 $f" || bad "缺 $f"
done

# ---------------------------------------------------------------- 2. 三个 pane
step 2 "三个 pane 起来了且认领了角色名"
# 过滤必须用 workspace_id（如 w12），不是 label（pm-smoke-xxx）——
# 用 label 过滤会永远查空，之前三处误报全是这个。
WSID=$(herdr workspace list 2>/dev/null | python3 -c "
import json,sys
for x in json.load(sys.stdin)['result']['workspaces']:
    if x.get('label')=='$WS': print(x['workspace_id'])
")
# agent name 带 workspace 命名空间（herdr 里 name 全局唯一，不加前缀两个项目会撞）
ROWS=$(herdr agent list 2>/dev/null | python3 -c "
import json,sys
for a in json.load(sys.stdin)['result']['agents']:
    if a.get('pane_id','').startswith('$WSID:'):
        name = a.get('name','-')
        role = name.split('-', 1)[0]      # 剥掉 -<scope> 后缀，还原角色名
        print(role, a.get('agent_status','-'), a.get('pane_id','-'), name)
")
for role in pm tl dev; do
  line=$(printf '%s\n' "$ROWS" | grep "^$role " || true)
  if [ -n "$line" ]; then
    ok "$role 已认领（$(echo "$line" | cut -d' ' -f2,3)）"
  else
    bad "$role 没认领名字" "现有：$ROWS"
  fi
done

# ---------------------------------------------------------------- 2b. 中途换 agent
step 2b "swap：中途把 tl 换成另一个 agent（交接 + 配置落盘 + 映射更新）"
OLD_TL=$(printf '%s\n' "$ROWS" | grep '^tl ' | awk '{print $3}')
# tl 刚被投完章程，可能 working 也可能 idle/blocked —— 都合法，强断会 flaky。
# 安全阀本身由单测用桩固定验证；这里验证「换完之后映射/tab/配置都对得上」。
if swapout=$("$BIN" --project "$WORK" --workspace "$WS" swap tl devin --no-recap --force 2>&1); then
  ok "swap 执行成功（--force）"
  NEW_TL=$(python3 -c "
import json
print((json.load(open('.xteam/session.json')).get('panes') or {}).get('tl',''))")
  if [ -n "$NEW_TL" ] && [ "$NEW_TL" != "$OLD_TL" ]; then
    ok "pane 映射已更新（$OLD_TL → $NEW_TL ）"
  else
    bad "pane 映射没更新" "old=$OLD_TL new=$NEW_TL"
  fi
  if python3 -c "
import json,sys
r=json.load(open('.xteam/team.json')).get('roles',{})
sys.exit(0 if r.get('tl',{}).get('kind')=='devin' else 1)"; then
    ok "team.json 已记住新 agent"
  else
    bad "team.json 没记住 devin" "$(cat .xteam/team.json 2>/dev/null)"
  fi
  if herdr agent list 2>/dev/null | python3 -c "
import json,sys
for a in json.load(sys.stdin)['result']['agents']:
    if a.get('pane_id','').startswith('$WSID:') and a.get('name','').startswith('tl-'):
        sys.exit(0)
sys.exit(1)"; then
    ok "新的 tl agent 已认领名字"
  else
    bad "新 tl 没认领名字" "$swapout"
  fi
else
  bad "swap 失败" "$swapout"
fi

# ---------------------------------------------------------------- 3. 门铃真的送达
step 3 "门铃：say 之后 dev 确实醒了（状态或 state_change_seq 变化）"
# 不断言「必须停在 working」：devin 收到消息后可能一个回合就做完回到 done，
# 那是**送达成功**的表现。真正的判据是它动过了——状态变了或 seq 涨了。
DEVPANE=$(printf '%s\n' "$ROWS" | grep '^dev ' | cut -d' ' -f3)
dev_seq() {
  herdr agent list 2>/dev/null | python3 -c "
import json,sys
for a in json.load(sys.stdin)['result']['agents']:
    if a.get('pane_id')=='$DEVPANE':
        print(a.get('agent_status','-'), a.get('state_change_seq','-'))
"
}
# 预置初值：dev_seq 若无输出（pane 已消失），read 不会赋值，
# set -u 下引用未定义变量会直接中断整个脚本。
before="-"; before_seq="-"
read -r before before_seq <<< "$(dev_seq)"
say_out=$("$BIN" --project "$WORK" --workspace "$WS" say dev "smoke: 读到就回一句收到，不要做别的" 2>&1)
sleep 8
after="-"; after_seq="-"
read -r after after_seq <<< "$(dev_seq)"
if [ "$after" != "$before" ] || [ "$after_seq" != "$before_seq" ]; then
  ok "dev 动了 ${before}/${before_seq} -> ${after}/${after_seq}"
  ok "（门铃真的进去了）"
else
  bad "dev 毫无反应，送达存疑" "before=${before}/${before_seq} after=${after}/${after_seq}"
fi

# ---------------------------------------------------------------- 4. 门铃到不存在的角色必须被拒
step 4 "门铃到不存在的角色要明确失败，不能静默"
if "$BIN" --project "$WORK" --workspace "$WS" say nobody "x" >/dev/null 2>&1; then
  bad "对不存在的角色返回了成功"
else
  ok "不存在的角色被拒（退出码非 0）"
fi

step 5 "status 打出角色 + 义务 + 时间戳"
st=$("$BIN" --project "$WORK" --workspace "$WS" status 2>&1)
if printf '%s' "$st" | grep -q "pm" && printf '%s' "$st" | grep -q "tl" \
   && printf '%s' "$st" | grep -q "dev"; then
  ok "status 含三个角色"
else
  bad "status 输出异常" "$st"
fi
# 时间戳是用户判断 idle/ready 时刻的唯一依据，必须真的在输出里
if printf '%s' "$st" | grep -qE '[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}'; then
  ok "status 头部带当前时间戳"
else
  bad "status 缺当前时间戳" "$st"
fi
if printf '%s' "$st" | grep -q "最后活动"; then
  ok "status 含「最后活动」时刻列"
else
  bad "status 缺「最后活动」列" "$st"
fi
printf '%s\n' "$st" | sed 's/^/     | /'

# ---------------------------------------------------------------- 6. 巡检一轮
step 6 "watch once：跑一轮巡检不炸"
if "$BIN" --project "$WORK" --workspace "$WS" watch once >/dev/null 2>&1; then
  ok "巡检单轮执行成功"
else
  bad "巡检单轮失败"
fi
[ -f .xteam/watch/idle.state ] && ok "计时状态落盘" || bad "计时状态没落盘"
# 巡检必须能自愈：kill -9 掉 run，supervise 应在几秒内拉起新的
# （watch once 不写 pidfile，只有 start/run 才写，所以先 start）
if true; then
  "$BIN" --project "$WORK" --workspace "$WS" watch start >/dev/null 2>&1
  sleep 3
  [ -f .xteam/watch/watch.pid ] && ok "pidfile 在项目内（不是 /tmp 全局）" \
    || bad "pidfile 缺失或位置不对"
  R1=""; R2=""
  R1=$(cat .xteam/watch/watch.pid 2>/dev/null || true)
  if [ -n "$R1" ] && kill -0 "$R1" 2>/dev/null; then
    kill -9 "$R1" 2>/dev/null
    sleep 8
    R2=$(cat .xteam/watch/watch.pid 2>/dev/null)
    if [ -n "$R2" ] && [ "$R2" != "$R1" ] && kill -0 "$R2" 2>/dev/null; then
      ok "run 被 kill -9 后自动复活 (pid ${R1} -> ${R2})"
    else
      bad "run 被 kill 后没复活" "R1=${R1:-无} R2=${R2:-无}"
    fi
  else
    bad "run 进程没起来，无法验证自愈"
  fi
fi
[ -f .xteam/watch/events.log ] && ok "事件日志存在" || bad "事件日志缺失"
# 每轮巡检都要落一行采样：只有「出事才写日志」的话，
# 「巡检还在跑」和「巡检已死」在日志里长得一样
if grep -q "SAMPLE" .xteam/watch/events.log 2>/dev/null; then
  ok "巡检每轮落采样行"
else
  bad "巡检没落采样行（分不清「没出事」和「巡检已死」）" \
      "$(tail -3 .xteam/watch/events.log 2>/dev/null)"
fi

# ---------------------------------------------------------------- 7. stamp 时间线
step 7 "stamp 写入共享时间线，WAITING 标记可读"
"$BIN" --project "$WORK" --workspace "$WS" stamp dev "smoke 打点" >/dev/null 2>&1
"$BIN" --project "$WORK" --workspace "$WS" stamp pm "smoke 等人类" --waiting --wait-for "冒烟收尾" >/dev/null 2>&1
if [ -f .xteam/watch/timeline.log ] && grep -q "WAITING" .xteam/watch/timeline.log; then
  ok "时间线落盘且 WAITING 标记可读（巡检据此免打扰）"
else
  bad "时间线没落盘或缺 WAITING 标记" "$(cat .xteam/watch/timeline.log 2>/dev/null)"
fi
if grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' .xteam/watch/timeline.log 2>/dev/null; then
  ok "时间线每行以时间戳开头"
else
  bad "时间线行首缺时间戳"
fi

step 8 "手工推进一轮协议，status 的义务要跟着变"
mkdir -p .xteam/tasks/smoke
printf '# spec\n' > .xteam/tasks/smoke/spec.md
d1=$("$BIN" --project "$WORK" --workspace "$WS" status 2>&1 | grep -c "version-spec")
[ "$d1" -ge 1 ] && ok "写了 spec → PM 欠 version-spec（共识阶段）" \
  || bad "spec 未产生 version-spec 义务"

# 共识阶段：spec 有版本号后 TL 欠评估，不是直接拆解
printf '{"round":1}' > .xteam/tasks/smoke/spec.json
d1b=$("$BIN" --project "$WORK" --workspace "$WS" status 2>&1 | grep -c "assess")
[ "$d1b" -ge 1 ] && ok "spec v1 → TL 欠 assess（必须回评估）" \
  || bad "spec v1 未产生 assess 义务"

printf '# request\n' > .xteam/tasks/smoke/request.md
d2=$("$BIN" --project "$WORK" --workspace "$WS" status 2>&1 | grep -c "implement")
[ "$d2" -ge 1 ] && ok "写了 request → dev 欠 implement" || bad "request 未产生 implement 义务"

printf '{"round":1,"head":"x","verification":[]}' > .xteam/tasks/smoke/delivered.json
d3=$("$BIN" --project "$WORK" --workspace "$WS" status 2>&1 | grep -c "chase")
[ "$d3" -ge 1 ] && ok "已交付 → TL 欠 chase" || bad "交付未产生 chase 义务"

printf 'done\n' > .xteam/tasks/smoke/closed.md
# **next-slice 的前提是队列里还有未开工项**（4d52a83 起：队列空时这条义务让位给
# 「结项 report」，因为「还有下一片要做」和「全做完了」是两种状态）。不补这一行，
# 这片闭合后 PM 欠的是 report —— 而这一步验的是「闭合后别停」。
printf '| 1 | next-one | todo | |\n' >> .xteam/QUEUE.md
d4=$("$BIN" --project "$WORK" --workspace "$WS" status 2>&1 | grep -c "next-slice")
[ "$d4" -ge 1 ] && ok "已闭合 + 队列有未开工项 → PM 欠 next-slice" \
  || bad "闭合未产生 next-slice 义务"

# 队列也被清空时，闭合片不该再催 next-slice —— 改由 report 义务接手（结项）。
grep -v 'next-one' .xteam/QUEUE.md > .xteam/QUEUE.md.tmp || true
mv .xteam/QUEUE.md.tmp .xteam/QUEUE.md
d4b=$("$BIN" --project "$WORK" --workspace "$WS" status 2>&1 | grep -c "report")
[ "$d4b" -ge 1 ] && ok "队列清空 → PM 改欠结项 report" \
  || bad "队列清空却没人欠结项 report"

# 补回队列项，后面的步骤仍按「有下一片」的状态跑
printf '| 1 | next-one | todo | |\n' >> .xteam/QUEUE.md

# ------------------------------------------------- 生命周期：up 复用 / down 只关 pane
step "生命周期" "down 只关 pane 保留 workspace；再 up 复用同一个"
if d_out=$("$BIN" --project "$WORK" --workspace "$WS" down 2>&1); then
  wsid2=$(herdr workspace list 2>/dev/null | python3 -c "
import json,sys
rows=json.load(sys.stdin)['result']['workspaces']
print(next((x['workspace_id'] for x in rows if x.get('label')=='$WS'), 'MISSING'))")
  if [ "$wsid2" = "$WSID" ]; then
    ok "down 之后 workspace 还在，可复用: ${wsid2}"
  else
    bad "down 把 workspace 关了" "期望保留 $WSID ，实际 $wsid2 "
  fi
  if printf '%s' "$d_out" | grep -q "保留着"; then
    ok "down 明确说了 workspace 保留"
  else
    bad "down 没说明 workspace 被保留" "$d_out"
  fi
  if printf '%s' "$d_out" | grep -qE "关闭 [0-9]+ 个角色 pane"; then
    ok "down 确实关了角色 pane"
  else
    bad "down 没关 pane" "$d_out"
  fi
  st_out=$("$BIN" --project "$WORK" --workspace "$WS" status 2>&1)
  if printf '%s' "$st_out" | grep -q "没有 agent 在跑"; then
    ok "down 后 status 如实说「pane 已关」"
  else
    bad "down 后 status 仍报运行中" "$st_out"
  fi
  if u_out=$("$BIN" --project "$WORK" --workspace "$WS" up "$WORK" 2>&1); then
    if printf '%s' "$u_out" | grep -q "复用，不新建"; then
      ok "再次 up 复用了同一个 workspace"
    else
      bad "再次 up 另开了 workspace" "$u_out"
    fi
  else
    bad "down 之后 up 不回来" "$u_out"
  fi
else
  bad "down 失败" "$d_out"
fi

# ---------------------------------------------------------------- 收尾
finish
