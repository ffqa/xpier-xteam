#!/usr/bin/env ruby
# test_formula.rb — Formula 模板的运行时守卫（v2：结构性禁止）。
#
# 为什么需要它：release.yml 里的 `ruby -c` 只管语法，运行期才炸的东西它看不见——
# F-13 的 #{tap_and_name} NameError 是一类；F-15 的是另一类：单字符串
# `system("command -v python3 …")` 在 `brew ruby` 下成立、在 Formula 上下文却走
# Kernel#exec 不经 shell → 同一行结果不同 → 「明明有 python3 却报找不到」。
# 这类 bug 只有「单字符串 system 一律算违规」的结构断言才能通杀。
#
# 用法：ruby tests/test_formula.rb [模板或已渲染的 formula 路径]
#   无参：验仓库里的 .github/formula/xteam.rb.template（本地一条命令）
#   有参：验任意模板/成品——AC-3 拿修复前版本复核用
#         `git show <base>:.github/formula/xteam.rb.template` 落临时文件再跑。
#
# 替身面（模板用到的 DSL 子集）：
#   类方法  desc/homepage/url/sha256/license/version/depends_on/test/caveats
#   实例    opoo/odie/system/prefix/full_name + Open3.capture2e
# `system` 记调用形式：单字符串 → 记违规并返回 false；argv 多参 → true。
# `Open3.capture2e` 按布景 $check_ok/$check_out 返回（install.sh --check 的替身）。

require "stringio"
require "open3"

TPL = ARGV[0] ||
  File.expand_path("../.github/formula/xteam.rb.template", __dir__)
# spec §2.1 MP-23.4 钉的**整行**（不是子串）：插值渲染后必须一字不差。
EXACT = "· 装完重跑：brew install ffqa/tap/xteam"

class OdieFatal < StandardError; end   # 区分「odie 致命」与普通异常

class FakeStatus
  def initialize(ok); @ok = ok; end
  def success?;       @ok;      end
end

# ---- 布景注入点（每个场景前重置/赋值） ----
$check_ok     = true    # install.sh --check 体检结果
$check_out    = ""      # 体检输出（会进 odie 消息）
$formula_name = "ffqa/tap/xteam"
$sys_calls    = []      # system 的调用记录（argv 数组）
$sys_one      = []      # 单字符串调用 = 结构性违规（跨布景累计）
$cap_calls    = []      # Open3.capture2e 的调用记录

Open3.define_singleton_method(:capture2e) do |*args|
  $cap_calls << args
  [$check_out, FakeStatus.new($check_ok)]
end

class Formula
  # ---- 类级 DSL：收录即忽略（不产生 install 期行为） ----
  def self.desc(*);       end
  def self.homepage(*);   end
  def self.url(*);        end
  def self.sha256(*);     end
  def self.license(*);    end
  def self.version(*);    end
  def self.depends_on(*); end
  def self.test(&blk);    end  # `test do ... end` 只注册，块不在 install 里跑

  # ---- 实例面：install 实际用到的那几个 ----
  def opoo(msg);  puts msg;                        end
  def odie(msg);  puts msg; raise OdieFatal, msg;  end
  def system(*args)
    $sys_calls << args
    if args.length == 1 && args.first.is_a?(String)
      $sys_one << args.first          # 单字符串 = 依赖 shell 语义 = 违规
      return false
    end
    true                              # argv 多参 = 不经 shell
  end
  def prefix;     "/tmp/xteam-prefix"; end
  def full_name;  $formula_name;       end
end

src = File.read(TPL)
  .gsub("@VERSION@", "0.0.0")
  .gsub("@SHA256@", "0" * 64)
  .gsub("@NAME@", "xteam-0.0.0")
eval(src)                     # 定义 class Xteam < Formula

def run_install
  $sys_calls = []
  $cap_calls = []
  out = StringIO.new
  err = nil
  begin
    $stdout = out
    Xteam.new.install
  rescue Exception => e         # NameError 走 StandardError 之外也要抓
    err = e
  ensure
    $stdout = STDOUT
  end
  [out.string, err]
end

$fails = []
def check(name, ok, detail = nil)
  puts(ok ? "  ✓ #{name}" : "  ✗ #{name} — #{detail}")
  $fails << name unless ok
end

exact_line = ->(msg) { msg.lines.any? { |l| l.chomp == EXACT } }

# MP-23.2：install.sh --check 成功（有 python3）→ 不阻断，照常装。
$check_ok = true
$check_out = "xteam 安装体检\n  python3 3.12.2 ✓\n"
_o, e = run_install
check("MP-23.2 --check 成功不阻断（无异常）", e.nil?, e && "#{e.class}: #{e.message}")
check("MP-23.2 以 argv 委托 install.sh --check",
      $cap_calls.any? { |a| a.include?("install.sh") && a.include?("--check") },
      $cap_calls.inspect)
check("MP-23.2 体检过后照常调 install.sh 安装",
      $sys_calls.any? { |a| a.include?("install.sh") && a.include?("--prefix") },
      $sys_calls.inspect)

# MP-23.3：--check 失败（无 python3）→ odie 致命 + 消息可执行。
$check_ok = false
$check_out = "install: 找不到 python3（需要 3.9+）。若用 Homebrew 版本化 Python：" \
             " export PATH=\"$(brew --prefix python@3.12)/libexec/bin:$PATH\"\n"
_o, e = run_install
check("MP-23.3 以 odie 致命（不是静默 return）", e.is_a?(OdieFatal), e && e.class)
msg = e ? e.message : ""
check("MP-23.3 消息含 python3", msg.include?("python3"))
check("MP-23.3 消息含修复指引（brew/install.sh）",
      msg.include?("brew install python@3.12") && msg.include?("install.sh"))
check("MP-23.3 不是 Empty installation", !msg.include?("Empty installation"))

# MP-23.4：文案整行钉死 + 错名变体必红（队列序 16 并入）。
check("MP-23.4 提示整行逐字", exact_line.(msg), msg.inspect)
$formula_name = "ffqa/tap/xteamX"          # 错名变体：渲染出行尾带 X
_o2, e_mut = run_install
check("MP-23.4 错名变体必红（整行不再命中）",
      !(e_mut && exact_line.(e_mut.message)),
      e_mut && e_mut.message.inspect)
$formula_name = "ffqa/tap/xteam"

# MP-23.1：结构性禁止——上面三个布景跑下来，公式零单字符串 system 调用。
check("MP-23.1 零单字符串 system 调用（结构性禁止）",
      $sys_one.empty?, $sys_one.inspect)

# MP-23.5 回归：MP-12~MP-21 由 tests/e2e_multi.sh 末行计数兜住（此处不减断言）。

if $fails.empty?
  puts "RESULT: 五布景全过（结构性禁止生效）✓"
  exit 0
else
  puts "RESULT: #{$fails.length} 条不过：#{$fails.join('；')}"
  puts "  单字符串 system 违规：#{$sys_one.inspect}" unless $sys_one.empty?
  exit 1
end
