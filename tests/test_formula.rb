#!/usr/bin/env ruby
# test_formula.rb — Formula 模板的运行时守卫。
#
# 为什么需要它：release.yml 里的 `ruby -c` 只管语法，而 `install` 里插值一个
# 不存在的方法（#{tap_and_name}）是**运行期**才炸的 NameError —— 于是出现过
# 「CI 全绿 + 用户 brew install 拿到 Ruby 异常」：本该救人的「找不到 python3」
# 分支自己先炸。这个守卫用替身 Formula 类把渲染后的公式真正跑一遍那条分支。
#
# 用法：ruby tests/test_formula.rb [模板或已渲染的 formula 路径]
#   无参：验仓库里的 .github/formula/xteam.rb.template（本地一条命令）
#   有参：验任意模板/成品——AC-3 拿修复前版本复核用
#         `git show <base>:.github/formula/xteam.rb.template` 落临时文件再跑。
#
# 替身 Formula 只提供模板用到的 API 面（Homebrew 的 DSL 子集）：
#   类方法  desc/homepage/url/sha256/license/version/depends_on/test/caveats
#   实例    opoo/odie/system/prefix/full_name
# `system` 恒返回 false —— 模拟「机器上没有 python3」，那正是唯一会走到
# 提示分支的场景（也是 F-13 用户真实踩中的路径）。

require "stringio"

TPL = ARGV[0] ||
  File.expand_path("../.github/formula/xteam.rb.template", __dir__)
WANT = "brew install ffqa/tap/xteam"

class Formula
  # ---- 类级 DSL：收录即忽略（它们不产生 install 期行为） ----
  def self.desc(*);       end
  def self.homepage(*);   end
  def self.url(*);        end
  def self.sha256(*);     end
  def self.license(*);    end
  def self.version(*);    end
  def self.depends_on(*); end
  def self.test(&blk);    end  # `test do ... end` 只注册，块不在 install 里跑

  # ---- 实例面：install 实际用到的那几个 ----
  def opoo(msg);  puts msg;             end
  def odie(msg);  raise msg;            end
  def system(*);  false;                end  # 恒 false → 必走「无 python3」分支
  def prefix;     "/tmp/xteam-prefix";  end
  def full_name;  "ffqa/tap/xteam";     end
end

src = File.read(TPL)
  .gsub("@VERSION@", "0.0.0")
  .gsub("@SHA256@", "0" * 64)
  .gsub("@NAME@", "xteam-0.0.0")
eval(src)                     # 定义 class Xteam < Formula

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

if err
  puts "RESULT: #{err.class}: #{err.message}"
  exit 1
end
unless out.string.include?(WANT)
  puts "RESULT: 未抛异常，但输出缺「#{WANT}」："
  puts out.string
  exit 1
end
puts "RESULT: 未抛异常，输出含 #{WANT} ✓"
