# xteam —— 本地发版入口。
#
# 真正的打包与发布都在 CI 里（.github/workflows/release.yml）。这里只做
# 「本地能做的前置」：升版本、跑门禁、提交、打 tag、推上去。
# 为什么不把发布逻辑留在本机：发版要求「tag 指向的提交 = 打包的提交 =
# 发布的提交」三者一致，这在 CI 里天然成立；留在本机就多一次「忘了重打包
# 却发了 tag」的机会，而那种失败在用户机器上才暴露。
#
# 用法：
#   make release              # 升 patch、跑门禁、提交、打 tag、推 → CI 接走
#   make release MINOR=1      # 升 minor
#   make release MAJOR=1      # 升 major
#   make patch / minor / major # 只升版本并提交，不推
#   make verify               # 只跑门禁前两层
#   make pack                 # 只打包到 dist/（不升版本、不推）
#   make check                # 门禁前两层 + 打包自证（发版前手动确认用）

SHELL := /bin/bash
.DEFAULT_GOAL := help

VERSION := $(shell tr -d '[:space:]' < VERSION)
REPO    := ffqa/xpier-xteam
TAP     := ffqa/homebrew-tap

# 升版本用 pack.sh --bump，它会同时提交 VERSION。
# 不在 Makefile 里自己算版本号 —— 两处算就一定会有一处算错。
BUMP_FLAG := $(if $(MAJOR),--bump=major,$(if $(MINOR),--bump=minor,$(if $(PATCH),--bump=patch,--bump=patch)))

.PHONY: help release patch minor major verify pack check bump clean

help:
	@echo 'xteam $(VERSION)'
	@echo
	@echo '  make release           升 patch + 门禁 + 提交 + 打 tag + 推（CI 完成发布）'
	@echo '  make release MINOR=1   升 minor'
	@echo '  make release MAJOR=1   升 major'
	@echo '  make patch             只升 patch 版本并提交'
	@echo '  make verify            门禁前两层（静态 + 单测）'
	@echo '  make pack              只打包到 dist/'
	@echo '  make check             门禁前两层 + 打包自证'

# ---------------------------------------------------------------- 门禁
# 端到端层（第 3 层）**不在这里跑**：它要 herdr + 真 agent 起三个 pane，约 110s
# 且依赖本机登录状态。发版前手动跑一次 `bash tests/smoke.sh` 即可。
verify:
	@echo '▸ 门禁前两层（静态 + 单测）'
	@python3 tests/lint_names.py
	@python3 tests/lint_shell.py install.sh tests/smoke.sh pack.sh brew-publish.sh
	@python3 -m py_compile bin/xteam bin/xteam_lib.py tests/test_protocol.py
	@python3 tests/test_protocol.py | tail -2

# ---------------------------------------------------------------- 打包
# 不带 --no-verify：解包 → 装到临时前缀 → 跑门禁前两层，是发版前最便宜的一次
# 「这个包到底能不能用」验证。有 herdr 时还会跑完整三层。
pack:
	@echo '▸ 打包（会跑自证验证）'
	@bash pack.sh

check: verify pack

# ---------------------------------------------------------------- 升版本
bump:
	@bash pack.sh $(BUMP_FLAG)

patch:
	@bash pack.sh --bump=patch

minor:
	@bash pack.sh --bump=minor

major:
	@bash pack.sh --bump=major

# ---------------------------------------------------------------- 发版
# 顺序是有讲究的：
#   1. 先 verify —— 版本已经升了、也提交了，才发现代码有问题就很难看
#      （要回退得再改一次 VERSION）
#   2. 再 pack —— 用刚升完版本的干净工作区打出自证包
#   3. 最后才提交剩余改动 + 打 tag + 推
release: verify
	@echo
	@echo '▸ 升版本并提交'
	@bash pack.sh $(BUMP_FLAG)
	@echo
	@echo '▸ 提交剩余改动（如果有）'
	@bash -c 'set -euo pipefail; \
	  if [ -z "$$(git status --porcelain)" ]; then \
	    echo "  工作区干净"; \
	  else \
	    echo "  还有未提交改动，一并提交："; git status --short; \
	    git add -A; git commit -q -m "release: $$(tr -d "[:space:]" < VERSION)"; \
	  fi'
	@echo
	@echo '▸ 打包自证（确认这个包真能用）'
	@bash pack.sh
	@echo
	@bash -c 'set -euo pipefail; \
	  NEWV=$$(tr -d "[:space:]" < VERSION); \
	  if [ -z "$$NEWV" ]; then echo "VERSION 是空的" >&2; exit 1; fi; \
	  if ! grep -qE "^[0-9]+\.[0-9]+\.[0-9]+$$" <<<"$$NEWV"; then \
	    echo "VERSION 不是 X.Y.Z 形式：$$NEWV" >&2; exit 1; fi; \
	  echo "▸ 打 tag 并推送"; \
	  git tag -a "xteam-v$$NEWV" -m "release $$NEWV"; \
	  git push origin main; \
	  git push origin "xteam-v$$NEWV"; \
	  echo; echo "✓ tag xteam-v$$NEWV 已推送"; \
	  echo "  CI 会接着：核对 tag 与 VERSION → 打包 → 建 Release → 更新 $(TAP)"; \
	  echo "  看进度：gh run watch  （或 gh run list）"; \
	  echo; echo "  发完后："; \
	  echo "    brew tap ffqa/tap"; \
	  echo "    brew install ffqa/tap/xteam"'

clean:
	@rm -rf dist
	@echo '已清理 dist/'
