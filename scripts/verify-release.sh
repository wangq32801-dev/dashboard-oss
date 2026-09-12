#!/usr/bin/env bash
# Single local release gate. Read-only: it never writes business data.
# Usage: DASH_URL=http://127.0.0.1:8787 bash scripts/verify-release.sh
#
# ZC-QA-1.2 退出码三态：
#   0 = 全部门禁通过（含浏览器门禁）
#   1 = 产品失败（任一门禁失败 —— 禁止发布）
#   2 = 无产品失败，但存在环境阻塞（如沙箱不允许 bind / 浏览器可执行缺失 /
#       DASH_URL 不可达）。此码下不得宣称「浏览器已验证/全绿」。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
FAIL=0
PASS=0
SKIP=0
ENV_BLOCK=0
ok(){ printf '  ✓ %s\n' "$*"; PASS=$((PASS+1)); }
fail(){ printf '  ✗ %s\n' "$*" >&2; FAIL=$((FAIL+1)); }
skip(){ printf '  · %s（跳过）\n' "$*"; SKIP=$((SKIP+1)); }
env_block(){ printf '  ⚠ %s（环境阻塞，未验证）\n' "$*" >&2; ENV_BLOCK=$((ENV_BLOCK+1)); }

echo "驾驶舱发布前统一验收"
echo "根目录：$ROOT_DIR"
echo "环境：$(uname -s) $(uname -m) · python3 $(python3 --version 2>&1 | awk '{print $2}') · node $(node --version 2>/dev/null || echo '缺失')"
echo "时间：$(date '+%Y-%m-%d %H:%M:%S %z')"

echo "── 1. Python 语法与回归 ──"
if python3 -c "compile(open('dashboard-server.py', encoding='utf-8').read(),'dashboard-server.py','exec')"; then ok "dashboard-server.py compile"; else fail "dashboard-server.py compile"; fi
if python3 -m unittest discover -s tests -p 'test_*.py'; then ok "Python tests"; else fail "Python tests"; fi

echo "── 2. JavaScript 语法 ──"
MAIN_JS=$(mktemp "${TMPDIR:-/tmp}/dashboard-main-check.XXXXXX")
trap 'rm -f "$MAIN_JS"' EXIT
if python3 - "$MAIN_JS" <<'PY'
import re, sys
html = open('hermes-dashboard.html', encoding='utf-8').read()
scripts = re.findall(r'<script(?![^>]*application/json)[^>]*>(.*?)</script>', html, re.S)
if not scripts: raise SystemExit('no executable script block')
open(sys.argv[1], 'w', encoding='utf-8').write(max(scripts, key=len))
PY
then
  if node --check < "$MAIN_JS"; then ok "main HTML script"; else fail "main HTML script"; fi
else
  fail "extract main HTML script"
fi
for module in frontend-modules/*.js; do
  if node --check "$module" >/dev/null; then ok "$(basename "$module")"; else fail "$(basename "$module")"; fi
done

echo "── 3. HTML/CSS 结构与敏感信息 ──"
if python3 - <<'PY'
import re
html = open('hermes-dashboard.html', encoding='utf-8').read()
body = re.sub(r'<!--.*?-->', '', html, flags=re.S)
body = re.sub(r'<script\b.*?</script>', '', body, flags=re.S)
body = re.sub(r'<style\b.*?</style>', '', body, flags=re.S)
for tag in ('div', 'section', 'header', 'footer', 'main', 'aside'):
    tokens = re.findall(r'<(%s)\b[^>]*>|</%s>' % (tag, tag), body)
    opens = sum(1 for token in tokens if token == tag)
    closes = len(tokens) - opens
    if opens != closes:
        raise SystemExit(f'{tag} imbalance: open={opens} close={closes}')
PY
then
  ok "HTML structure"
else
  fail "HTML structure"
fi
if (cd tests && python3 -m unittest test_api_contracts test_release_contract); then ok "API/release contract tests"; else fail "API/release contract tests"; fi
if PYTHONDONTWRITEBYTECODE=1 python3 scripts/recovery-drill.py; then ok "temporary 9-domain recovery drill"; else fail "temporary 9-domain recovery drill"; fi
if PYTHONDONTWRITEBYTECODE=1 python3 scripts/mirror-rebuild-drill.py; then ok "temporary Obsidian mirror rebuild drill"; else fail "temporary Obsidian mirror rebuild drill"; fi
if git diff --check; then ok "git diff --check"; else fail "git diff --check"; fi

echo "── 4. Node 写入契约 ──"
if node tests/task-mutations.test.mjs; then ok "task mutation contract"; else fail "task mutation contract"; fi
if node tests/write-coordinator.test.mjs; then ok "write coordinator contract"; else fail "write coordinator contract"; fi
if node tests/view-lifecycle.test.mjs; then ok "view lifecycle 50-switch contract"; else fail "view lifecycle 50-switch contract"; fi
if node tests/today-view.test.mjs; then ok "today execution selection contract"; else fail "today execution selection contract"; fi

echo "── 5. 折叠屏、交互行为与启动性能 ──"
if [ "${SKIP_BROWSER:-0}" = "1" ]; then
  skip "browser gates requested skip (SKIP_BROWSER=1)"
else
  # 环境预检：DASH_URL 可达性（服务未起属环境阻塞，浏览器门禁依赖它；
  # Python/Node 静态门禁不依赖，仍已真实执行）。
  DASH_URL="${DASH_URL:-http://127.0.0.1:8787}"
  SERVER_OK=1
  python3 - "$DASH_URL" <<'PY' || SERVER_OK=0
import sys, urllib.request
url = sys.argv[1].rstrip('/') + '/api/healthz'
try:
    with urllib.request.urlopen(url, timeout=5) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
  run_browser_gate() {
    local name="$1"; shift
    set +e
    "$@"
    local rc=$?
    set -e
    if [ "$rc" -eq 0 ]; then ok "$name"
    elif [ "$rc" -eq 2 ]; then env_block "$name（浏览器可执行缺失或启动失败）"
    else fail "$name"; fi
  }
  if [ "$SERVER_OK" -ne 1 ]; then
    env_block "foldable smoke（DASH_URL $DASH_URL 不可达：未起服/沙箱不允许 bind）"
    env_block "interactions 行为测试（同上）"
  else
    run_browser_gate "foldable smoke" env DASH_URL="$DASH_URL" node tests/foldable-smoke.mjs
    run_browser_gate "advisor/cmdk 交互行为" env DASH_URL="$DASH_URL" node tests/interactions.test.mjs
  fi
fi

echo "── 6. 结果 ──"
printf '通过：%d；跳过：%d；环境阻塞：%d；失败：%d\n' "$PASS" "$SKIP" "$ENV_BLOCK" "$FAIL"
if [ "$FAIL" -eq 0 ] && [ "$ENV_BLOCK" -eq 0 ] && [ "$SKIP" -eq 0 ]; then
  echo "═══ 发布前验收通过 ═══"; exit 0
fi
if [ "$FAIL" -eq 0 ]; then
  echo "═══ 无产品失败，但存在跳过或环境阻塞项：不得宣称浏览器已验证/全绿 ═══" >&2
  exit 2
fi
echo "═══ 发布前验收失败，禁止发布 ═══" >&2
exit 1
