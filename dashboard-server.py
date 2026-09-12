#!/usr/bin/env python3
"""
个人驾驶舱 · 本地后端服务（开源版）
静态文件 + 角色/任务/习惯 CRUD API + 仪表盘数据聚合
数据源: TickTick MCP（任务/习惯实时） + DASH_DATA_DIR 下的 Markdown/JSON
"""
import base64, hashlib, json, math, os, re, socket, subprocess, sys, threading, time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import urllib.request

from health_ingest import scan_ingest
from ai_prompts import prompt_header, prompt_meta
from ai_runtime import build_evidence, record_call, usage_summary

DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
# ─── 数据根目录（开源版）──────────────────────────────────────────
# 所有用户数据（JSON 状态、演示数据、恢复快照）都收敛到这一个目录，默认为
# 仓库下的 data/（git 忽略）。想放到别处设 DASH_DATA_DIR=/path 即可。
# 磁盘上的角色/复盘/镜像 Markdown 同样写入该目录，想用 Obsidian 管理时
# 直接把 DASH_DATA_DIR 指向你的 vault 子目录。
DASH_DATA_DIR = os.environ.get("DASH_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
if os.environ.get("DASH_DEMO") == "1":
    import tempfile
    _demo_storage = tempfile.TemporaryDirectory(prefix="dashboard-demo-")
    DASH_DATA_DIR = _demo_storage.name
    os.environ["DASH_DATA_DIR"] = DASH_DATA_DIR
    for _key in list(os.environ):
        if _key.startswith("DASH_") and (_key.endswith("_FILE") or _key.endswith("_MD") or _key in ("DASH_ROLES_DIR", "DASH_RECOVERY_BACKUP_DIR")):
            os.environ.pop(_key, None)
os.makedirs(DASH_DATA_DIR, exist_ok=True)

def _dd(*parts):
    """数据目录内的绝对路径。"""
    return os.path.join(DASH_DATA_DIR, *parts)

def _dd_expanduser_default(env_key, *parts):
    """允许环境变量覆盖；默认落到数据目录（不再写用户 home 根）。"""
    return os.path.expanduser(os.environ.get(env_key, "")) if os.environ.get(env_key) else _dd(*parts)

# ─── 演示模式（开源版）──────────────────────────────────────────
# 无 TickTick / LLM / 健康数据源时的开箱体验：DASH_DEMO=1 强制开启；
# 默认在「未配置 TickTick token 且没有任何用户角色数据」时自动进入。
DEMO_MODE = os.environ.get("DASH_DEMO", "") == "1"
_demo_payload_cache = {"payload": None}

def _demo_enabled():
    global DEMO_MODE
    if os.environ.get("DASH_DEMO", "") == "1":
        return True
    if os.environ.get("DASH_DEMO", "") == "0":
        return False
    if DEMO_MODE:
        return True
    # 自动判定：无 token 且角色目录为空 → 演示
    if TICKTICK_TOKEN:
        return False
    try:
        return not (os.path.isdir(OBSIDIAN_ROLES_DIR) and any(
            f.endswith(".md") for f in os.listdir(OBSIDIAN_ROLES_DIR)))
    except Exception:
        return True

def _demo():
    """惰性构建演示 payload（进程内缓存；按天失效保证日期相对数据新鲜）。"""
    from datetime import date as _date
    key = _date.today().isoformat()
    if _demo_payload_cache.get("key") != key:
        try:
            from demo_data import build_demo_payload
            _demo_payload_cache["payload"] = build_demo_payload(_date.today())
            _demo_payload_cache["key"] = key
        except Exception as e:
            print("[demo] 演示数据构建失败: %r" % e, flush=True)
            _demo_payload_cache["payload"] = None
    return _demo_payload_cache.get("payload") or {}

OBSIDIAN_ROLES_DIR = _dd_expanduser_default("DASH_ROLES_DIR", "角色")
PORT = 8787  # 命令行端口只在 __main__ 入口解析，保证模块可被测试和复用


# ── 对外错误契约（ZC-QA-1 重设计）──
# 原则：客户端只拿「稳定错误码 + 固定通用文案」；异常详情仅进服务端日志（stderr → journal）。
# _scrub_client_text 是纵深防御层：任何将要下发的半动态文本都必须经过它，
# 清理控制字符 / 绝对路径(/etc /var /opt 等) / URL 查询参数 / 认证凭据特征。
_CLIENT_ERROR_TEXTS = {
    "internal_error": "服务器内部错误，请稍后重试",
    "upstream_error": "上游服务暂时不可用，请稍后重试",
}


def _scrub_client_text(text, limit=300):
    if not isinstance(text, str):
        text = str(text) if text is not None else ""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    for home in (os.path.expanduser("~"), "/home/ubuntu", "/root"):
        if home and home != "/":
            text = text.replace(home, "~")
    text = re.sub(r"/(?:etc|var|opt|usr|private|tmp|Library)/[^\s'\"<>）)】，]*", "[路径]", text)
    text = re.sub(r"\?[^\s'\"<>）)】，]*", "?[查询参数]", text)
    text = re.sub(r"(?i)(bearer\s+)[^\s'\"<>，。）)]+", "\\1[已脱敏]", text)
    text = re.sub(r"(?i)((?:token|password|passwd|secret|authorization|api[_-]?key)\s*[=:]\s*)[^\s'\"<>，。）)]+", "\\1[已脱敏]", text)
    return text[:limit]


def _log_exc(exc, code, context=""):
    """详细异常只进服务端日志；绝不进入任何客户端响应。"""
    try:
        try:
            detail = repr(exc)
        except Exception:
            detail = "<unreprable exception>"
        print("[exc] code=%s%s %s" % (code, (" " + context) if context else "", _scrub_client_text(detail, 500)), flush=True)
    except Exception:
        pass


def _client_error(exc, code="internal_error", context=""):
    """返回对外错误对象：稳定 code + 固定通用文案。异常详情只记服务端日志。"""
    _log_exc(exc, code, context)
    return {"error": _CLIENT_ERROR_TEXTS.get(code) or _CLIENT_ERROR_TEXTS["internal_error"], "code": code}

TICKTICK_URL = "https://mcp.dida365.com"


# 静态资源 MIME 映射（前端资源 + 装饰图）
MIME_MAP = {
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif',
    '.svg': 'image/svg+xml', '.ico': 'image/x-icon', '.webp': 'image/webp',
    '.js': 'application/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
    '.webmanifest': 'application/manifest+json; charset=utf-8',
    '.html': 'text/html', '.htm': 'text/html', '.md': 'text/markdown',
    '.json': 'application/json', '.xml': 'application/xml', '.txt': 'text/plain',
}
# P2-S4: 主静态路由只服务前端必需资源，收窄暴露面。
# 排除 .md/.json/.xml/.txt —— 这些会把工作目录里的内部文档/数据文件原样下发（虽然路径穿越已拦截、
# 即使外层有认证也不应暴露）。
STATIC_EXTS = (
    '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.webp',
    '.js', '.css', '.webmanifest', '.html', '.htm',
)

# ZC 新视角审计：静态服务白名单——仓库内静态文件不再默认全部可下载。
# 允许集合 = 页面/manifest/SW 实测引用的精确文件 + 前端模块目录 + img/ 资源
# （CSS url() 引用均在 /img/ 前缀下）；历史工作产物（_sw_work2.js、_kb_work2.html、
# benchmark-research.html、guide.js 等）与测试产物一律 404。
# 非 STATIC_EXTS 路径不受影响（仍走「兜底返回驾驶舱页」的 PWA 路由约定）。
STATIC_ALLOW_EXACT = frozenset({
    "/hermes-dashboard.html", "/munger.html", "/portal.html",
    "/manifest.webmanifest", "/sw.js", "/chart.umd.min.js", "/quotes.js",
    "/moon-crescent.png", "/icon-192.png", "/icon-512.png",
    "/icon-512-maskable.png", "/icon-1024.png",
})


def _static_allowed(path):
    # 先规范化再判定（ZC 审计：/frontend-modules/../verify.js 形式的 .. 段
    # 可绕过前缀白名单读到仓库根文件）；含 .. 段一律拒绝。
    # %2f 等编码分隔符先 unquote 再判定，防编码变体绕过。
    from urllib.parse import unquote as _uq
    import posixpath as _pp
    norm = _pp.normpath("/" + "/".join(seg for seg in _uq(path).split("/") if seg not in ("", ".")))
    if norm.endswith("/"):
        norm = norm.rstrip("/")
    if ".." in norm.split("/"):
        return False
    if norm in STATIC_ALLOW_EXACT:
        return True
    if norm.startswith("/frontend-modules/") and norm.endswith(".js"):
        return True
    if norm.startswith("/img/"):
        return True
    return False

ON_VPS = os.path.exists(os.path.expanduser("~/.dash_path"))

# ZC 新视角审计：kb_diag.jsonl 可注入 + 大小上限（默认 2MB，超限轮转 .old 保留一代），
# 测试通过替换常量隔离到临时目录；此前为无界追加。
KB_DIAG_FILE = _dd_expanduser_default("DASH_KB_DIAG_FILE", "kb_diag.jsonl")
KB_DIAG_MAX_BYTES = int(os.environ.get("DASH_KB_DIAG_MAX_BYTES", str(2 * 1024 * 1024)))

# HEALTH-ABC-D1: HealthKit 累计型指标（cumulative quantity）——HAE 实时推送是「当日累计快照」，
# 聚合时同天取最大值（≈全天真实值），避免被次日凌晨推送的「前日残留」后读覆盖污染。
CUMULATIVE_METRICS = {"active_energy", "step_count", "walking_running_distance",
                      "apple_exercise_time", "apple_stand_time", "basal_energy_burned",
                      "flights_climbed", "cycling_distance"}
                      # HEALTH-ABC-D1 补全：flights_climbed(爬楼)/cycling_distance(骑行) 同为 HealthKit cumulative，
                      # 同日多次推送是「当日累计快照」，后读覆盖会被后推的偏小快照污染（8-23 flights 08:28推13 → 21:14推1），
                      # 与已有累计指标同规则取最大。

def _load_config():
    """只读取显式配置或项目 config；不继承个人主目录凭据。
    文件未提供的键允许由部署环境变量补充（Docker/compose/systemd/.env.example 契约）；
    DASH_DEMO=1 时配置文件与环境变量凭据一律不读。"""
    if os.environ.get("DASH_DEMO") == "1":
        return {}
    cfg = {}
    # 查找顺序：DASH_CONFIG 环境变量 → 项目目录 ./config
    candidates = [
        os.environ.get("DASH_CONFIG", ""),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "config"),
    ]
    path = next((p for p in candidates if p and os.path.isfile(p)), "")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    # 部署环境变量兜底：仅当文件未提供该键时才从进程环境读取。
    for key in ("TICKTICK_TOKEN", "PROJECT_IDS", "LLM_API_BASE", "LLM_API_KEY",
                "LLM_MODEL", "LLM_FALLBACK", "USER_NAME", "WECHAT_DELIVER",
                "VPS_DASH_URL", "VPS_AUTH_USER", "VPS_AUTH_PASS",
                "HEALTH_PUSH_PUBLIC_URL"):
        if not cfg.get(key):
            value = os.environ.get(key, "")
            if value:
                cfg[key] = value
    # 兼容别名：LLM_MODEL_FALLBACK → LLM_FALLBACK
    if "LLM_FALLBACK" not in cfg and "LLM_MODEL_FALLBACK" in cfg:
        cfg["LLM_FALLBACK"] = cfg["LLM_MODEL_FALLBACK"]
    return cfg

_cfg = _load_config()
TICKTICK_TOKEN = _cfg.get("TICKTICK_TOKEN", "")
# 可配置的用户称呼：config 文件或部署环境变量的 USER_NAME=xx；默认「用户」。
# 用于 AI 提示词、自动写入文档的落款等。
USER_NAME = _cfg.get("USER_NAME", "").strip() or "用户"

# TickTick 项目名 → 项目 ID 映射：完全由用户配置（config 文件或部署环境变量，
# 格式：PROJECT_IDS=名称A:idA,名称B:idB）。未配置时建任务返回明确错误。
def _load_project_ids():
    raw = _cfg.get("PROJECT_IDS", "").strip()
    mapping = {}
    for part in raw.split(","):
        if ":" in part:
            name, pid = part.split(":", 1)
            name = name.strip()
            if name and pid.strip():
                mapping[name] = pid.strip()
    if not mapping:
        mapping = {"📥 收集箱": "inbox"}  # TickTick MCP 默认收集箱
    return mapping
PROJECT_IDS = _load_project_ids()
_shadow_probe_cache = {"at": 0.0, "data": None}
SHADOW_REPORT_FILE = _dd_expanduser_default("DASH_SHADOW_REPORT_FILE", "shadow_report.json")
SYNC_CONFLICT_FILE = _dd_expanduser_default("DASH_SYNC_CONFLICT_FILE", "sync_conflicts.json")
SYNC_CONFLICT_ACK_FILE = _dd_expanduser_default("DASH_SYNC_CONFLICT_ACK_FILE", "sync_conflict_ack.json")
_shadow_reconcile_lock = threading.Lock()
_shadow_reconcile_last = 0.0
_shadow_reconcile_cooldown = 120

# 第 8 批：只对会触发 LLM/恢复副作用的端点做轻量进程内限流。
# 这是防误触/脚本重放的保险丝，不替代 nginx/Cloudflare 的公网防护；
# 普通读接口和常规任务 CRUD 不限流，避免单人使用时被自己的操作锁住。
_RATE_LIMITS = {
    "/api/butler/vision": (6, 60),
    "/api/butler/stream": (30, 60),
    "/api/advisor/stream": (30, 60),
    "/api/munger/advisor": (30, 60),
    "/api/ai/draft": (30, 60),
    "/api/ai/oracle/refresh": (10, 60),
    "/api/review/draft/refresh": (10, 60),
    "/api/recovery/restore": (10, 60),
}
_rate_lock = threading.Lock()
_rate_windows = {}

def _rate_limit_retry(path, client):
    """Return retry-after seconds when a bounded expensive route is hot."""
    rule = _RATE_LIMITS.get(path)
    if not rule:
        return 0
    limit, window = rule
    now = time.time()
    key = (str(client or "unknown")[:80], path)
    with _rate_lock:
        # Keep this tiny table self-cleaning; no request data is retained.
        cutoff = now - window * 2
        for old_key, (started, _count) in list(_rate_windows.items()):
            if started < cutoff:
                _rate_windows.pop(old_key, None)
        started, count = _rate_windows.get(key, (now, 0))
        if now - started >= window:
            started, count = now, 0
        if count >= limit:
            return max(1, int(window - (now - started) + 0.999))
        _rate_windows[key] = (started, count + 1)
    return 0
VPS_DASH_URL = _cfg.get("VPS_DASH_URL", "")
HEALTH_PUSH_PUBLIC_URL = _cfg.get("HEALTH_PUSH_PUBLIC_URL", "https://your-domain.example/health-api/push")
VPS_AUTH_USER = _cfg.get("VPS_AUTH_USER", "")
VPS_AUTH_PASS = _cfg.get("VPS_AUTH_PASS", "")
VPS_AUTH = "Basic " + base64.b64encode(f"{VPS_AUTH_USER}:{VPS_AUTH_PASS}".encode()).decode()

# 数据权威注册表（第 2 批）：把事实源边界集中声明，供运维状态和契约测试读取。
# 这里只放域名/策略，不放实际路径、凭据或业务正文；SQLite 明确是读模型。
DATA_AUTHORITY_REGISTRY = {
    "tasks": {"authority": "TickTick", "writable": True, "revision": "remote-readback"},
    "habits": {"authority": "TickTick", "writable": True, "revision": "remote-readback"},
    "roles": {"authority": "Obsidian Markdown", "writable": True, "revision": "atomic-file"},
    "reviews": {"authority": "Obsidian Markdown", "writable": True, "revision": "atomic-file"},
    "dashboard_json": {"authority": "VPS JSON + Mac merge", "writable": True, "revision": "updated_at/ts"},
    "health_raw": {"authority": "HAE raw JSON", "writable": False, "revision": "receive-time"},
    "ai_outcomes": {"authority": "AI outcomes JSON", "writable": True, "revision": "updated_at"},
    "sqlite_shadow": {"authority": "derived read model", "writable": False, "revision": "source-revision"},
}

def _sync_conflict_id(report_id, item):
    """Stable id for acknowledgement; hash only the bounded, payload-free summary."""
    fields = {k: item.get(k) for k in ("domain", "key", "reason", "left_side", "right_side", "left_ts", "right_ts", "fields") if k in item}
    raw = json.dumps({"report_id": report_id, "item": fields}, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]

# ─── 缓存（30s，写操作时清空） ───
CACHE_TTL = 30
_cache = {}  # key -> (ts, data)

def cached(key, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    data = fn()
    # P2-S7: 负结果（None=失败/无数据）不入缓存，避免瞬时故障被放大为整段 30s 假性空数据。
    # 空 dict/list 是合法「无数据」，照常缓存；只有 None（异常）跳过缓存、下次立即重试。
    if data is not None:
        _cache[key] = (time.time(), data)
    return data

def bust_cache():
    _cache.clear()

# P2-D9: 统一 VPS 代理请求（集中鉴权/超时/错误处理，替代散落的 3 处独立 urllib）。
# path 传相对路径（如 "/api/health?days=14"），自动拼 VPS_DASH_URL + Authorization。
# 返回 (status, body_text)；任何异常（含未配置 VPS 端点）返回 (None, None)。
def _vps_get(path, timeout=6, data=None, method="GET"):
    if not VPS_DASH_URL:
        return None, None
    url = VPS_DASH_URL.rstrip("/") + path
    headers = {"Authorization": VPS_AUTH}
    body = None
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    # 网络请求重试一次并记录失败，避免瞬时抖动导致旧快照长时间未更新，
    # 单次失败即放弃太脆；重试扛偶发抖动，失败原因落 stderr 方便下次定位（不再无痕吞异常）。
    last_err = None
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode()
        except Exception as e:
            last_err = e
            if attempt == 1:
                time.sleep(0.6)
    if last_err is not None:
        try:
            sys.stderr.write("[vps-get-fail] %s -> %s\n" % (path, str(last_err)[:160]))
        except Exception:
            pass
    return None, None

def _f(v):
    """宽松数值解析：None/非法值 → None"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _health_unit(value):
    """Normalize HAE unit labels without guessing a value's magnitude."""
    if isinstance(value, dict):
        value = value.get("units")
    elif not isinstance(value, str):
        # Bare HAE quantities use the field's documented default unit; a
        # numeric value itself is never a unit label.
        return ""
    return re.sub(r"\s+", "", str(value or "")).strip().lower()

def _health_number(value):
    """Read a finite numeric HealthKit quantity; booleans and NaN are invalid."""
    if isinstance(value, dict):
        value = value.get("qty")
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None

def _normalise_health_quantity(name, point):
    """Return a unit-aware HAE quantity for metrics shown on the health page.

    HAE puts units on either the metric object or point.  Core cards need stable
    units, but an unknown unit must remain visible as unverified rather than
    silently receiving a conversion based on a guessed magnitude.
    """
    point = point if isinstance(point, dict) else {}
    qty = _health_number(point.get("qty"))
    unit = _health_unit(point)
    source = "reported"
    out_unit = unit
    if name in {"active_energy", "basal_energy_burned"}:
        if unit in {"kj", "kilojoule", "kilojoules"}:
            qty = round(qty / 4.184, 1) if qty is not None else None
            out_unit = "kcal"
        elif unit in {"kcal", "cal", "kilocalorie", "kilocalories"}:
            out_unit = "kcal"
        elif qty is not None:
            source, out_unit = "unverified", unit or "unknown"
    elif name in {"walking_running_distance", "cycling_distance"}:
        if unit in {"m", "meter", "meters"}:
            qty = round(qty / 1000, 2) if qty is not None else None
            out_unit = "km"
        elif unit in {"km", "kilometer", "kilometers"}:
            out_unit = "km"
        elif unit in {"mi", "mile", "miles"}:
            qty = round(qty * 1.609344, 2) if qty is not None else None
            out_unit = "km"
        elif qty is not None:
            source, out_unit = "unverified", unit or "unknown"
    elif name == "apple_exercise_time":
        if unit in {"s", "sec", "second", "seconds"}:
            qty = round(qty / 60, 1) if qty is not None else None
            out_unit = "min"
        elif unit in {"min", "minute", "minutes"}:
            out_unit = "min"
    return {"qty": qty, "units": out_unit, "source": source}

_file_lock = threading.RLock()  # 可重入：既保护原子写，也覆盖完整 read→modify→write 事务
_memory_lock = threading.Lock()  # 管家长期记忆专用锁：保护 load→modify→write 原子性（避免与 _file_lock 死锁）
_corrupt_json_files = set()  # 解析失败的 JSON 禁止后续以默认空值覆盖
DASH_RECOVERY_BACKUP_DIR = _dd_expanduser_default("DASH_RECOVERY_BACKUP_DIR", "recovery")
DASH_RECOVERY_BACKUP_LIMIT = 12

def _serialized_file_update(fn):
    """把现有文件型 CRUD 的完整读改写序列串行化。"""
    def wrapped(*args, **kwargs):
        with _file_lock:
            return fn(*args, **kwargs)
    wrapped.__name__ = getattr(fn, "__name__", "serialized_file_update")
    wrapped.__doc__ = getattr(fn, "__doc__", None)
    return wrapped

def _safe_role_path(name):
    """校验角色名 → 文件路径，防路径遍历；非法返回 None"""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    base = os.path.realpath(OBSIDIAN_ROLES_DIR)
    fpath = os.path.realpath(os.path.join(OBSIDIAN_ROLES_DIR, name + ".md"))
    if not (fpath == base or fpath.startswith(base + os.sep)):
        return None
    return fpath

def call_ticktick_mcp(tool_name, arguments):
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments}
    }).encode()
    req = urllib.request.Request(TICKTICK_URL, data=body,
        headers={"Content-Type":"application/json","Accept":"application/json","Authorization":f"Bearer {TICKTICK_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            r = json.loads(resp.read())
    except Exception as e:
        out = {"success": False}; out.update(_client_error(e)); return out
    # R1: JSON-RPC 工具执行失败通过 result.isError 表达（如"标题为空"），不是顶层 error 字段；
    # 所有调用方统一按 `"error" in r` 判断成败，此前 isError 未被识别导致失败被当成功入库
    # （实锤：create_task 空标题被 TickTick 拒绝，仍返回 success:true）。这里统一提前到源头识别。
    if isinstance(r, dict):
        top_err = r.get("error")
        if top_err:
            return {"error": top_err.get("message") if isinstance(top_err, dict) else str(top_err)}
        result = r.get("result")
        if isinstance(result, dict) and result.get("isError"):
            content = result.get("content") or []
            text = content[0].get("text") if content and isinstance(content[0], dict) else None
            return {"error": text or "TickTick 工具调用失败"}
    return r

def parse_role_file(path):
    """解析 Obsidian 角色文件，提取 frontmatter + 内容"""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return None
    fm = {}
    if text.startswith("---"):
        end = text.find("---", 3)
        if end > 0:
            for line in text[3:end].strip().split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
    body = text[text.find("---", 3)+3:].strip() if text.startswith("---") else text
    goal = ""
    krs = []
    insights = []
    actions = []
    mission = ""
    review = ""
    review_log = ""
    section = None
    for line in body.split("\n"):
        if line.startswith("## 使命宣言"): section = "mission"
        elif line.startswith("## 季度目标"): section = "goal"
        elif line.startswith("## 关键结果"): section = "krs"
        elif line.startswith("## 对话洞察"): section = "insights"
        elif line.startswith("## 本周行动"): section = "actions"
        elif line.startswith("## 复盘笔记"): section = "review"
        elif line.startswith("## 复盘记录"): section = "review_log"
        elif line.startswith("## "): section = None
        elif section == "mission" and line.strip() and not line.startswith("<!--"):
            mission = line.strip()
        elif section == "goal" and line.strip() and not line.startswith("<!--"):
            goal = line.strip()
        elif section == "krs" and line.strip().startswith("- ["):
            krs.append(line.strip().lstrip("- [ ] ").lstrip("- [x] "))
        elif section == "insights" and line.strip().startswith("- "):
            insights.append(line.strip().lstrip("- "))
        elif section == "actions" and line.strip().startswith("- ["):
            raw = line.strip()
            done = raw.startswith("- [x]") or raw.startswith("- [X]")
            body = raw[5:].strip() if raw.startswith("- [") else raw
            pushed = False
            taskId = ""
            mm = re.search(r"<!--pushed:([^>\s]+)-->", body)
            if mm:
                pushed = True
                taskId = mm.group(1)
                body = body[:mm.start()].rstrip()
            actions.append({"text": body, "done": done, "pushed": pushed, "taskId": taskId})
        elif section == "review" and line.strip() and not line.startswith("<!--"):
            review = line.strip()
        elif section == "review_log" and line.strip().startswith("### "):
            review_log = (review_log + "\n" + line.strip()).strip()
    return {"id": fm.get("role",""), "name": fm.get("role",""), "emoji": fm.get("emoji",""), "goal": goal,
            "mission": mission, "review": review, "review_log": review_log,
            "key_results": krs, "insights": insights, "actions": actions, "color": "#6c5ce7"}

def load_obsidian_roles():
    """从 Obsidian 读取所有角色"""
    roles = []
    if os.path.isdir(OBSIDIAN_ROLES_DIR):
        for fname in sorted(os.listdir(OBSIDIAN_ROLES_DIR)):
            if fname.endswith(".md"):
                r = parse_role_file(os.path.join(OBSIDIAN_ROLES_DIR, fname))
                if r and r["id"]:
                    colors = {"笔杆子":"#6c5ce7","身体":"#00b894","学习者":"#74b9ff","家人":"#e17055","爱人":"#fd79a8","朋友":"#fdcb6e"}
                    r["color"] = colors.get(r["name"], "#8888aa")
                    roles.append(r)
    return roles if roles else None

def _fetch_project_tasks(pid, with_status=False):
    """拉单个项目的未完成任务（供并发调用）"""
    r = call_ticktick_mcp("get_project_with_undone_tasks", {"project_id": pid})
    if "error" in r and r["error"]:
        return None if with_status else []
    result = r.get("result", {})
    items = result.get("structuredContent", {}).get("tasks", [])
    if not items:
        content = result.get("content", [])
        if content and isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    try:
                        sc = json.loads(c["text"])
                        if isinstance(sc, dict) and "tasks" in sc:
                            items = sc["tasks"]
                    except Exception:
                        pass
    out = []
    for t in items:
        out.append({
            "id": t.get("id",""), "pid": t.get("projectId",""),
            "title": t.get("title",""), "priority": t.get("priority",3),
            "due": _due_date_str(t.get("dueDate") or ""),
            "tags": t.get("tags") or [],
            "content": t.get("content") or ""
        })
    return out

def _fetch_completed_project_tasks(pid, days=3650, with_status=False):
    """拉单个项目的已完成任务，供归档已完成条目时做权威回读。"""
    today = date.today()
    start = (today - timedelta(days=days)).isoformat() + "T00:00:00+08:00"
    end = (today + timedelta(days=1)).isoformat() + "T23:59:59+08:00"
    r = call_ticktick_mcp("list_completed_tasks_by_date", {"search": {
        "projectIds": [pid], "startDate": start, "endDate": end}})
    if "error" in r and r["error"]:
        return None if with_status else []
    result = r.get("result", {}) or {}
    items = result.get("structuredContent", {}).get("result", [])
    if not items:
        content = result.get("content", [])
        for c in content if isinstance(content, list) else []:
            if isinstance(c, dict) and c.get("type") == "text":
                try:
                    sc = json.loads(c["text"])
                    if isinstance(sc, dict):
                        items = sc.get("result") or sc.get("tasks") or []
                except Exception:
                    pass
    return [{"id": t.get("id", ""), "pid": t.get("projectId", pid),
             "title": t.get("title", ""), "priority": t.get("priority", 3),
             "due": _due_date_str(t.get("dueDate") or ""),
             "tags": t.get("tags") or [], "content": t.get("content") or ""}
            for t in items if isinstance(t, dict)]

def _readback_created_task(project_id, task_id, title):
    """创建任务后的权威回读；返回规范化任务，或 None（未确认）。"""
    expected_title = str(title or "")
    for delay in (0.25, 0.75):
        time.sleep(delay)
        listed = _fetch_project_tasks(project_id, with_status=True)
        if listed is None:
            continue
        found = next((x for x in listed if x.get("id") == task_id
                      and x.get("title") == expected_title
                      and (not x.get("pid") or x.get("pid") == project_id)), None)
        if found is not None:
            return found
    return None

def _tick_priority(v):
    """滴答只认 0/1/3/5 档位：把 0-5 的任意值归档到最近档（4→5、2→1），字符串高/中/低也归档"""
    p = int(round(v)) if isinstance(v, (int, float)) else _norm_priority(v)
    return 5 if p >= 4 else 3 if p >= 2 else 1 if p >= 1 else 0


def _due_date_str(s):
    """把 TickTick 返回的 dueDate 归一为北京时间的 YYYY-MM-DD。
    裸日期串直接截取；带时区的完整时间（如 2026-08-18T16:00:00+0000）转 +8 后取日期，
    避免 UTC 切片把北京时间 8/19 的任务显示成 8/18。"""
    if not s:
        return ""
    s2 = str(s).replace("T00:00:00+0000", "").replace("T00:00:00+08:00", "")
    if s2 != s:
        return s2[:10]
    try:
        # py3.10 的 fromisoformat 不认无冒号偏移（+0000/+0800），先补冒号
        s3 = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", str(s)).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s3)
        return (dt + timedelta(hours=8)).strftime("%Y-%m-%d")
    except Exception:
        return str(s)[:10]


def load_ticktick_tasks():
    """从 TickTick 拉所有未完成任务（4 个项目并发拉取）"""
    tasks = []
    with ThreadPoolExecutor(max_workers=len(PROJECT_IDS)) as ex:
        for out in ex.map(lambda pid: _fetch_project_tasks(pid, with_status=True), PROJECT_IDS.values()):
            if out is None:
                # 不把缺失项目的残缺集合缓存为完整任务表；调用方保留旧快照。
                raise RuntimeError("部分任务项目暂不可用，请稍后重试")
            tasks.extend(out)
    return tasks


_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_int(s):
    """中文数字（一~十、两、十五、二十）→ int；认不出返回 None。"""
    import re as _re
    s = str(s).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s in _CN_NUM:
        return _CN_NUM[s]
    m = _re.match(r"^十([一二三四五六七八九])?$", s)
    if m:
        return 10 + (_CN_NUM.get(m.group(1), 0) if m.group(1) else 0)
    m = _re.match(r"^([一二三四五六七八九])十([一二三四五六七八九])?$", s)
    if m:
        return _CN_NUM[m.group(1)] * 10 + (_CN_NUM.get(m.group(2), 0) if m.group(2) else 0)
    return None


def _parse_nl_schedule(spec):
    """自然语言时间 → cron 表达式。支持：每天X点X分 / 每周X / 每隔X小时 / 每小时"""
    import re as _re
    s = str(spec or "").strip()
    if not s:
        return None
    m = _re.search(r"每天?[早中晚]?([0-9]{1,2})[点:：时]([0-9]{1,2})?分?", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2) or 0)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return "%d %d * * *" % (mm, hh)
    m = _re.search(r"每隔?([0-9]+)小时", s)
    if m:
        n = max(1, int(m.group(1)))
        return "0 */%d * * *" % n
    if "每小时" in s or "每个小时" in s:
        return "0 * * * *"
    # 每周X：支持多天（每周一三五）+ 可选时刻（每周一晚上8点），默认 9 点
    wdmap = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 0, "天": 0}
    m = _re.search(r"每周?([一二三四五六日天]{1,5})", s)
    if not m:
        m = _re.search(r"每周?([1-7])", s)
    if m:
        ch = m.group(1)
        if ch.isdigit():
            days = str(int(ch) % 7)
        else:
            days = ",".join(str(wdmap[c]) for c in ch)
        hh, mm = 9, 0
        tm = _re.search(r"([0-9]{1,2})[点:：时](半|\d{1,2})?分?", s)
        if tm:
            hh = int(tm.group(1))
            _mm = tm.group(2)
            mm = 30 if _mm == "半" else int(_mm or 0)
            if ("晚上" in s or "午夜" in s) and hh == 12:
                hh = 0
            elif ("下午" in s or "晚上" in s or "傍晚" in s) and 1 <= hh < 12:
                hh += 12
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return "%d %d * * %s" % (mm, hh, days)
    return None

# 微信推送目标（可选）：在 config 文件或部署环境变量配 WECHAT_DELIVER=weixin:<openid>；
# 未配置时「定时提醒」降级为仅创建本地 cron 任务，不推送微信。
_WECHAT_DELIVER = _cfg.get("WECHAT_DELIVER", "").strip()


# ── 管家自定义约束（用户/管家可按需增改，注入每次对话）──
BUTLER_RULES_FILE = _dd_expanduser_default("DASH_BUTLER_RULES_FILE", "butler_rules.json")
def _load_butler_rules():
    data = _read_json(BUTLER_RULES_FILE, [])
    return data if isinstance(data, list) else []
def _save_butler_rule(rule):
    rules = _load_butler_rules()
    if rule not in rules:
        rules.append(rule)
    if not _write_json(BUTLER_RULES_FILE, rules[-30:]):
        return None
    return rules
def _butler_rules_text():
    rules = _load_butler_rules()
    if not rules:
        return ""
    return "\n【用户给管家的自定义约束（必须遵守，优先级最高）】\n" + "\n".join("· " + r for r in rules) + "\n"


def _data_authority_status():
    """Return a payload-free summary of authority/revision/conflict state."""
    report = _read_json(SYNC_CONFLICT_FILE, {})
    if not isinstance(report, dict):
        report = {}
    raw_items = [x for x in (report.get("items") or []) if isinstance(x, dict)]
    domains = sorted({str(x.get("domain"))[:80] for x in raw_items if x.get("domain")})
    return {
        "registryVersion": 1,
        "domainCount": len(DATA_AUTHORITY_REGISTRY),
        "revisionedDomainCount": sum(1 for item in DATA_AUTHORITY_REGISTRY.values() if item.get("revision")),
        "writableDomainCount": sum(1 for item in DATA_AUTHORITY_REGISTRY.values() if item.get("writable")),
        "conflictCount": len(raw_items),
        "conflictDomains": domains[:20],
        "shadow": {"mode": "read-model", "authority": "source-domains"},
    }


def _call_vision(image_b64, mime, question, system):
    """Responses API 多模态图像理解：用 doubao-seed-2-1-turbo 看图。返回文本。"""
    import urllib.request as _u, json as _j
    base = _cfg.get("LLM_API_BASE", "").rstrip("/")
    key = _cfg.get("LLM_API_KEY", "")
    if not base or not key or _demo_enabled():
        raise RuntimeError("vision_not_configured")
    url = base + "/responses"
    content = []
    if system:
        content.append({"type": "input_text", "text": system})
    content.append({"type": "input_text", "text": question})
    content.append({"type": "input_image", "image_url": "data:%s;base64,%s" % (mime or "image/png", image_b64)})
    payload = {"model": "doubao-seed-2-1-turbo",
               "input": [{"role": "user", "content": content}],
               "max_output_tokens": 4000}
    req = _u.Request(url, data=_j.dumps(payload).encode(),
                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    with _u.urlopen(req, timeout=180) as r:
        d = _j.loads(r.read())
    out = d.get("output") or []
    txt = ""
    for o in out:
        if o.get("type") == "message":
            c = o.get("content") or []
            if isinstance(c, list):
                txt += "".join((x.get("text") or "") for x in c if isinstance(x, dict))
            elif isinstance(c, str):
                txt += c
        elif o.get("text"):
            txt += o["text"]
    return txt.strip()

def _kb_diag_append(rec):
    """kb_diag.jsonl 追加一条（超 KB_DIAG_MAX_BYTES 先轮转 .old 保留一代）。"""
    import json as _j
    _kb_path, _kb_max = KB_DIAG_FILE, KB_DIAG_MAX_BYTES
    try:
        if os.path.exists(_kb_path) and os.path.getsize(_kb_path) > _kb_max:
            os.replace(_kb_path, _kb_path + ".old")
    except OSError:
        pass
    with open(_kb_path, "a", encoding="utf-8") as _f:
        _f.write(_j.dumps(rec, ensure_ascii=False) + "\n")


# 健康扩展指标的唯一后端注册表；响应同时回传 extra_keys，前端可据此校验漂移。
HEALTH_EXTRA_KEYS = (
    "vo2_max","flights_climbed","cycling_distance","basal_energy_burned",
    "time_in_daylight","walking_speed","walking_step_length",
    "walking_asymmetry_percentage","walking_double_support_percentage",
    "walking_heart_rate_average","stair_speed_up","stair_speed_down",
    "six_minute_walking_test_distance","environmental_audio_exposure",
    "headphone_audio_exposure","apple_stand_time")

class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DASHBOARD_DIR, **kwargs)

    def _is_trusted_client(self):
        """只信任 loopback 来源；外网直连（DASH_HOST 误开 0.0.0.0 时）一律拒绝。
        VPS 经 nginx 反代到 127.0.0.1，后端看到的是 loopback，正常放行。
        Docker 等反代场景可设 DASH_ALLOW_REMOTE=1 关闭此防护（信任反代层）。"""
        raw_host = self.headers.get("Host", "")
        try:
            host = urlparse("//" + raw_host).hostname
        except ValueError:
            return False
        allowed_hosts = {"localhost", "127.0.0.1", "::1"}
        allowed_hosts.update(x.strip().lower() for x in os.environ.get("DASH_ALLOWED_HOSTS", "").split(",") if x.strip())
        if host not in allowed_hosts:
            return False
        if os.environ.get("DASH_ALLOW_REMOTE") == "1":
            return True
        client = self.client_address[0] if self.client_address else ""
        return client in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")

    def _origin_allowed(self):
        """跨站写防护：浏览器对跨站 POST 必带 Origin 头，而 text/plain 的
        「简单请求」可绕过 CORS 预检直接到达本地端口——仅靠绑定 loopback 挡不住。
        规则：无 Origin 放行；本地 HTTP 必须主机和端口均相同；
        反代来源须列入 DASH_ALLOWED_ORIGINS 精确白名单，其余一律 403。"""
        origin = self.headers.get("Origin", "")
        if not origin:
            return True
        try:
            host = (urlparse(origin).hostname or "").lower()
        except Exception:
            return False
        if not host:
            return False
        try:
            parsed = urlparse(origin)
            if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                return False
            expected = urlparse("http://" + self.headers.get("Host", ""))
            if parsed.scheme == "http" and host in ("localhost", "127.0.0.1", "::1") and host == expected.hostname and (parsed.port or 80) == (expected.port or 80):
                return True
            return origin in [x.strip() for x in os.environ.get("DASH_ALLOWED_ORIGINS", "").split(",") if x.strip()]
        except ValueError:
            return False

    def _forbidden(self):
        body = b'{"error":"forbidden: non-loopback access denied"}'
        self.send_response(403)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _shadow_status(self):
        """Cheap read-only status; full reconciliation remains an offline command."""
        paths = [self.LOCAL_STATE_FILE, HEALTH_NOTES_FILE, RELATIONS_FILE, LISTENING_FILE, PROACTIVE_FILE]
        out = {"mode": "shadow", "ticktick_configured": bool(TICKTICK_TOKEN),
               "local_sources": {str(p): os.path.exists(p) for p in paths},
               "reconcile_command": "python3 shadow_live_check.py"}
        try:
            with open(SHADOW_REPORT_FILE, encoding="utf-8") as f:
                report = json.load(f)
                out["shadow_summary"] = {"healthy": report.get("healthy"), "entities": report.get("entities"),
                                          "changed": len(report.get("changed", [])), "missing": len(report.get("missing", [])),
                                          "extra": len(report.get("extra", [])), "checked_at": report.get("checked_at") or report.get("revision")}
                # 只返回有限的可读差异摘要，不把 shadow payload/任务正文整体下发到前端。
                out["shadow_details"] = {
                    "changed": [{"id": str(x.get("id", ""))[:120], "fields": [str(f)[:80] for f in (x.get("fields") or [])[:12]]}
                                for x in (report.get("changed") or [])[:20] if isinstance(x, dict)],
                    "missing": [str(x)[:120] for x in (report.get("missing") or [])[:20]],
                    "extra": [str(x)[:120] for x in (report.get("extra") or [])[:20]],
                }
        except Exception: pass
        # Explicit ?check=1 performs a read-only remote probe; normal UI calls stay cheap.
        if parse_qs(urlparse(getattr(self, "path", "")).query).get("check", ["0"])[0] == "1":
            now = time.time()
            if _shadow_probe_cache["data"] is not None and now - _shadow_probe_cache["at"] < 60:
                out.update(_shadow_probe_cache["data"])
            else:
                try:
                    probe = {"ticktick_tasks": len(load_ticktick_tasks()), "ticktick_reachable": True}
                except Exception as exc:
                    probe = {"ticktick_reachable": False}; probe.update(_client_error(exc, "upstream_error"))
                probe["checked_at"] = datetime.now().isoformat(timespec="seconds")
                _shadow_probe_cache.update({"at": now, "data": probe})
                out.update(probe)
        return out

    def _sync_conflicts(self):
        """Return bounded, payload-free summaries from the last two-way merge."""
        report = _read_json(SYNC_CONFLICT_FILE, {})
        if not isinstance(report, dict):
            report = {}
        allowed = ("domain", "key", "reason", "left_side", "right_side", "left_ts", "right_ts", "fields")
        items = [{k: x.get(k) for k in allowed if k in x}
                 for x in (report.get("items") or []) if isinstance(x, dict)][-20:]
        report_id = str(report.get("updated_at") or report.get("revision") or "current")[:160]
        acks = _read_json(SYNC_CONFLICT_ACK_FILE, [])
        ack_ids = {str(x.get("id")) for x in acks if isinstance(x, dict) and str(x.get("report_id") or "") == report_id}
        visible, acknowledged = [], 0
        for item in items:
            if _sync_conflict_id(report_id, item) in ack_ids:
                acknowledged += 1
            else:
                visible.append(item)
        return {"count": len(visible), "total_count": len(items), "acknowledged_count": acknowledged,
                "updated_at": report.get("updated_at") or "", "items": visible,
                "available": bool(report)}

    def _ack_sync_conflict(self, data):
        """Mark one conflict as seen without choosing or overwriting either side."""
        report = _read_json(SYNC_CONFLICT_FILE, {})
        if not isinstance(report, dict):
            report = {}
        report_id = str(report.get("updated_at") or report.get("revision") or "current")[:160]
        key = str((data or {}).get("key") or "").strip()[:200]
        if not key:
            return {"success": False, "error": "缺少冲突 key"}
        wanted_fields = (data or {}).get("fields")
        wanted_fields = [str(x)[:80] for x in wanted_fields] if isinstance(wanted_fields, list) else []
        allowed = ("domain", "key", "reason", "left_side", "right_side", "left_ts", "right_ts", "fields")
        item = None
        for raw in (report.get("items") or []):
            if not isinstance(raw, dict) or str(raw.get("key") or "") != key:
                continue
            candidate = {k: raw.get(k) for k in allowed if k in raw}
            if wanted_fields and [str(x) for x in (candidate.get("fields") or [])] != wanted_fields:
                continue
            item = candidate
            break
        if item is None:
            return {"success": False, "error": "冲突已不在当前报告中，未写入"}
        conflict_id = _sync_conflict_id(report_id, item)
        rows = _read_json(SYNC_CONFLICT_ACK_FILE, [])
        if not isinstance(rows, list):
            rows = []
        if not any(isinstance(x, dict) and x.get("report_id") == report_id and x.get("id") == conflict_id for x in rows):
            rows.append({"report_id": report_id, "id": conflict_id, "key": key,
                         "acked_at": datetime.now().isoformat(timespec="seconds")})
        if not _write_json(SYNC_CONFLICT_ACK_FILE, rows[-200:]):
            return {"success": False, "error": "冲突确认记录写入失败"}
        out = self._sync_conflicts()
        out.update({"success": True, "message": "已标记为已知悉；未修改任一端数据"})
        return out

    def _shadow_reconcile(self):
        """运行只读影子对账脚本并回读报告；冷却期间复用现有报告，避免重复打 TickTick。"""
        global _shadow_reconcile_last
        now = time.time()
        with _shadow_reconcile_lock:
            if now - _shadow_reconcile_last < _shadow_reconcile_cooldown:
                out = self._shadow_status()
                report = _read_json(SHADOW_REPORT_FILE, {})
                if isinstance(report, dict):
                    out["ticktick_reachable"] = True
                    if report.get("ticktick_tasks") is not None:
                        out["ticktick_tasks"] = report.get("ticktick_tasks")
                out.update({"success": True, "cooldown": True,
                            "message": "对账刚刚运行过，已返回最近结果"})
                return out
            script = os.path.join(DASHBOARD_DIR, "shadow_live_check.py")
            if not os.path.isfile(script):
                return {"success": False, "error": "缺少 shadow_live_check.py"}
            try:
                proc = subprocess.run(
                    [sys.executable, script, "--report", SHADOW_REPORT_FILE],
                    cwd=DASHBOARD_DIR, capture_output=True, text=True, timeout=90)
                if proc.returncode != 0:
                    return {"success": False, "error": "影子对账失败，请检查本地服务日志"}
                _shadow_reconcile_last = now
                out = self._shadow_status()
                try:
                    report = _read_json(SHADOW_REPORT_FILE, {})
                    if isinstance(report, dict):
                        out["ticktick_reachable"] = True
                        if report.get("ticktick_tasks") is not None:
                            out["ticktick_tasks"] = report.get("ticktick_tasks")
                except Exception:
                    pass
                out.update({"success": True, "cooldown": False, "message": "完整影子对账完成"})
                return out
            except subprocess.TimeoutExpired:
                return {"success": False, "error": "影子对账超时（>90 秒），未修改业务数据"}
            except Exception as exc:
                return {"success": False, "error": "影子对账异常：%s" % str(exc)[:300]}

    def do_GET(self):
        if not self._is_trusted_client():
            return self._forbidden()
        if not self._origin_allowed():
            return self._forbidden()
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            try:
                if path == "/api/boot":
                    self.send_json(self._get_boot_payload())
                elif path == "/api/healthz":
                    self.send_json({"ok": True, "service": "dashboard", "ts": datetime.now().isoformat(timespec="seconds")})
                elif path == "/api/dashboard-data":
                    self.send_json(cached("dash", self._get_dashboard_data))
                elif path == "/api/habits":
                    self.send_json(cached("habits", self._get_habits))
                elif path == "/api/vps-status":
                    self.send_json(self._vps_status())
                elif path == "/api/shadow/status":
                    self.send_json(self._shadow_status())
                elif path == "/api/sync/conflicts":
                    self.send_json(self._sync_conflicts())
                elif path == "/api/mutations/recent":
                    self.send_json(_recent_mutation_receipts())
                elif path == "/api/recovery/status":
                    self.send_json(_recovery_status())
                elif path == "/api/ops/status":
                    self.send_json(_ops_status())
                elif path == "/api/advisor/outcomes":
                    self.send_json(_advisor_outcome_summary())
                elif path == "/api/ai/usage":
                    # 只返回模型/耗时/成功率元数据，不暴露提示词、健康数值或生成正文。
                    self.send_json(usage_summary())
                elif path == "/api/ai/contracts":
                    # 供维护/验收查看契约版本；只读、无用户数据。
                    self.send_json({"catalog": prompt_meta("draft").get("catalog"),
                                    "scenes": {name: prompt_meta(name) for name in (
                                        "butler", "advisor", "oracle", "night_watch", "weekly_review",
                                        "health_briefing", "draft", "profile", "memory_digest", "socratic")}})
                elif path == "/api/ai/oracle":
                    self.send_json(self._get_ai_oracle())
                elif path == "/api/stats":
                    self.send_json(cached("stats", self._get_stats))
                elif path == "/api/stats/week":
                    self.send_json(cached("week", self._get_week_report))
                elif path == "/api/review/draft":
                    self.send_json(self._get_weekly_draft())
                elif path == "/api/local-state":
                    self.send_json(self._get_local_state())
                elif path == "/api/relations":
                    self.send_json(self.get_relations())
                elif path == "/api/listening":
                    self.send_json(self.get_listening())
                elif path == "/api/health/notes":
                    self.send_json(self.get_health_notes())
                elif path == "/api/health/changes":
                    times = self._health_fetch_times()
                    ingest = self._health_ingest_status()
                    self.send_json({"health": times.get("health", ""), "workout": times.get("workout", ""),
                                    "ingest": ingest,
                                    "fingerprint": "%s|%s|%s" % (times.get("health", ""), times.get("workout", ""),
                                                                    ingest.get("latest_receive_at", ""))})
                elif path == "/api/health/ingest":
                    self.send_json(self._health_ingest_status())
                elif path == "/api/week-plan":
                    self.send_json(self.get_week_plan())
                elif path == "/api/proactive":
                    self.send_json(self.get_proactive())
                elif path == "/api/butler/status":
                    self.send_json(self.butler_status())
                elif path == "/api/butler/history":
                    self.send_json({"history": _load_butler_history()})
                elif path == "/api/butler/actions":
                    # 只读最近动作审计；不把任务正文/参数回传到 UI。
                    rows = [{k: row.get(k) for k in ("clientTurnId", "kind", "status", "success", "duplicate", "ts", "error", "message", "entityId", "undoable", "undoed")}
                            for row in _load_action_audit()[-50:] if isinstance(row, dict)]
                    self.send_json({"actions": rows, "count": len(rows)})
                elif path == "/api/butler/prefs":
                    self.send_json({"prefs": _load_butler_prefs()})
                elif path == "/api/butler/gaps":
                    self.send_json({"gaps": self._butler_gap_analysis()})
                elif path == "/api/butler/profile":
                    self.send_json(_load_user_profile())
                elif path == "/api/coach/cards":
                    self.send_json(self.coach_cards())
                elif path == "/api/chain-health":
                    self.send_json(cached("dash", self._get_dashboard_data).get("chain_health", {}))
                elif path == "/api/correlation":
                    q = parse_qs(urlparse(self.path).query)
                    try:
                        cdays = max(7, min(30, int(q.get("days", ["14"])[0])))
                    except Exception:
                        cdays = 14
                    self.send_json(cached("corr%d" % cdays, lambda: self._correlation(cdays)))
                elif path == "/api/health":
                    q = parse_qs(urlparse(self.path).query)
                    try:
                        hdays = max(1, min(60, int(q.get("days", ["14"])[0])))
                    except Exception:
                        hdays = 14
                    # fresh=1 由健康页手动刷新使用，绕过 30 秒聚合缓存，确保刚推送的 Apple Watch 数据可见。
                    fresh = q.get("fresh", ["0"])[0] == "1"
                    hdata = self._get_health(hdays) if fresh else cached("health%d" % hdays, lambda: self._get_health(hdays))
                    out = dict(hdata) if isinstance(hdata, dict) else hdata
                    if isinstance(out, dict):
                        out["alerts"] = self._health_alerts(hdata)
                    self.send_json(out)
                elif path == "/api/health/briefing":
                    self.send_json(self._health_briefing())
                elif path == "/api/health/behavior":
                    self.send_json(self._behavior_corr())
                else:
                    self.send_json({"error": "not found"})
                return
            except Exception as e:
                # R1: 统一未捕获异常的对外契约为 {error}，与既有 not-found 分支保持同形，避免连接被硬中断
                try:
                    payload = {"success": False}; payload.update(_client_error(e)); self.send_json(payload, status=500)
                except Exception:
                    pass
            return
        # 静态文件（图片 + 前端资源：chart.js / manifest / 图标）——白名单内才可读（见 _static_allowed）
        if path.endswith(STATIC_EXTS):
            if not _static_allowed(path):
                self.send_json({"error": "not found"})
                return
            fpath = os.path.realpath(os.path.join(DASHBOARD_DIR, path.lstrip('/')))
            base = os.path.realpath(DASHBOARD_DIR)
            lexical = os.path.abspath(os.path.join(DASHBOARD_DIR, path.lstrip('/')))
            if os.path.isfile(fpath) and fpath.startswith(base + os.sep) and fpath == lexical:
                st = os.stat(fpath)
                etag = '"%x-%x"' % (st.st_mtime_ns, st.st_size)
                if path.endswith('/sw.js'):
                    cache_control = "no-cache"
                elif path.endswith(('.html', '.htm')):
                    # HTML 是应用壳，任何导航/交互修复都必须在下一次打开时生效。
                    # 离线回退由 Service Worker 负责，HTTP 层不再缓存旧页面。
                    cache_control = "no-store"
                else:
                    cache_control = "max-age=86400"
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304); self.send_cors(); self.send_security_headers()
                    self.send_header("ETag", etag); self.send_header("Cache-Control", cache_control)
                    self.end_headers(); return
                self.send_response(200)
                self.send_cors()
                self.send_security_headers()
                ctype = MIME_MAP.get(os.path.splitext(path)[1].lower(), 'application/octet-stream')
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(st.st_size))
                self.send_header("ETag", etag)
                # RT-B-01：SW 版本指纹取 document.lastModified，必须真实下发 Last-Modified，
                # 否则浏览器按规范回落为“加载时刻”，版本每次加载都变 → SW 每载重装全量 precache。
                self.send_header("Last-Modified", self.date_time_string(st.st_mtime))
                # HTML 文件短缓存（5分钟），便于更新即时生效；其他静态资源长缓存
                self.send_header("Cache-Control", cache_control)
                self.end_headers()
                try:
                    with open(fpath, "rb") as f:
                        self.wfile.write(f.read())
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
        # portal.html 门户页（个人 AI 工作台入口）
        if path in ("/portal.html", "/portal"):
            fpath = os.path.join(DASHBOARD_DIR, "portal.html")
            if os.path.isfile(fpath):
                st = os.stat(fpath); etag = '"%x-%x"' % (st.st_mtime_ns, st.st_size)
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304); self.send_cors(); self.send_security_headers()
                    self.send_header("ETag", etag); self.send_header("Cache-Control", "max-age=300")
                    self.end_headers(); return
                self.send_response(200)
                self.send_cors()
                self.send_security_headers()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(st.st_size)); self.send_header("ETag", etag)
                self.send_header("Last-Modified", self.date_time_string(st.st_mtime))
                self.send_header("Cache-Control", "max-age=300")
                self.end_headers()
                try:
                    with open(fpath, "rb") as f:
                        self.wfile.write(f.read())
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
        # 其余路径一律返回驾驶舱页面（nginx 反代剥掉前缀后落到这里）
        main_file = os.path.join(DASHBOARD_DIR, "hermes-dashboard.html")
        st = os.stat(main_file)
        etag = '"%x-%x"' % (st.st_mtime_ns, st.st_size)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304); self.send_cors(); self.send_security_headers()
            self.send_header("ETag", etag); self.send_header("Cache-Control", "no-store"); self.end_headers(); return
        self.send_response(200)
        self.send_cors()
        self.send_security_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(st.st_size))
        self.send_header("ETag", etag)
        # RT-B-01：用户导航入口（/ 等）走本兜底分支，SW 版本指纹依赖此头
        self.send_header("Last-Modified", self.date_time_string(st.st_mtime))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with open(main_file, "rb") as f:
                self.wfile.write(f.read())
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self):
        if not self._is_trusted_client():
            return self._forbidden()
        if urlparse(self.path).path == "/api/healthz":
            body = json.dumps({"ok": True, "service": "dashboard"}, ensure_ascii=False).encode()
            self.send_response(200); self.send_cors(); self.send_security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
            self.end_headers(); return
        self.send_response(404)
        self.send_security_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if not self._is_trusted_client():
            return self._forbidden()
        if not self._origin_allowed():
            return self._forbidden()
        path = urlparse(self.path).path
        retry_after = _rate_limit_retry(
            path, self.client_address[0] if self.client_address else ""
        )
        if retry_after:
            self.send_json(
                {"success": False, "error": "请求过于频繁，请稍后重试"},
                status=429,
                extra_headers={"Retry-After": str(retry_after)},
            )
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self.send_json({"success": False, "error": "非法 Content-Length"}, status=400)
            return
        # 普通 JSON 请求上限 1 MiB；视觉接口单独允许约 8 MiB base64 + JSON 开销。
        limit = 12 * 1024 * 1024 if path == "/api/butler/vision" else 1024 * 1024
        if length < 0 or length > limit:
            self.send_json({"success": False, "error": "请求体过大"}, status=413)
            return
        try:
            data = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            # 不把 JSON 解码器的内部细节（可能含请求片段）回传给公网客户端。
            self.send_json({"success": False, "error": "请求体解析失败"}, status=400)
            return
        if not isinstance(data, dict):
            self.send_json({"success": False, "error": "请求体必须是 JSON 对象"}, status=400)
            return

        # 写操作幂等：客户端重试/双击时，同一路由 + clientMutationId 只执行一次。
        mutation_key = str(data.get("clientMutationId") or "").strip()[:160]
        if mutation_key and path in MUTATION_ROUTES:
            previous = _find_mutation_result(path, mutation_key)
            if previous is not None:
                duplicate = dict(previous)
                duplicate["duplicate"] = True
                duplicate["clientMutationId"] = mutation_key
                self.send_json(duplicate)
                return
            with _mutation_runtime_lock:
                active_key = (path, mutation_key)
                if active_key in _active_mutations:
                    self.send_json({"success": False, "uncertain": True,
                                    "error": "该操作正在执行，请稍候重试",
                                    "clientMutationId": mutation_key}, status=409)
                    return
                _active_mutations.add(active_key)
            self._mutation_ctx = (path, mutation_key, data)

        # P0：写操作后由前端触发，失效聚合缓存，确保下次读取拿到最新数据（否则会被 30s 缓存挡住）
        if path == "/api/invalidate":
            bust_cache()
            self.send_json({"success": True})
            return
        try:
            if path == "/api/tasks/create":     self.send_json(self.create_task(data))
            elif path == "/api/tasks/update":   self.send_json(self.update_task(data))
            elif path == "/api/tasks/archive":  self.send_json(self.set_task_archived(data, True))
            elif path == "/api/tasks/unarchive": self.send_json(self.set_task_archived(data, False))
            elif path == "/api/tasks/delete":   self.send_json(self.delete_task(data))
            elif path == "/api/tasks/complete": self.send_json(self.complete_task(data))
            elif path == "/api/roles/create":   self.send_json(self.create_role(data))
            elif path == "/api/roles/sync":     self.send_json(self.sync_role(data))
            elif path == "/api/habits/checkin": self.send_json(self.checkin_habit(data))
            elif path == "/api/habits/create":  self.send_json(self.create_habit(data))
            elif path == "/api/review/save":    self.send_json(self.save_review(data))
            elif path == "/api/ai/oracle/refresh":
                # 写操作契约：刷新并生成成功时显式返回 success，避免前端统一写入层误判为“保存失败”。
                oracle = self._get_ai_oracle(force=True)
                self.send_json(dict(oracle, success=True) if isinstance(oracle, dict) else {"success": False, "error": "神谕生成失败"})
            elif path == "/api/review/draft":      self.send_json(self._get_weekly_draft())
            elif path == "/api/review/draft/refresh":
                # This endpoint is a mutation because it writes the draft file;
                # return an explicit success marker so the idempotency ledger
                # stores the receipt and retries cannot regenerate it twice.
                draft = self._generate_weekly_draft()
                self.send_json(dict(draft, success=True) if isinstance(draft, dict)
                               else {"success": False, "error": "复盘草稿生成失败"})
            elif path == "/api/local-state":      self.send_json(self._save_local_state(data))
            elif path == "/api/relations":      self.send_json(self.post_relation(data))
            elif path == "/api/listening":      self.send_json(self.post_listening(data))
            elif path == "/api/health/notes":   self.send_json(self.post_health_note(data))
            elif path == "/api/health/notes/delete": self.send_json(self.delete_health_note(data))
            elif path == "/api/week-plan":      self.send_json(self.post_week_plan(data))
            elif path == "/api/relations/delete": self.send_json(self.delete_relation(data))
            elif path == "/api/relations/restore": self.send_json(self.restore_relation(data))
            elif path == "/api/relations/update": self.send_json(self.update_relation(data))
            elif path == "/api/listening/delete": self.send_json(self.delete_listening(data))
            elif path == "/api/listening/restore": self.send_json(self.restore_listening(data))
            elif path == "/api/listening/update": self.send_json(self.update_listening(data))
            elif path == "/api/habits/dimension": self.send_json(self.set_habit_dimension(data))
            elif path == "/api/habits/update":  self.send_json(self.update_habit(data))
            elif path == "/api/roles/delete":   self.send_json(self.delete_role(data))
            elif path == "/api/proactive":      self.send_json(self.post_proactive(data))
            elif path == "/api/proactive/delete": self.send_json(self.delete_proactive(data))
            elif path == "/api/proactive/restore": self.send_json(self.restore_proactive(data))
            elif path == "/api/proactive/update": self.send_json(self.update_proactive(data))
            elif path == "/api/butler/vision":
                    img = data.get("image") or ""
                    mime = data.get("mime") or "image/png"
                    q = data.get("question") or ""
                    allowed_mimes = {"image/png", "image/jpeg", "image/webp", "image/gif"}
                    mime = str(mime).split(";", 1)[0].strip().lower()
                    if not isinstance(img, str) or not img:
                        self.send_json({"success": False, "error": "缺少图片"}); return
                    if mime not in allowed_mimes:
                        self.send_json({"success": False, "error": "不支持的图片类型"}, status=400); return
                    if len(img) > 8 * 1024 * 1024:
                        self.send_json({"success": False, "error": "图片过大 (>8MB base64)，已拒绝"}); return
                    try:
                        decoded = base64.b64decode(img, validate=True)
                    except Exception:
                        self.send_json({"success": False, "error": "图片编码无效"}, status=400); return
                    if not decoded:
                        self.send_json({"success": False, "error": "缺少图片"}, status=400); return
                    try:
                        if _demo_enabled() or not (_cfg.get("LLM_API_BASE") and _cfg.get("LLM_API_KEY")):
                            self.send_json({"success": False, "degraded": True, "error": "图片理解未配置，图片不会离开本机。"}); return
                        sysctx = "你是 AI 管家。用户发来一张图，请仔细看：先说图片里是什么内容，再给出 1-3 句有洞察的看法或建议。用中文，简洁、有温度、不啰嗦。"
                        txt = _call_vision(img, mime, q or "帮我看看这张图：内容是什么，我该关注什么？", sysctx)
                        if not txt:
                            self.send_json({"success": False, "error": "图片理解失败"}); return
                        self.send_json({"success": True, "reply": txt})
                    except Exception as e:
                        self.send_json({"success": False, **_client_error(e, "upstream_error")})
                    return
            elif path == "/api/butler/chat":    self.send_json(self.butler_chat(data))
            elif path == "/api/butler/act":     self.send_json(self.butler_act(data))
            elif path == "/api/butler/undo":    self.send_json(self.undo_action(data))
            elif path == "/api/kb-diag":
                # 键盘诊断上报：真机几何采样落数据目录，不进业务数据。
                # 注意：body 已由 do_POST 顶部读入 data，绝不能再 self.rfile.read()（二次读会阻塞挂起）。
                # ZC 新视角审计：jsonl 只追加原本无界增长——超 KB_DIAG_MAX_BYTES 轮转为 .old（保留一代）。
                try:
                    rec = dict(data if isinstance(data, dict) else {})
                    rec["ua"] = (self.headers.get("User-Agent", "")[:80])
                    import datetime as _dt
                    rec["_t"] = _dt.datetime.now().strftime("%H:%M:%S")
                    _kb_diag_append(rec)
                except Exception:
                    pass
                self.send_json({"ok": True})
            elif path == "/api/butler/stream":
                self.send_sse(self.butler_stream(data))
            elif path == "/api/ai/draft":
                self.send_sse(self.ai_draft_stream(data))
            elif path == "/api/advisor/stream":
                self.send_sse(self.advisor_stream(data))
            elif path == "/api/advisor/fill":
                self.send_json(self.advisor_fill(data))
            elif path == "/api/advisor/outcomes":
                self.send_json(_record_advisor_outcome(data))
            elif path == "/api/munger/advisor":
                self.send_sse(self.munger_advisor_stream(data))
            elif path == "/api/munger/fill":
                self.send_json(self.munger_advisor_fill(data))
            elif path == "/api/ai/socratic":
                self.send_json(self.ai_socratic(data))
            elif path == "/api/ai/socratic/compile":
                self.send_json(self.ai_socratic_compile(data))
            elif path == "/api/butler/history/clear":
                ok = _clear_butler_history()
                self.send_json({"success": bool(ok),
                                **({"msg": "对话历史已清空"} if ok else {"error": "对话历史写入失败"})})
            elif path == "/api/butler/profile":
                self.send_json(_merge_user_profile(data))
            elif path == "/api/butler/prefs/save":
                key = data.get("key", "")
                value = data.get("value", "")
                if not key:
                    self.send_json({"success": False, "error": "缺少 key"})
                else:
                    prefs = _save_butler_pref(key, value)
                    self.send_json({"success": bool(prefs is not None), "prefs": prefs,
                                    **({} if prefs is not None else {"error": "偏好写入失败"})})
            elif path == "/api/shadow/reconcile":
                self.send_json(self._shadow_reconcile())
            elif path == "/api/sync/conflicts/ack":
                self.send_json(self._ack_sync_conflict(data))
            elif path == "/api/recovery/restore":
                self.send_json(_restore_json_snapshot(data.get("key"), data.get("backup")))
            elif path == "/api/sync-vps":
                self.send_json(self._run_sync_vps(data))
            else:                               self.send_json({"error": "not found"})
        except Exception as e:
            # R1: 统一未捕获异常的对外契约为稳定错误码+通用文案（详情只进服务端日志），
            # 避免连接被硬中断导致前端网络层报错
            try:
                self.send_json({"success": False, **_client_error(e, "internal_error", "do_POST fallback")})
            except Exception:
                pass

    def send_cors(self):
        origin = self.headers.get("Origin", "")
        try:
            host = urlparse(origin).hostname or ""
        except Exception:
            host = ""
        if origin and self._origin_allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def send_security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def do_OPTIONS(self):
        self.send_response(204); self.send_cors(); self.send_security_headers()
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_json(self, data, status=200, extra_headers=None):
        ctx = getattr(self, "_mutation_ctx", None)
        if ctx and isinstance(data, dict) and data.get("success") is True:
            route, key, request_data = ctx
            data = dict(data)
            data.setdefault("clientMutationId", key)
            data.setdefault("receipt", _standard_mutation_receipt(route, key, data, request_data))
        if status == 200 and isinstance(data, dict):
            if data.get("error") == "not found":
                status = 404
            elif data.get("success") is False:
                # degraded 响应（AI 未配置等预期降级）保持 200：
                # 「没配 AI」不是客户端请求错误，400 会污染前端 console 并误报门禁
                status = 200 if data.get("degraded") else 400
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status); self.send_cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for key, value in (extra_headers or {}).items():
            key, value = str(key).strip(), str(value).strip()
            if key and "\r" not in key and "\n" not in key and "\r" not in value and "\n" not in value:
                self.send_header(key, value)
        try:
            self.end_headers(); self.wfile.write(body)
        finally:
            ctx = getattr(self, "_mutation_ctx", None)
            if ctx:
                route, key, _request_data = ctx
                try:
                    if isinstance(data, dict) and data.get("success") is True and status < 400:
                        _store_mutation_result(route, key, data)
                finally:
                    with _mutation_runtime_lock:
                        _active_mutations.discard((route, key))
                    self._mutation_ctx = None

    def send_sse(self, gen):
        """发送 SSE 流式响应：逐块写入 wfile，结束后立刻半关闭写端，
        让浏览器无需等 keep-alive 闲置超时即可感知到流尾——避免多轮对话时
        前一轮的悬挂 fetch 阻塞下一轮 /api/butler/stream。"""
        self.send_response(200); self.send_cors()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # 必须为 close：BaseHTTPRequestHandler 不会自动做 chunked 分帧，
        # 若用 keep-alive 客户端将永远等不到响应结束，导致 busy 卡死多轮对话。
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.end_headers()
        try:
            for chunk in gen:
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            pass
        finally:
            # 主动关闭生成器，使断连时的部分历史保存与上游资源清理立即执行。
            try:
                close = getattr(gen, "close", None)
                if close:
                    close()
            except Exception as exc:
                print("[sse-close] " + type(exc).__name__, flush=True)
            self.close_connection = True
            try:
                # 半关闭写端：通知浏览器流结束，立刻释放 fetch 锁
                self.wfile.flush()
                if hasattr(self.connection, "shutdown"):
                    try:
                        self.connection.shutdown(socket.SHUT_WR)
                    except Exception:
                        pass
            except Exception:
                pass

    def log_message(self, fmt, *args):
        pass  # 静音访问日志

    # ─── 数据聚合 ───
    def _dash(self):
        return cached("dash", self._get_dashboard_data)

    def _habits(self):
        return cached("habits", self._get_habits)

    def _get_boot_payload(self):
        """R1: 启动聚合端点——一次请求带回首屏全部数据，把 N×隧道RTT 压成 1×。
        每项独立 try/except：单项失败不影响其余，结构上与各自单独端点完全同形。"""
        out = {}
        def _safe(key, fn):
            try:
                out[key] = fn()
            except Exception as e:
                out[key] = {"success": False}; out[key].update(_client_error(e))
        # dashboard(TickTick)、统计、周报、健康来自互不依赖的数据源。串行执行会把
        # 4 个延迟相加，超过前端聚合等待窗后又触发 5 个回退请求。并行只读聚合取最大延迟。
        jobs = {
            "dashboard": lambda: cached("dash", self._get_dashboard_data),
            "stats": lambda: cached("stats", self._get_stats),
            "week": lambda: cached("week", self._get_week_report),
            "health": lambda: cached("health1", lambda: self._get_health(1)),
        }
        with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="boot-read") as pool:
            futures = {key: pool.submit(fn) for key, fn in jobs.items()}
            for key, future in futures.items():
                try:
                    out[key] = future.result()
                except Exception as e:
                    out[key] = {"success": False}; out[key].update(_client_error(e))
        # _get_dashboard_data 已预热 habits；此时读取同一缓存不会并发重复请求 TickTick。
        _safe("habits", lambda: cached("habits", self._get_habits))
        _safe("relations", lambda: self.get_relations())
        _safe("listening", lambda: self.get_listening())
        _safe("proactive", lambda: self.get_proactive())
        _safe("week_plan", lambda: self.get_week_plan())
        def _chain_from_dashboard():
            dashboard = out.get("dashboard")
            if not isinstance(dashboard, dict) or dashboard.get("error"):
                raise RuntimeError("dashboard unavailable")
            return dashboard.get("chain_health", {})
        _safe("chain_health", _chain_from_dashboard)
        # 神谕允许慢（可能触发LLM生成），其余先返回也不等它太久： oracle 由前端单独拉
        # 健康数据（今日）已在上方并行读取；renderTodayHealth 仍保留自己的 10min 过期窗口。
        return out

    def _get_dashboard_data(self):
        if _demo_enabled():
            d = _demo()
            out = {"demo": True, "roles": d.get("roles", []),
                   "tasks": d.get("tasks", []), "archived_tasks": [],
                   "habits": d.get("habits", []), "chain_health": {},
                   "parse_warnings": []}
            return out
        roles = load_obsidian_roles() or []
        all_tasks = cached("tasks", load_ticktick_tasks) or []
        archived_tasks = [t for t in all_tasks if "archived" in (t.get("tags") or [])]
        tasks = [t for t in all_tasks if "archived" not in (t.get("tags") or [])]
        habits = cached("habits", self._get_habits).get("habits", [])
        dims = _load_habit_dims()
        # ── 链路健康度（五条数据链路自检）──
        roles_with_kr = sum(1 for r in roles if any(
            str(k.get("text") if isinstance(k, dict) else k).strip()
            for k in (r.get("key_results") or [])))
        role_names = {str(r.get("name") or "").strip() for r in roles if str(r.get("name") or "").strip()}
        task_links = []
        parse_warnings = []
        for task in tasks:
            content = str(task.get("content") or "")
            role_match = re.search(r"(?m)^角色[：:]\s*(.+?)\s*$", content)
            quad_match = re.search(r"(?m)^象限[：:]\s*(q[1-4])\s*$", content, re.I)
            if role_match and role_match.group(1).strip() not in role_names:
                parse_warnings.append({"taskId": task.get("id", ""), "title": task.get("title", ""), "kind": "unknown_role", "role": role_match.group(1).strip()})
            if "角色" in content and not role_match:
                parse_warnings.append({"taskId": task.get("id", ""), "title": task.get("title", ""), "kind": "malformed_role"})
            if "象限" in content and not quad_match:
                parse_warnings.append({"taskId": task.get("id", ""), "title": task.get("title", ""), "kind": "malformed_quadrant"})
            task_links.append((task, role_match.group(1).strip() if role_match else "",
                               quad_match.group(1).lower() if quad_match else ""))
        tasks_with_role = sum(1 for _, role, _ in task_links if role in role_names)
        tasks_with_quadrant = sum(1 for _, _, quad in task_links if quad)
        tasks_actionable = sum(1 for _, role, quad in task_links if role in role_names and quad)
        roles_with_action = len({role for _, role, _ in task_links if role in role_names})
        habits_with_dim = sum(1 for h in habits if h.get("dimension"))
        habits_with_role = sum(1 for h in habits if str(h.get("role") or "").strip() in role_names)
        rels = _alive(_read_json(RELATIONS_FILE, []))
        wkey = _week_key()
        relations_this_week = sum(1 for x in rels if x.get("week") == wkey)
        listens = _alive(_read_json(LISTENING_FILE, []))
        pro = _read_json(PROACTIVE_FILE, {}) or {}
        pro["concerns"] = _alive(pro.get("concerns") or [])
        pro["logs"] = _alive(pro.get("logs") or [])
        chain_health = {
            "rolesWithKR": roles_with_kr,
            "rolesWithAction": roles_with_action,
            "rolesTotal": len(roles),
            "tasksWithRole": tasks_with_role,
            "tasksWithQuadrant": tasks_with_quadrant,
            "taskParseWarnings": parse_warnings[:100],
            "tasksActionable": tasks_actionable,
            "tasksTotal": len(tasks),
            "habitsWithDim": habits_with_dim,
            "habitsWithRole": habits_with_role,
            "habitsTotal": len(habits),
            "relationsThisWeek": relations_this_week,
            "listeningThisWeek": sum(1 for x in listens if x.get("week") == wkey),
            "influenceCount": sum(1 for c in (pro.get("concerns") or [])
                                  if c.get("circle") == "influence"),
            "concernCount": len(pro.get("concerns") or []),
            "proactiveLogsThisWeek": sum(1 for x in (pro.get("logs") or [])
                                         if x.get("week") == wkey),
            "lastReviewWeek": _last_review_week(roles),
            "thisWeek": wkey,
        }
        gaps = []
        for role in roles:
            if not any(str(k.get("text") if isinstance(k, dict) else k).strip()
                       for k in (role.get("key_results") or [])):
                gaps.append({"type": "role_kr", "entityId": role.get("id"), "go": "roles",
                             "label": "补关键结果", "detail": "角色「%s」还没有可验证的 KR" % role.get("name", "")})
            if str(role.get("name") or "").strip() not in {linked for _, linked, _ in task_links}:
                gaps.append({"type": "role_action", "entityId": role.get("id"), "go": "roles",
                             "label": "拆出本周行动", "detail": "角色「%s」还没有关联行动" % role.get("name", "")})
        for task, role, quad in task_links:
            missing = []
            if role not in role_names:
                missing.append("角色")
            if not quad:
                missing.append("象限")
            if missing:
                gaps.append({"type": "task_meta", "entityId": task.get("id"), "go": "actions",
                             "label": "补行动归属", "detail": "「%s」缺%s" % (task.get("title", "未命名"), "、".join(missing))})
        for habit in habits:
            missing = []
            if not habit.get("dimension"):
                missing.append("四维")
            if str(habit.get("role") or "").strip() not in role_names:
                missing.append("角色")
            if missing:
                gaps.append({"type": "habit_meta", "entityId": habit.get("id"), "go": "habits",
                             "label": "补习惯归属", "detail": "「%s」缺%s" % (habit.get("name", "未命名"), "、".join(missing))})
        if chain_health["lastReviewWeek"] != wkey:
            gaps.append({"type": "review", "entityId": "", "go": "review",
                         "label": "完成本周复盘", "detail": "最近复盘：%s" % (chain_health["lastReviewWeek"] or "尚未复盘")})
        chain_health["gaps"] = gaps[:24]
        chain_health["gapCount"] = len(gaps)
        chain_health["ppc"] = self._ppc_scores(tasks, habits, chain_health)
        return {"roles": roles, "tasks": tasks, "archived_tasks": archived_tasks, "habits": habits,
                "chain_health": chain_health,
                "correlation_tip": self._correlation_tip(),
                "cached_at": time.time()}

    def _vps_status(self):
        """Mac 本地探测 VPS 驾驶舱是否在线（VPS 自身直接返回在线）"""
        if ON_VPS:
            return {"online": True, "self": True}
        hit = _cache.get("vps")
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        online = False
        # P2-D9: 统一走 _vps_get（鉴权/超时/错误集中处理）
        status, _ = _vps_get("", timeout=5)
        online = status == 200
        data = {"online": online}
        _cache["vps"] = (time.time(), data)
        return data

    def _run_sync_vps(self, data=None):
        """保留接口错误契约，但禁止通过 HTTP 执行部署脚本。"""
        return {"success": False, "error": "开源版不提供远程部署执行接口", "logs": ""}

    # ─── 健康×任务跨域相关性（睡眠充足日 vs 第二象限完成数；只读计算，不写数据）───

    def _correlation(self, days=14):
        """近 N 天：按日历日对齐「睡眠 total」与「当日第二象限(Q2)完成数」，分桶输出。
        数据不足（某桶 <3 天）时 correlation_hint 返回 None，不硬编结论。"""
        days = max(7, min(30, int(days)))
        try:
            hd = self._get_health(days)
        except Exception:
            hd = {}
        sleep = {x.get("date"): x.get("total") for x in (hd.get("sleep") or [])
                 if x.get("date") and x.get("total") is not None}
        # 当日 Q2 完成数：completedTime 落在该日的任务，content 含「象限：q2」
        today_d = date.today()
        start = (today_d - timedelta(days=days - 1)).isoformat() + "T00:00:00+08:00"
        end = today_d.isoformat() + "T23:59:59+08:00"
        q2_by_day = {}
        try:
            r = call_ticktick_mcp("list_completed_tasks_by_date", {"search": {
                "projectIds": list(PROJECT_IDS.values()),
                "startDate": start, "endDate": end}})
            items = r.get("result", {}).get("structuredContent", {}).get("result") or []
            for t in items:
                if "象限：q2" not in (t.get("content") or ""):
                    continue
                ct = t.get("completedTime") or ""
                try:
                    d = datetime.fromisoformat(ct.replace("+0000", "+00:00")).astimezone().date().isoformat()
                except Exception:
                    continue
                q2_by_day[d] = q2_by_day.get(d, 0) + 1
        except Exception:
            pass
        buckets = {"good": {"label": "睡眠≥7h", "days": [], "q2s": []},
                   "mid": {"label": "睡眠6-7h", "days": [], "q2s": []},
                   "poor": {"label": "睡眠<6h", "days": [], "q2s": []}}
        for d in range(days):
            day = (today_d - timedelta(days=d)).isoformat()
            sl = sleep.get(day)
            if sl is None:
                continue
            q2 = q2_by_day.get(day, 0)
            key = "good" if sl >= 7 else ("poor" if sl < 6 else "mid")
            buckets[key]["days"].append(day)
            buckets[key]["q2s"].append(q2)
        out_buckets = []
        for k in ("good", "mid", "poor"):
            b = buckets[k]
            n = len(b["days"])
            out_buckets.append({"key": k, "label": b["label"], "days": n,
                                "q2_completed_avg": round(sum(b["q2s"]) / n, 2) if n else None})
        hint = None
        has_signal = False
        if len(buckets["good"]["days"]) >= 3 and len(buckets["poor"]["days"]) >= 3:
            ga = sum(buckets["good"]["q2s"]) / len(buckets["good"]["days"])
            pa = sum(buckets["poor"]["q2s"]) / len(buckets["poor"]["days"])
            if pa > 0:
                diff = (ga - pa) / pa * 100
                if abs(diff) >= 10:
                    hint = "睡眠充足日（≥7h）Q2 完成率比不足日（<6h）高 %.0f%%" % abs(diff)
                    has_signal = True
            elif ga > 0:
                hint = "睡眠充足日平均完成 %.1f 件 Q2，不足日（<6h）为 0——睡眠和产出疑似正相关" % ga
                has_signal = True
        elif len(buckets["good"]["days"]) + len(buckets["poor"]["days"]) >= 3:
            hint = "数据还不够（每档至少 3 天才能下结论）——攒够数据后这里会告诉你睡眠和产出是否挂钩"
        return {"days": days, "buckets": out_buckets, "correlation_hint": hint,
                "has_signal": has_signal,
                "yesterday_sleep": sleep.get((today_d - timedelta(days=1)).isoformat()),
                "date": today_d.isoformat(), "available": bool(sleep)}

    def _correlation_tip(self):
        """昨晚睡眠<6h 且相关性有真实统计信号（has_signal）→ 生成今日 Q2 安排提醒。
        诚实红线：数据不足（hint 只是"攒数据"提示）或睡眠正常 → 空字符串，不硬编。"""
        try:
            c = cached("corr14", lambda: self._correlation(14))
            ys = c.get("yesterday_sleep")
            hint = c.get("correlation_hint")
            if ys is None or float(ys) >= 6 or not c.get("has_signal") or not hint:
                return ""
            return ("昨晚睡眠 %.1fh（不足 6h）。你的真实数据显示：%s——"
                    "今日 Q2 任务注意安排，别排高认知负荷。" % (float(ys), hint))
        except Exception:
            return ""

    # ─── 健康页 AI 三件套：异常预警（纯规则）/ 每日身体简报（LLM·日缓存）/ 行为关联（纯统计）───


    def _health_fetch_times(self):
        """扫描数据目录下的原始健康文件，返回健康和锻炼数据的最新获取时间。"""
        out = {"health": "", "workout": ""}
        root = _dd("health-data", "raw")
        if not os.path.isdir(root):
            return out
        latest = {"health": None, "workout": None}
        for day in sorted(os.listdir(root)):
            daydir = os.path.join(root, day)
            if not os.path.isdir(daydir):
                continue
            for fn in sorted(os.listdir(daydir)):
                if not fn.endswith("_ios.json"):
                    continue
                ts_str = fn.split("_")[0]
                if not (ts_str.isdigit() and len(ts_str) >= 6):
                    continue
                ts = day + " " + ts_str[:2] + ":" + ts_str[2:4]
                try:
                    with open(os.path.join(daydir, fn), encoding="utf-8") as f:
                        j = json.load(f)
                except Exception:
                    continue
                d2 = j.get("data") or {}
                if d2.get("metrics") and (latest["health"] is None or ts > latest["health"]):
                    latest["health"] = ts
                if d2.get("workouts") and (latest["workout"] is None or ts > latest["workout"]):
                    latest["workout"] = ts
        out["health"] = latest["health"] or ""
        out["workout"] = latest["workout"] or ""
        return out

    def _health_ingest_status(self):
        """Return metadata about the raw Apple Watch feed, without exposing values."""
        result = scan_ingest(_dd("health-data", "raw"))
        # 只暴露传输安全性与可供 HAE 填写的公开端点；token 永不进入响应。
        result["transport"] = {
            "secure": str(HEALTH_PUSH_PUBLIC_URL).lower().startswith("https://"),
            "push_url": HEALTH_PUSH_PUBLIC_URL,
            "legacy_http_supported": True,
        }
        return result

    def _health_alerts(self, d):
        """异常预警条（纯规则，零 LLM）：睡眠债 / HRV 连续低 / 静息心率升高 / 呼吸率偏快 / 睡眠效率低。
        基线口径与恢复度一致（基线=窗口内除最新值外均值）；只基于真实数据触发，最多 3 条，每条带 focus 供前端跳 ✦ 深聊。"""
        try:
            d = d if isinstance(d, dict) else {}
            alerts = []
            dv = d.get("derived") if isinstance(d.get("derived"), dict) else {}
            debt = _f(dv.get("sleep_debt"))
            if debt is not None and debt >= 5:
                alerts.append({"key": "debt", "icon": "⚖️", "focus": "cbti",
                               "text": "近7晚累计睡眠债 %.1fh——身体在透支，本周先还债再谈强度" % debt})
            def _series(key):
                values = []
                source = d.get(key)
                if not isinstance(source, (list, tuple)):
                    return values
                for item in source:
                    if not isinstance(item, dict):
                        continue
                    value = _f(item.get("qty"))
                    if value is not None:
                        values.append(value)
                return values

            hrv = _series("hrv")
            if len(hrv) >= 6:
                base, tail = sum(hrv[:-3]) / len(hrv[:-3]), hrv[-3:]
                if base > 0 and all(v < base * 0.95 for v in tail):
                    alerts.append({"key": "hrv", "icon": "🌊", "focus": "recovery",
                                   "text": "HRV 连续 3 天低于基线（基线%dms）——自主神经没缓过来，安排主动恢复" % round(base)})
            rhr = _series("rhr")
            if len(rhr) >= 3:
                base, cur = sum(rhr[:-1]) / len(rhr[:-1]), rhr[-1]
                if cur > base + 5:
                    alerts.append({"key": "rhr", "icon": "💙", "focus": "recovery",
                                   "text": "静息心率 %d bpm 高于基线 %d——身体还在踩油门，今天别上强度" % (round(cur), round(base))})
            ra = _f(dv.get("resp_avg"))
            if ra is not None and ra >= 14.5:
                alerts.append({"key": "resp", "icon": "🫁", "focus": "resp",
                               "text": "近7晚平均呼吸率 %.1f 次/分（偏快）——白天练 2 分钟共振呼吸补副交感" % ra})
            eff = _f(dv.get("sleep_eff"))
            if eff is not None and eff < 85:
                alerts.append({"key": "eff", "icon": "🎯", "focus": "cbti",
                               "text": "近7晚睡眠效率 %.0f%%（<85%%）——收紧就寝窗口，困了才上床" % eff})
            # 指标断供可见（数据源问题由前端预警条显示）。
            _missing_notes = []
            for _k, _nm in (("rhr", "静息心率"), ("resp", "呼吸率"), ("wrist", "手腕温度"), ("mindful", "正念")):
                _arr = d.get(_k) or []
                if not isinstance(_arr, (list, tuple)):
                    continue
                if _arr:
                    _tail = _arr[-1] if isinstance(_arr[-1], dict) else {}
                    _last = str(_tail.get("date") or "")[:10]
                    try:
                        _gap = (date.today() - date.fromisoformat(_last)).days
                        if _gap >= 2:
                            _missing_notes.append("%s止于%s（缺%d天）" % (_nm, _last[5:], _gap))
                    except Exception:
                        pass
            if _missing_notes:
                alerts.append({"key": "stale", "icon": "📭", "focus": "",
                               "text": "数据断供：" + "、".join(_missing_notes[:2]) + "——检查 Apple Watch 同步/HAE 推送权限"})
            _quality = d.get("quality") if isinstance(d.get("quality"), dict) else {}
            _invalid = _quality.get("invalid") if isinstance(_quality.get("invalid"), dict) else {}
            if _invalid:
                _bad = "、".join("%s(%s)" % (k, v) for k, v in list(_invalid.items())[:2])
                alerts.append({"key": "invalid", "icon": "⚠️", "focus": "",
                               "text": "数据质量异常：" + _bad + "——已标记，不用于强结论"})
            return alerts[:3]
        except Exception:
            return []

    def _health_briefing(self):
        """每日身体简报（数据指纹感知：最新一晚睡眠变了就重新生成）：
        ① 昨晚身体一句话 ② 今日强度建议 ③ 今晚一个动作（优先滴定熄灯时刻）。
        规则先行：无昨晚睡眠数据不生成；LLM 失败不缓存（下次打开重试）。"""
        if _demo_enabled():
            return {"demo": True, "briefing": "【演示数据】睡眠 7.1h（较昨天 +0.4），步数 8,400，HRV 52ms。配置健康数据源后这里显示你的真实简报。"}
        today = date.today().isoformat()
        d = cached("health14", lambda: self._get_health(14))
        sleep = (d or {}).get("sleep") or []
        _brief_evidence = build_evidence(
            "health_briefing", d or {}, window_days=14, source="health_snapshot",
            fetched_at=(d or {}).get("fetched_at") if isinstance(d, dict) else None,
            ingest=(d or {}).get("ingest") if isinstance(d, dict) else None,
        )
        # 数据指纹：最新一晚睡眠 date+total。当日新睡眠推上来 → 指纹变 → 重新生成（修"解读滞后"）
        _fp = ""
        if sleep:
            _l = sleep[-1]
            _fp = "%s_%.1f" % (_l.get("date") or "", _f(_l.get("total")) or 0)
        for _fk in ("hrv", "wrist", "rhr"):
            _arr = d.get(_fk) or []
            if _arr:
                _v = _arr[-1].get("qty")
                _fp += "_%s=%.1f" % (_fk, _v or 0)
        try:
            cache = _read_json(BRIEFING_CACHE_FILE, {}) or {}
            if (cache.get("date") == today and cache.get("text") and cache.get("fp") == _fp):
                return {"date": today, "text": cache["text"], "cached": True,
                        "evidence": cache.get("evidence") or _brief_evidence}
        except Exception:
            cache = {}
        if not sleep:
            return {"date": today, "text": "", "evidence": _brief_evidence}
        last = sleep[-1]
        hrv = [x.get("qty") for x in (d.get("hrv") or []) if x.get("qty") is not None]
        rhr = [x.get("qty") for x in (d.get("rhr") or []) if x.get("qty") is not None]
        # HEALTH-ABC-C2: 简报快照补手腕温度 / 呼吸率（医学级解读输入）
        wt = [x.get("qty") for x in (d.get("wrist") or []) if x.get("qty") is not None]
        resp = [x.get("qty") for x in (d.get("resp") or []) if x.get("qty") is not None]
        snap = {"昨晚睡眠": {"时长h": last.get("total"), "深睡h": last.get("deep"), "REMh": last.get("rem"),
                           "核心h": last.get("core"), "清醒h": last.get("awake"),
                           "就寝": str(last.get("start") or "")[11:16], "起床": str(last.get("end") or "")[11:16]},
                "近7晚": d.get("derived") or {},
                "HRV": {"最新ms": round(hrv[-1], 1), "基线ms": round(sum(hrv[:-1]) / len(hrv[:-1]), 1)} if len(hrv) >= 2 else None,
                "静息心率": {"最新bpm": round(rhr[-1]), "基线bpm": round(sum(rhr[:-1]) / len(rhr[:-1]))} if len(rhr) >= 2 else None,
                "手腕温度": {"最新℃": round(wt[-1], 1), "基线℃": round(sum(wt[:-1]) / len(wt[:-1]), 1),
                          "偏离℃": round(wt[-1] - sum(wt[:-1]) / len(wt[:-1]), 2)} if len(wt) >= 2 else None,
                "呼吸率": {"最新次/分": round(resp[-1], 1)} if resp else None}
        rd = d.get("readiness") or {}
        if rd.get("available"):
            weak = sorted(rd.get("factors") or [], key=lambda f: f[1] - f[2])[:3]
            snap["恢复度"] = {"总分": rd.get("score"),
                             "弱项": ["%s %s（%d/%d）" % (f[0], f[3], f[1], f[2]) for f in weak]}
        try:
            note = [n for n in _read_json(HEALTH_NOTES_FILE, []) if n.get("date") == today]
            if note:
                snap["今晨主观备注"] = note[-1].get("note")
        except Exception:
            pass
        system = (prompt_header("health_briefing") +
                  "你是「" + USER_NAME + "驾驶舱」的健康简报助手。基于真实 Apple Watch 数据生成今日身体简报，恰好 3 行：\n"
                  "第1行以「昨晚」开头：一句话概括昨晚身体——从睡眠时长/结构、HRV、静息心率、呼吸率、手腕温度里挑最突出的一两件，引用真实数字。\n"
                  "第2行以「今日」开头：强度建议——从「轻量化/正常/可上强度」三选一，一句理由（基于恢复度弱项和睡眠债）。\n"
                  "第3行以「今晚」开头：一个具体动作——优先用滴定熄灯时刻 rec_bedtime（如「今晚 01:36 熄灯」，把小时数换算成 01:36 这样的时刻），"
                  "或针对最弱项给一个微行动。\n"
                  "规则：每行 ≤40 字；克制、具体、不喊口号不鸡汤；只用给你的数据，没有的指标不提；"
                  "有今晨备注时必须把主客观对齐着看；不输出任何其他内容，不用 markdown。\n"
                  "【医学级解读口径（HEALTH-ABC-C2）】参考范围：成人睡眠 7-9h；深睡≈15-25%、REM≈20-25%；"
                  "HRV 个体差异极大、只看相对自己基线的稳定与否；静息心率连续 3-5 天较基线 +5~10bpm=身体硬扛；"
                  "手腕温度偏离基线 ≥0.3℃=应激信号（生病/酒精/深夜大餐/训练）、≥0.5℃ 且伴 HRV 降+静息心率升多为生病前兆；"
                  "呼吸率 12-20 次/分。解读原则：HRV/静息心率/手腕温度是「身体响应」主角，睡眠/活动只作上下文，避免双重惩罚。"
                  "若数据出现上述异常信号，必须在对应行给出明确提示或就医指征（如「连续多日如此建议就医」）。")
        r = _call_llm([{"role": "system", "content": system},
                       {"role": "user", "content": "今日健康数据快照：\n" + json.dumps(snap, ensure_ascii=False, default=str)}],
                      temperature=0.7, max_tokens=260, feature="health_briefing")
        text = (r.get("content") or "").strip()
        if "error" in r or not text:
            return {"date": today, "text": ""}
        try:
            _write_json(BRIEFING_CACHE_FILE, {"date": today, "text": text, "fp": _fp,
                                              "evidence": _brief_evidence})
        except Exception:
            pass
        return {"date": today, "text": text, "evidence": _brief_evidence}

    def _behavior_corr(self):
        """行为关联统计（纯统计，零 LLM）：有睡眠备注的日子 vs 无备注日子的平均睡眠对比。
        备注与睡眠同为「早晨醒来日」口径，按日期直接对齐；两侧各≥3天才出结论（诚实红线，不硬编）。"""
        try:
            d = cached("health30", lambda: self._get_health(30))
            sleep = {x.get("date"): x.get("total") for x in (d.get("sleep") or [])
                     if x.get("date") and x.get("total") is not None}
            if len(sleep) < 6:
                return {"available": False}
            noted = {str(n.get("date")) for n in _read_json(HEALTH_NOTES_FILE, []) if n.get("date")}
            a = [v for k, v in sleep.items() if k in noted]
            b = [v for k, v in sleep.items() if k not in noted]
            if len(a) < 3 or len(b) < 3:
                return {"available": False, "note_days": len(a), "plain_days": len(b),
                        "hint": "备注还太少（有备注 %d 晚 / 无备注 %d 晚），各攒够 3 晚后这里会告诉你：写备注的晚上睡眠到底差多少" % (len(a), len(b))}
            am, bm = sum(a) / len(a), sum(b) / len(b)
            diff = am - bm
            hint = ("近30天：有备注的 %d 晚平均睡眠 %.1fh，无备注的 %d 晚 %.1fh——%s。" %
                    (len(a), am, len(b), bm,
                     "写备注的晚上确实睡得更差（差 %.1fh），备注本身就是信号" % abs(diff) if diff < 0
                     else "写备注的晚上反而睡得更多（多 %.1fh）——注意可能是事后解释而非原因" % diff))
            return {"available": True, "note_days": len(a), "plain_days": len(b),
                    "note_avg": round(am, 1), "plain_avg": round(bm, 1),
                    "diff": round(diff, 1), "hint": hint}
        except Exception:
            return {"available": False}

    # ─── 健康看板（Apple Watch · Health Auto Export 推送，数据仅在 VPS 落盘）───

    def _get_health(self, days=14):
        """近 N 天健康聚合：本地直接读数据目录，配置远端时走代理。"""
        if _demo_enabled():
            return _demo().get("health", {})
        data = self._health_from_local(days) if ON_VPS else self._health_from_vps(days)
        if isinstance(data, dict) and "quality" not in data:
            data["quality"] = self._health_quality(data)
        return data

    @staticmethod
    def _health_quality(data):
        """标记健康数据质量，不修改原始数值：区分断供(stale)与异常值(invalid)。"""
        if not isinstance(data, dict):
            return {"has_data": False, "stale": [], "invalid": {}}
        today = date.today()
        stale, invalid = [], {}
        ranges = {"sleep": (0, 24), "hrv": (0, 500), "rhr": (20, 220),
                  "resp": (4, 40), "spo2": (0, 100), "wrist": (25, 45),
                  "steps": (0, 200000), "energy": (0, 20000)}
        for metric, (lo, hi) in ranges.items():
            rows = data.get(metric) or []
            if not rows:
                continue
            last = str(rows[-1].get("date") or "")[:10]
            try:
                age = (today - date.fromisoformat(last)).days
                if age >= 2:
                    stale.append({"metric": metric, "last": last, "days": age})
            except Exception:
                stale.append({"metric": metric, "last": last, "days": None})
            bad = 0
            for row in rows:
                value = row.get("total") if metric == "sleep" else row.get("qty")
                if value is not None:
                    try:
                        if not (lo <= float(value) <= hi): bad += 1
                    except (TypeError, ValueError):
                        bad += 1
            if bad: invalid[metric] = bad
        return {"has_data": any(bool(data.get(k)) for k in ranges),
                "stale": stale, "invalid": invalid,
                "window_days": len(data.get("days") or [])}

    @staticmethod
    def _health_quality_context(data):
        """给 AI 的短质量摘要：明确缺失/陈旧/异常，禁止模型把空白当正常值。"""
        if not isinstance(data, dict):
            return "健康数据不可用"
        labels = {"sleep": "睡眠", "heart": "心率", "energy": "活动能量", "exercise": "锻炼",
                  "hrv": "HRV", "rhr": "静息心率", "spo2": "血氧", "resp": "呼吸率",
                  "steps": "步数", "dist": "步行距离", "wrist": "手腕温度", "stand": "站立"}
        window = len(data.get("days") or [])
        expected = ("sleep", "heart", "energy", "exercise", "hrv", "rhr", "spo2", "resp", "steps", "dist")
        missing = [labels[k] for k in expected if not isinstance(data.get(k), list) or not data.get(k)]
        partial = ["%s %d/%d天" % (labels[k], len(data.get(k) or []), window)
                   for k in expected if window and 0 < len(data.get(k) or []) < window]
        q = data.get("quality") or DashboardHandler._health_quality(data)
        stale = []
        for row in q.get("stale") or []:
            stale.append("%s%s" % (labels.get(row.get("metric"), row.get("metric", "指标")),
                                    ("（%s天前）" % row["days"]) if row.get("days") is not None else ""))
        invalid = ["%s %s条" % (labels.get(k, k), v) for k, v in (q.get("invalid") or {}).items()]
        parts = []
        if data.get("source") == "cache" or data.get("stale"):
            parts.append("当前为缓存/降级数据")
        if missing:
            parts.append("未收到：" + "/".join(missing))
        if partial:
            parts.append("部分天：" + "/".join(partial))
        if stale:
            parts.append("陈旧：" + "/".join(stale))
        if invalid:
            parts.append("异常：" + "/".join(invalid))
        ingest = data.get("ingest") or {}
        if ingest.get("status") == "empty":
            parts.append("原始健康数据接收为空")
        elif ingest.get("status") == "stale":
            age = ingest.get("age_hours")
            parts.append("原始健康数据接收陈旧%s" % (("（%.1fh）" % float(age)) if isinstance(age, (int, float)) else ""))
        return "；".join(parts) if parts else "核心指标质量正常"

    def _health_from_vps(self, days=14):
        """本地端：从配置的远端服务代理拉取健康聚合。
        远端不可达时回退到数据目录中的上次成功快照，避免健康页空白。"""
        empty = {"source": "none", "days": [], "sleep": [], "heart": [],
                 "energy": [], "exercise": [], "mindful": [], "hrv": [], "rhr": [],
                 "spo2": [], "resp": [], "steps": [], "dist": [], "workouts": [],
                 "wrist": [], "stand": [], "effort": [], "workout_quality": {"count": 0}}
        snap_file = _dd_expanduser_default("DASH_HEALTH_SNAPSHOT_FILE", "health_snapshot.json")
        if not VPS_DASH_URL:
            return self._health_fallback(empty, snap_file, "VPS 端点未配置")
        try:
            # P2-D9: 统一走 _vps_get（鉴权/超时/错误集中处理）
            _st, body = _vps_get("/api/health?days=%d" % days, timeout=8)
            if _st is None:
                return self._health_fallback(empty, snap_file, "VPS 不可达")
            data = json.loads(body)
            if isinstance(data, dict) and isinstance(data.get("days"), list):
                data["source"] = "proxy"
                # VPS 旧版本可能没有新增指标键；把响应补成稳定 schema，避免
                # 某个缺失数组让健康页/AI 分支误判整段数据为空。
                for key in ("sleep", "heart", "energy", "exercise", "mindful", "hrv", "rhr",
                            "spo2", "resp", "steps", "dist", "wrist", "stand", "effort", "workouts"):
                    if not isinstance(data.get(key), list):
                        data[key] = []
                if not isinstance(data.get("workout_quality"), dict):
                    data["workout_quality"] = {"count": len(data.get("workouts") or [])}
                # VPS 旧版未算 derived 时本地补算（同一份代码，口径一致）
                if not data.get("derived"):
                    try:
                        data["derived"] = self._health_derived(data)
                    except Exception:
                        pass
                # 成功 → 落快照缓存（供 VPS 掉线时兜底）
                try:
                    snap = dict(data)
                    snap["_snap_ts"] = datetime.now().isoformat(timespec="seconds")
                    _write_json(snap_file, snap)
                except Exception:
                    pass
                return data
            return self._health_fallback(empty, snap_file, "VPS 端点未就绪")
        except Exception as e:
            return self._health_fallback(empty, snap_file, str(e)[:120])

    def _health_fallback(self, empty, snap_file, reason):
        """VPS 不可达时读上次成功快照兜底；无快照则返回空 + 降级标记。"""
        snap = _read_json(snap_file, None)
        if isinstance(snap, dict) and isinstance(snap.get("days"), list):
            ts = snap.pop("_snap_ts", None)
            snap["source"] = "cache"
            snap["stale"] = True
            snap["cached_at"] = ts or ""
            snap["error"] = reason or "VPS 暂不可达"
            return snap
        out = dict(empty)
        out["source"] = "error"
        out["error"] = reason or "VPS 不可达且无本地缓存"
        return out

    def _health_from_local(self, days=14):
        """服务端：读数据目录下按日期分组的原始文件并按天聚合。"""
        empty = {"source": "none", "days": [], "sleep": [], "heart": [],
                 "energy": [], "exercise": [], "mindful": [], "hrv": [], "rhr": [],
                 "spo2": [], "resp": [], "steps": [], "dist": [],
                 "wrist": [], "stand": [], "effort": [], "workouts": [],
                 "workout_quality": {"count": 0}}  # HEALTH-ABC-A1: stable schema for empty feeds
        ingest_status = self._health_ingest_status()
        empty["ingest"] = ingest_status
        root = _dd("health-data", "raw")
        if not os.path.isdir(root):
            return empty
        today = date.today()
        # HEALTH-ABC-F1: 收集已存在的日期目录，用于区分「跨天污染」与「历史回溯」——
        # 见下方非累计指标分支的规则说明。
        _existing_dirs = {d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))}
        # 派生指标（睡眠债/滴定）固定按 ≥14 天读窗取「最近7个有记录的夜晚」，与查询范围无关
        read_days = max(days, 14)
        want = {(today - timedelta(days=i)).isoformat() for i in range(read_days)}
        agg = {}  # date -> {metric_name: datapoint}
        rides = {}  # workout id -> workout（HEALTH-ABC-A1: 真实 HAE workout 提取，按 id 去重）
        for day in sorted(os.listdir(root)):
            if day not in want:
                continue
            daydir = os.path.join(root, day)
            if not os.path.isdir(daydir):
                continue
            for fn in sorted(os.listdir(daydir)):
                if not fn.endswith("_ios.json"):
                    continue
                try:
                    with open(os.path.join(daydir, fn), encoding="utf-8") as f:
                        j = json.load(f)
                except Exception:
                    continue
                for m in (j.get("data") or {}).get("metrics") or []:
                    name = m.get("name", "")
                    for p in m.get("data") or []:
                        if not isinstance(p, dict):
                            continue
                        # HAE 将单位放在 metric 级而非每个 data point；保留它，否则下游只能猜单位。
                        p = dict(p)
                        if m.get("units") and not p.get("units"):
                            p["units"] = m.get("units")
                        d = str(p.get("date", ""))[:10]
                        if not d:
                            continue
                        if name in CUMULATIVE_METRICS:
                            # HEALTH-ABC-D1: 累计型指标（HealthKit cumulative）同天取最大值——
                            # HAE 实时推送是「当日累计快照」，普通后读覆盖会被次日凌晨推送的「前日深夜残留」污染
                            # （如 8-22 活动能量被 8-23 文件覆盖成 47.7 kcal、8-20 步数被覆盖成 216 步；
                            #  累计语义下最大值≈最接近全天真实值）
                            _prev = agg.get(d, {}).get(name)
                            _q = _health_number(p.get("qty"))
                            _prev_q = _health_number((_prev or {}).get("qty"))
                            if _prev is None or (_q is not None and (_prev_q is None or _q > _prev_q)):
                                agg.setdefault(d, {})[name] = p
                        else:
                            # HEALTH-SLEEP: 睡眠多次推送取 totalSleep 最大者（HAE 对同一晚睡眠会多次推送阶段性快照，
                            # 后读覆盖会选到最后推的那次——但睡眠阶段随时间增长，越晚推的 start 越靠后、totalSleep 越小，
                            # 反而是"半截"快照；取 totalSleep 最大 = 覆盖整晚最完整，最接近真实睡眠时长。
                            # 同日多次推送取稳定的累计值，避免较小快照覆盖较大快照。
                            if name == "sleep_analysis":
                                _prev_s = agg.get(d, {}).get(name)
                                _ts = _health_number(p.get("totalSleep"))
                                _prev_ts = _health_number((_prev_s or {}).get("totalSleep"))
                                if _prev_s is None or (_ts is not None and (_prev_ts is None or _prev_ts < _ts)):
                                    agg.setdefault(d, {})[name] = p
                            else:
                                # HEALTH-ABC-F1: 非累计指标防跨天污染——HAE 每日推送是「全量快照」，
                                # 次日文件会带前日重新汇总值（数据点 date 指向前日），后读覆盖会把
                                # 「前日当天目录」的准确值顶掉（实锤：08-25 心率 91.3 被 08-26 文件的 65.7 覆盖）。
                                # 规则：数据点 date < 文件目录日期 且 该 date 已有对应目录 → 跨天污染，跳过；
                                #       该 date 无对应目录（历史回溯，如 08-19 全量导入 07月数据）→ 保留。
                                if d < day and d in _existing_dirs:
                                    continue
                                agg.setdefault(d, {})[name] = p
                # HEALTH-ABC-A1: 骑行 workout 提取（data.workouts 数组，非 metrics）
                for w in (j.get("data") or {}).get("workouts") or []:
                    if isinstance(w, dict):
                        # HAE exports normally carry an id; older exports do not.
                        # A stable local key keeps those records visible while
                        # still de-duplicating repeated push batches.
                        key = str(w.get("id") or "%s|%s|%s" % (
                            w.get("start") or w.get("date") or "", w.get("name") or "", w.get("duration") or ""))
                        if key.strip("|"):
                            rides[key] = w
        show = {(today - timedelta(days=i)).isoformat() for i in range(days)}
        packed = self._health_pack(agg, show, "vps")
        # HEALTH-ABC-A1: 骑行专项——真实 workout 转前端友好格式（替代原空数组占位）
        # 读取窗口会为睡眠派生指标至少扩展到 14 天，但页面仍应严格遵守用户选择的显示范围。
        _all_workouts, workout_quality = self._pack_rides(rides)
        packed["workouts"] = [w for w in _all_workouts if w.get("date") in show]
        packed["workout_quality"] = workout_quality
        packed["readiness"] = self._health_readiness(packed)
        full = self._health_pack(agg, want, "vps") if read_days > days else packed
        # 训练负荷需要使用完整读取窗口；若只传 packed，默认 7 天页面会丢掉 28 天负荷计算所需的历史。
        packed["derived"] = self._health_derived({"days": sorted(want),
                                                   "sleep": full.get("sleep") or [],
                                                   "resp": full.get("resp") or [],
                                                   "workouts": _all_workouts})
        _ft = self._health_fetch_times()
        packed["health_fetched_at"] = _ft["health"]
        packed["workout_fetched_at"] = _ft["workout"]
        packed["ingest"] = ingest_status
        return packed

    # HEALTH-ABC-A1: 骑行 workout → 前端友好格式（按日期倒序；ID 已在提取时去重）
    @staticmethod
    def _pack_rides(rides):
        """把 HAE workout 统一成可审计字段；不把推算值伪装成原始值。"""
        def _qty(value):
            return _health_number(value)

        def _units(value):
            return _health_unit(value)

        def _route_points(value):
            if isinstance(value, dict):
                for key in ("locations", "points", "coordinates", "route"):
                    if isinstance(value.get(key), list):
                        return value[key]
                return []
            return value if isinstance(value, list) else []

        def _gps_distance_km(value):
            points = []
            for raw in _route_points(value):
                if not isinstance(raw, dict):
                    continue
                try:
                    lat = float(raw.get("latitude", raw.get("lat")))
                    lon = float(raw.get("longitude", raw.get("lon", raw.get("lng"))))
                except (TypeError, ValueError):
                    continue
                if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
                    continue
                accuracy = _qty(raw.get("horizontalAccuracy", raw.get("accuracy")))
                if accuracy is not None and (accuracy < 0 or accuracy > 100):
                    continue
                if points and abs(lat - points[-1][0]) < 1e-9 and abs(lon - points[-1][1]) < 1e-9:
                    continue
                points.append((lat, lon))
            if len(points) < 2:
                return None
            total_m = 0.0
            for (lat1, lon1), (lat2, lon2) in zip(points, points[1:]):
                rlat1, rlon1 = math.radians(lat1), math.radians(lon1)
                rlat2, rlon2 = math.radians(lat2), math.radians(lon2)
                dlat, dlon = rlat2 - rlat1, rlon2 - rlon1
                seg_m = 6371000 * 2 * math.asin(math.sqrt(
                    math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2))
                # 单个坏 GPS 跳点不能把短途骑行放大成几十公里。
                if seg_m <= 5000:
                    total_m += seg_m
            return total_m / 1000 if total_m > 0 else None

        out = []
        quality = {"count": 0, "distance": {"reported": 0, "gps": 0, "missing": 0},
                   "duration": {"reported": 0, "missing": 0},
                   "speed": {"reported": 0, "derived": 0, "missing": 0},
                   "elevation": {"reported": 0, "missing": 0},
                   "energy": {"reported_kj": 0, "reported_kcal": 0, "unverified": 0, "missing": 0},
                   "heart_rate": {"reported": 0, "missing": 0}}
        values = rides.values() if isinstance(rides, dict) else (rides or [])
        for w in values:
            if not isinstance(w, dict):
                continue
            quality["count"] += 1
            st = str(w.get("start") or w.get("date") or "")
            dur_obj = w.get("duration")
            dur = _qty(dur_obj)
            dur_unit = _units(dur_obj)
            if dur is not None and dur_unit in {"h", "hr", "hour", "hours"}:
                dur *= 3600
            elif dur is not None and dur_unit in {"min", "minute", "minutes"}:
                dur *= 60
            duration_source = "reported" if dur is not None and dur > 0 else "missing"
            quality["duration"][duration_source] += 1
            dist_obj = w.get("distance")
            dist = _qty(dist_obj)
            dist_unit = _units(dist_obj)
            if dist is not None and dist_unit in {"m", "meter", "meters"}:
                dist /= 1000
            elif dist is not None and dist_unit in {"mi", "mile", "miles"}:
                dist *= 1.609344
            elif dist is not None and dist_unit in {"ft", "foot", "feet"}:
                dist *= 0.0003048
            distance_source = "reported" if dist is not None and dist > 0 else ""
            # HAE 部分记录不填 distance，但提供 GPS route；只用有效且不过大的相邻段补算。
            if dist is None or dist <= 0:
                gps_km = _gps_distance_km(w.get("route"))
                if gps_km is not None:
                    dist, distance_source = gps_km, "gps"
                elif not distance_source:
                    distance_source = "missing"
            quality["distance"][distance_source] += 1
            spd_obj = w.get("speed")
            spd = _qty(spd_obj)
            spd_unit = _units(spd_obj)
            if spd is not None and spd_unit in {"m/s", "mps", "meter/second", "meters/second"}:
                spd *= 3.6
            elif spd is not None and spd_unit in {"mph", "mi/h", "mile/hour", "miles/hour"}:
                spd *= 1.609344
            speed_source = "reported" if spd is not None and spd >= 0 else "missing"
            if speed_source == "missing" and dist is not None and dur and dur > 0:
                spd, speed_source = dist / (dur / 3600), "derived"
            quality["speed"][speed_source] += 1
            elev = _qty(w.get("elevationUp"))
            elev_unit = _units(w.get("elevationUp"))
            if elev is not None and elev_unit in {"ft", "foot", "feet"}:
                elev *= 0.3048
            elevation_source = "reported" if elev is not None else "missing"
            quality["elevation"][elevation_source] += 1
            aeb = _qty(w.get("activeEnergyBurned"))
            aeb_unit = _units(w.get("activeEnergyBurned"))
            energy_source = "missing"
            if aeb is not None:
                if aeb_unit in {"kcal", "cal", "kilocalorie", "kilocalories"}:
                    kcal, energy_source = aeb, "reported_kcal"
                elif aeb_unit in {"kj", "kilojoule", "kilojoules", ""}:
                    # HAE v2 defines workout activeEnergyBurned as kJ even when
                    # the field is emitted as a bare number; the conversion is
                    # therefore explicit and auditable rather than a magnitude guess.
                    kcal = aeb / 4.184
                    energy_source = "reported_kj"
                else:
                    kcal, energy_source = None, "unverified"
            else:
                kcal = None
            quality["energy"][energy_source] += 1
            intensity = _qty(w.get("intensity"))
            hr = w.get("heartRate") or w.get("heart_rate") or {}
            hr_avg = _qty(hr.get("avg", hr.get("average", hr.get("Avg")))) if isinstance(hr, dict) else None
            hr_max = _qty(hr.get("max", hr.get("Max"))) if isinstance(hr, dict) else None
            heart_rate_source = "reported" if hr_avg is not None or hr_max is not None else "missing"
            quality["heart_rate"][heart_rate_source] += 1
            indoor = w.get("indoor", w.get("isIndoor"))
            location = "室内" if indoor is True else ("户外" if indoor is False else "")
            out.append({
                "id": str(w.get("id") or ""), "date": st[:10], "name": w.get("name", ""),
                "start": st[11:16] if len(st) >= 16 else "",
                "duration_min": round(dur / 60, 1) if dur else None,
                "duration_source": duration_source, "distance_km": round(dist, 2) if dist else None,
                "distance_source": distance_source, "speed_kmh": round(spd, 1) if spd else None,
                "speed_source": speed_source, "elevation_up_m": round(elev, 1) if elev else None,
                "elevation_source": elevation_source, "kcal": round(kcal) if kcal is not None else None,
                "energy_source": energy_source, "heart_rate_avg": round(hr_avg) if hr_avg is not None else None,
                "heart_rate_max": round(hr_max) if hr_max is not None else None,
                "heart_rate_source": heart_rate_source, "location": location,
                "intensity": round(intensity, 2) if intensity is not None else None,
            })
        out.sort(key=lambda x: (x["date"], x.get("start") or ""), reverse=True)
        return out, quality

    # ── 睡眠调节派生指标（CBT-I 口径：睡眠债 / 睡眠效率 / 就寝相位 / 滴定熄灯 / 呼吸率）──
    SLEEP_TARGET_H = 7.5

    @staticmethod
    def _hm_to_h(s):
        """'2026-08-19 02:39:00 +0800' → 2.65（小时）；非法返回 None"""
        s = str(s or "")
        if len(s) < 16:
            return None
        try:
            return int(s[11:13]) + int(s[14:16]) / 60.0
        except ValueError:
            return None

    @staticmethod
    def _health_derived(d):
        """从健康聚合算睡眠调节派生指标（供看板 KPI / AI 随行 / 教练卡共用同一口径）"""
        out = {"sleep_debt": None, "sleep_eff": None, "avg_bedtime": None,
               "avg_wake": None, "rec_bedtime": None, "ideal_bedtime": None,
               "resp_last": None, "resp_avg": None, "training_load_7d": None,
               "training_load_28d": None}
        if not isinstance(d, dict):
            return out
        rides = d.get("workouts") or []
        if rides:
            # 训练窗口按自然日而非“前 N 条记录”计算。骑行是低频数据，按条数会把
            # 一次周末长骑误当成近 7 天负荷，也会在空档期把更早记录算进来。
            anchor = None
            try:
                day_values = [date.fromisoformat(str(x)[:10]) for x in (d.get("days") or []) if str(x)[:10]]
                if day_values:
                    anchor = max(day_values)
            except (TypeError, ValueError):
                anchor = None

            def _load(window):
                if anchor is None:
                    rows = rides[:window]
                else:
                    cutoff = anchor - timedelta(days=window - 1)
                    rows = []
                    for row in rides:
                        try:
                            row_day = date.fromisoformat(str(row.get("date", ""))[:10])
                        except (AttributeError, TypeError, ValueError):
                            continue
                        if cutoff <= row_day <= anchor:
                            rows.append(row)
                return round(sum((x.get("duration_min") or 0) * (x.get("intensity") or 0) for x in rows), 1) or None
            out["training_load_7d"] = _load(7)
            out["training_load_28d"] = _load(28)
        sleep = d.get("sleep") or []
        if sleep:
            # 近7晚睡眠债：每晚 total-7.5 正负累计（负=欠债）
            vals = [x.get("total") for x in sleep[-7:] if x.get("total") is not None]
            if vals:
                out["sleep_debt"] = round(sum(7.5 - v for v in vals), 1)
            # 就寝相位（凌晨归 +24 再平均，mod 24）：就寝<12点视为凌晨
            beds, wakes, effs = [], [], []
            for x in sleep[-7:]:
                b = DashboardHandler._hm_to_h(x.get("start"))
                w = DashboardHandler._hm_to_h(x.get("end"))
                if b is not None:
                    beds.append(b + 24 if b < 12 else b)
                if w is not None:
                    wakes.append(w)  # 起床恒为白天时刻
                t = x.get("total")
                if b is not None and w is not None and t is not None:
                    inbed = (w - b) % 24
                    if 2 < inbed < 16 and inbed >= t > 0:
                        effs.append(t / inbed * 100)
            if beds:
                out["avg_bedtime"] = round(sum(beds) / len(beds) % 24, 2)
            if wakes:
                out["avg_wake"] = round(sum(wakes) / len(wakes), 2)
            if effs:
                out["sleep_eff"] = round(sum(effs) / len(effs), 0)
            # CBT-I 睡眠窗口滴定：目标效率 85%（已达标则 95% 放宽），
            # 建议在床时长 = 近7晚实际平均睡眠 / 目标效率 → 熄灯 = 平均起床 - 在床时长
            if out["avg_wake"] is not None and vals and len(vals) >= 3:
                avg_total = sum(vals) / len(vals)
                eff = out["sleep_eff"] if out["sleep_eff"] is not None else 85
                target = 0.95 if eff >= 90 else 0.85
                inbed = min(9.0, max(avg_total / target, avg_total))  # 上限9h防无睡眠数据时爆表
                out["rec_bedtime"] = round((out["avg_wake"] - inbed) % 24, 2)
                # 目标相位：按每晚 7.5h 睡眠、85% 效率反推的理想熄灯时刻（每周前移 15-20 分钟逼近）
                out["ideal_bedtime"] = round((out["avg_wake"] - 7.5 / 0.85) % 24, 2)
        resp = d.get("resp") or []
        rv = [x.get("qty") for x in resp[-7:] if x.get("qty") is not None]
        if rv:
            out["resp_last"] = round(rv[-1], 1)
            out["resp_avg"] = round(sum(rv) / len(rv), 1)
        return out

    def _health_pack(self, agg, want, source):
        """把 agg（date -> {metric: point}）整理成前端友好的分指标数组"""
        days = sorted(want)
        sleep, heart, energy, exercise, mindful = [], [], [], [], []
        hrv, rhr, spo2, resp, steps, dist = [], [], [], [], [], []
        # HEALTH-ABC-A1: 新增指标——手腕温度 / 站立 / 身体负荷（HAE 已推送，此前未打包）
        wrist, stand, effort = [], [], []
        extra = {k: [] for k in HEALTH_EXTRA_KEYS}
        for d in days:
            m = agg.get(d, {})
            s = m.get("sleep_analysis")
            if s:
                sleep.append({"date": d,
                              "total": _f(s.get("totalSleep")), "core": _f(s.get("core")),
                              "rem": _f(s.get("rem")), "deep": _f(s.get("deep")),
                              "awake": _f(s.get("awake")),
                              "start": s.get("sleepStart", ""), "end": s.get("sleepEnd", "")})
            h = m.get("heart_rate")
            if h:
                heart.append({"date": d, "avg": _f(h.get("Avg")), "max": _f(h.get("Max")), "min": _f(h.get("Min"))})
            e = m.get("active_energy")
            if e:
                q = _normalise_health_quantity("active_energy", e)
                energy.append({"date": d, **q})
            x = m.get("apple_exercise_time")
            if x:
                exercise.append({"date": d, **_normalise_health_quantity("apple_exercise_time", x)})
            mf = m.get("mindful_minutes")
            if mf:
                mindful.append({"date": d, "qty": _health_number(mf.get("qty")), "units": _health_unit(mf), "source": "reported"})
            v = m.get("heart_rate_variability")
            if v:
                hrv.append({"date": d, "qty": _health_number(v.get("qty")), "units": _health_unit(v), "source": "reported"})
            r = m.get("resting_heart_rate")
            if r:
                rhr.append({"date": d, "qty": _health_number(r.get("qty")), "units": _health_unit(r), "source": "reported"})
            o = m.get("blood_oxygen_saturation")
            if o:
                spo2.append({"date": d, "qty": _health_number(o.get("qty")), "units": _health_unit(o), "source": "reported"})
            q = m.get("respiratory_rate")
            if q:
                resp.append({"date": d, "qty": _health_number(q.get("qty")), "units": _health_unit(q), "source": "reported"})
            sp = m.get("step_count")
            if sp:
                steps.append({"date": d, "qty": _health_number(sp.get("qty")), "units": _health_unit(sp), "source": "reported"})
            di = m.get("walking_running_distance")
            if di:
                dist.append({"date": d, **_normalise_health_quantity("walking_running_distance", di)})
            # HEALTH-ABC-A1: 手腕温度(degC) / 站立小时(count) / 身体负荷(kcal/hr·kg)
            wt = m.get("apple_sleeping_wrist_temperature")
            if wt:
                wrist.append({"date": d, "qty": _health_number(wt.get("qty")), "units": _health_unit(wt), "source": "reported"})
            sh = m.get("apple_stand_hour")
            if sh:
                stand.append({"date": d, "qty": _health_number(sh.get("qty")), "units": _health_unit(sh), "source": "reported"})
            pe = m.get("physical_effort")
            if pe:
                effort.append({"date": d, "qty": _health_number(pe.get("qty")), "units": _health_unit(pe), "source": "reported"})
            for name, rows in extra.items():
                z = m.get(name)
                if z:
                    if name in {"basal_energy_burned", "cycling_distance"}:
                        nq = _normalise_health_quantity(name, z)
                        qty, units, unit_source = nq["qty"], nq["units"], nq["source"]
                    else:
                        qty = _health_number(z.get("qty"))
                        units = str(z.get("units") or "").strip()
                        unit_source = "reported"
                    # 对外统一少数会影响阅读的单位；保留其余 HAE 原始单位供前端显示。
                    unit_key = units.lower()
                    if name in {"basal_energy_burned", "active_energy"} and unit_key in {"kj", "kilojoule", "kilojoules"}:
                        qty = round(qty / 4.184, 1) if qty is not None else None
                        units = "kcal"
                    elif name == "cycling_distance" and unit_key in {"m", "meter", "meters"}:
                        qty = round(qty / 1000, 2) if qty is not None else None
                        units = "km"
                    elif unit_key == "km/hr":
                        units = "km/h"
                    elif unit_key == "dbaspl":
                        units = "dB"
                    elif qty is not None and name in {"basal_energy_burned", "active_energy", "cycling_distance"} and not unit_key:
                        unit_source = "unverified"
                    rows.append({"date": d, "qty": qty, "units": units,
                                 "source": unit_source, "avg": _f(z.get("Avg")),
                                 "max": _f(z.get("Max")), "min": _f(z.get("Min"))})
        return {"source": source, "days": days, "sleep": sleep, "heart": heart,
                "energy": energy, "exercise": exercise, "mindful": mindful,
                "hrv": hrv, "rhr": rhr, "spo2": spo2, "resp": resp,
                "steps": steps, "dist": dist,
                # HEALTH-ABC-A1: 新增指标进返回结构
                "wrist": wrist, "stand": stand, "effort": effort,
                "extra_keys": list(HEALTH_EXTRA_KEYS), **extra}

    # ── 骑行 workouts 已在 _health_from_local 中从 HAE data.workouts 聚合并规范化 ──

    # ── 健康 AI 伴随：readiness 恢复度 + 管家/神谕/教练卡共用块 ──

    @staticmethod
    def _health_readiness(d):
        """当日恢复度 v3（HEALTH-ABC-C1，对齐 Google/Oura 口径）：
        睡眠25 + 就寝规律10 + HRV25(个人基线,主角) + 静息心率15(基线)
        + 手腕温度10(基线偏离,生病/压力信号) + 睡眠债5(近14晚) + 活动5 + 锻炼5 = 100
        原则：HRV/静息心率/手腕温度是「身体响应」生理主角；睡眠/活动只做解释上下文——避免双重惩罚"""
        if not isinstance(d, dict) or d.get("source") not in ("vps", "proxy"):
            return {"available": False}
        sleep = d.get("sleep") or []
        if not sleep:
            return {"available": False}
        last = sleep[-1]
        factors, score = [], 0
        t = last.get("total")
        if t is not None:
            pts = round(min(25, t / 7.5 * 25))
            score += pts
            factors.append(("睡眠时长", pts, 25, "%.1fh" % t))
        st = str(last.get("start") or "")
        if len(st) >= 16:
            hh, mm = int(st[11:13]), int(st[14:16])
            if hh in (20, 21, 22) or (hh == 23 and mm <= 30):
                reg = 10
            elif hh in (23, 0):
                reg = 7
            elif hh == 1:
                reg = 4
            else:
                reg = 2
            score += reg
            factors.append(("就寝规律", reg, 10, "%s就寝" % st[11:16]))
        # HRV：今日值 vs 个人基线（均值，全窗数据排除当天），高出/持平满分，每低5%扣1分——恢复度主角
        hrv = d.get("hrv") or []
        if len(hrv) >= 2 and hrv[-1].get("qty") is not None:
            vals = [x["qty"] for x in hrv if x.get("qty") is not None]
            if len(vals) >= 2:
                base = sum(vals[:-1]) / (len(vals) - 1)
                cur = vals[-1]
                if base > 0:
                    dev = (cur - base) / base
                    pts = 25 if dev >= 0 else max(0, round(25 + dev * 5))
                    score += pts
                    factors.append(("HRV", pts, 25, "%.0fms（基线%.0f）" % (cur, base)))
        # 静息心率：今日 vs 个人基线，低于/持平满分，每高1bpm扣2分
        rhr = d.get("rhr") or []
        if len(rhr) >= 2 and rhr[-1].get("qty") is not None:
            vals = [x["qty"] for x in rhr if x.get("qty") is not None]
            if len(vals) >= 2:
                base = sum(vals[:-1]) / (len(vals) - 1)
                cur = vals[-1]
                dev = cur - base
                pts = 15 if dev <= 0 else max(0, round(15 - dev * 2))
                score += pts
                factors.append(("静息心率", pts, 15, "%.0fbpm（基线%.0f）" % (cur, base)))
        # HEALTH-ABC-C1: 手腕温度 10——vs 个人基线偏离（Oura：生病/压力第一信号；≥0.3℃ 开始扣分）
        wt = d.get("wrist") or []
        if len(wt) >= 2 and wt[-1].get("qty") is not None:
            vals = [x["qty"] for x in wt if x.get("qty") is not None]
            if len(vals) >= 2:
                base = sum(vals[:-1]) / (len(vals) - 1)
                cur = vals[-1]
                dev = cur - base
                pts = 10 if abs(dev) < 0.3 else (5 if abs(dev) < 0.5 else 0)
                score += pts
                factors.append(("手腕温度", pts, 10, "%+.2f℃（基线%.1f）" % (dev, base)))
        # HEALTH-ABC-C1: 睡眠债 5——近14晚累计缺口（正=欠债；Oura Sleep Balance 概念）
        tvals = [x.get("total") for x in sleep[-14:] if x.get("total") is not None]
        if tvals:
            debt = sum(7.5 - v for v in tvals)
            pts = 5 if debt <= 2.5 else (3 if debt <= 5 else 0)
            score += pts
            factors.append(("睡眠债", pts, 5, "%.1fh（近14晚）" % debt))
        def _yest(arr):
            days = d.get("days") or []
            if len(days) < 2:
                return None
            yd = days[-2]
            for x in arr or []:
                if x.get("date") == yd:
                    return x
            return None
        ye = _yest(d.get("energy"))
        if ye and ye.get("qty") is not None:
            pts = round(min(5, ye["qty"] / 500 * 5))
            score += pts
            factors.append(("活动能量", pts, 5, "%.0fkcal" % ye["qty"]))
        yx = _yest(d.get("exercise"))
        if yx and yx.get("qty") is not None:
            pts = round(min(5, yx["qty"] / 30 * 5))
            score += pts
            factors.append(("锻炼", pts, 5, "%.0fmin" % yx["qty"]))
        # HEALTH-ABC-C1: 正念不再计入恢复度（正念是干预手段而非恢复信号，避免稀释生理主角）
        # 缺项归一：数据不足的项（如HRV）按已有项比例折算，避免长期压分
        if factors:
            got = sum(f[2] for f in factors)
            if got > 0 and got < 100:
                score = round(score * 100 / got)
        weak = ""
        if factors:
            worst = min(factors, key=lambda f: f[1] / f[2] if f[2] else 1)
            if worst[1] / worst[2] < 0.5:
                weak = worst[0]
        return {"available": True, "score": min(100, score), "factors": factors,
                "weak": weak, "sleep_date": last.get("date", "")}

    def _health_block_for_butler(self):
        """管家上下文的健康块：客观数据 + 解读原则（解释模式而非复述数字）"""
        try:
            d = self._get_health(7)
        except Exception:
            return ""
        if not isinstance(d, dict) or d.get("source") not in ("vps", "proxy"):
            return ""
        sleep = d.get("sleep") or []
        lines = []
        if sleep:
            last = sleep[-1]
            seg = ""
            if last.get("deep") is not None:
                seg += "，深睡%.1fh" % last["deep"]
            if last.get("rem") is not None:
                seg += "/REM%.1fh" % last["rem"]
            st, en = str(last.get("start") or ""), str(last.get("end") or "")
            win = ("（%s→%s）" % (st[11:16], en[11:16])) if len(st) >= 16 and len(en) >= 16 else ""
            tot = last.get("total")
            lines.append("  · 最近睡眠（%s）：%.1fh%s%s" % (
                (last.get("date") or "")[5:], tot if tot is not None else 0, seg, win))
            vals = [x["total"] for x in sleep if x.get("total") is not None]
            if len(vals) >= 3:
                lines.append("  · 近%d天平均睡眠 %.1fh（最低 %.1fh，共 %d 晚有记录）" % (
                    len(vals), sum(vals) / len(vals), min(vals), len(vals)))
            lows = [v for v in vals if v < 6]
            if len(lows) >= 2:
                lines.append("  · ⚠️ 其中 %d 晚不足 6 小时" % len(lows))
        hr = d.get("heart") or []
        if hr:
            avgs = [x["avg"] for x in hr if x.get("avg") is not None]
            if avgs:
                lines.append("  · 近%d天平均心率 %.0f bpm（区间 %.0f-%.0f）" % (
                    len(avgs), sum(avgs) / len(avgs), min(avgs), max(avgs)))
        hrv = d.get("hrv") or []
        if hrv:
            vals = [x["qty"] for x in hrv if x.get("qty") is not None]
            if len(vals) >= 2:
                cur = vals[-1]
                base = sum(vals[:-1]) / (len(vals) - 1)
                dev = (cur - base) / base * 100 if base else 0
                lines.append("  · HRV 昨日 %.0fms（7天基线 %.0fms，%s%.0f%%）%s" % (
                    cur, base, "↑" if dev >= 0 else "↓", abs(dev),
                    "——自主神经恢复中" if dev >= 0 else "——压力/疲劳在积累"))
        rhr = d.get("rhr") or []
        if rhr:
            vals = [x["qty"] for x in rhr if x.get("qty") is not None]
            if len(vals) >= 2:
                cur, base = vals[-1], sum(vals[:-1]) / (len(vals) - 1)
                tag = "良好" if cur <= base else ("偏高 %.0fbpm" % (cur - base))
                lines.append("  · 静息心率 %.0f bpm（基线 %.0f，%s）" % (cur, base, tag))
        spo2 = d.get("spo2") or []
        if spo2:
            v = spo2[-1].get("qty")
            if v is not None:
                lines.append("  · 血氧 %.0f%%%s" % (v, "（偏低，建议关注）" if v < 95 else ""))
        wo = d.get("workouts") or []
        if wo:
            last_w = wo[-1]
            # HEALTH-ABC-E1: 字段对齐 _pack_rides（distance_km/duration_min/speed_kmh/intensity）
            lines.append("  · 最近骑行：%s %.1fkm / %dmin / 均速 %.1fkm/h / 强度 %s" % (
                last_w.get("date", "")[5:], last_w.get("distance_km") or 0, last_w.get("duration_min") or 0,
                last_w.get("speed_kmh") or 0, last_w.get("intensity") or "—"))
        rd = self._health_readiness(d)
        if rd.get("available"):
            weak = ("，弱项：%s" % rd["weak"]) if rd.get("weak") else ""
            lines.append("  · 今日恢复度 readiness %d/100%s" % (rd["score"], weak))
        # 睡眠调节派生（CBT-I 口径）：睡眠债 / 呼吸率 / 滴定熄灯
        try:
            dv = d.get("derived") or self._health_derived(d)
        except Exception:
            dv = {}
        _fh = lambda h: "%02d:%02d" % (int(h) % 24, round((h % 1) * 60)) if h is not None else "—"
        if dv.get("sleep_debt") is not None:
            tag = "（债在累积，优先补觉）" if dv["sleep_debt"] >= 5 else ""
            lines.append("  · 近7晚睡眠债 %.1fh%s" % (dv["sleep_debt"], tag))
        if dv.get("sleep_eff") is not None:
            lines.append("  · 睡眠效率 %.0f%%（总睡眠/在床，CBT-I 目标 ≥85%%）%s" % (
                dv["sleep_eff"], "——床太宽裕，可滴定收紧" if dv["sleep_eff"] < 85 else ""))
        if dv.get("avg_bedtime") is not None:
            lines.append("  · 平均就寝 %s / 起床 %s" % (_fh(dv.get("avg_bedtime")), _fh(dv.get("avg_wake"))))
        if dv.get("rec_bedtime") is not None:
            lines.append("  · 按睡眠效率滴定，建议今晚熄灯 %s（不困不上床，困了立刻熄灯）" % _fh(dv["rec_bedtime"]))
        if dv.get("ideal_bedtime") is not None and dv.get("rec_bedtime") is not None:
            lines.append("  · 目标相位 %s：从滴定熄灯起每周前移 15-20 分钟逼近（配合早晨固定时间晒光）" % _fh(dv["ideal_bedtime"]))
        if dv.get("resp_last") is not None:
            lines.append("  · 呼吸率 %.1f 次/分（7天均 %.1f）%s" % (
                dv["resp_last"], dv.get("resp_avg") or dv["resp_last"],
                "——偏快，副交感不足，可做共振呼吸（约6次/分）" if (dv.get("resp_avg") or 0) >= 14.5 else ""))
        try:
            notes = _read_json(HEALTH_NOTES_FILE, [])
            recent = [n for n in notes if n.get("note")][-3:]
            if recent:
                lines.append("  · 睡眠主观备注（最新在前）：" + "；".join(
                    "%s「%s」" % (str(n.get("date", ""))[5:], n["note"][:40]) for n in reversed(recent)))
        except Exception:
            pass
        # 健康预警（与前端预警条同源，让管家主动点破身体矛盾）
        try:
            alerts = self._health_alerts(d)
            if alerts:
                lines.append("  · ⚠️ 当前健康预警：" + "；".join(a["text"] for a in alerts))
        except Exception:
            pass
        try:
            lines.append("  · 数据质量：" + self._health_quality_context(d))
        except Exception:
            pass
        if not lines:
            return ""
        return ("【健康（Apple Watch 实测）】\n%s\n"
                "引用健康数据时解读含义和对今天的影响（如「昨晚只睡3.7h，上午的认知型要事建议轻量化」），"
                "不要只复述数字。存在健康预警时优先主动提及（用户可能没看到健康页的预警条）。\n"
                "【健康解读口径（医学级）】成人睡眠 7-9h；深睡≈15-25%%、REM≈20-25%%；HRV 只看相对自己基线的稳定（个体差异极大）；"
                "静息心率连续 3-5 天较基线 +5~10bpm=身体硬扛；手腕温度偏离基线 ≥0.3℃=应激信号（生病/酒精/深夜大餐/高强度训练）、"
                "≥0.5℃ 且伴 HRV 降+静息心率升=疑似生病前兆；呼吸率 12-20 次/分。"
                "HRV/静息心率/手腕温度是身体响应主角，睡眠/活动只作上下文，避免双重惩罚；"
                "出现异常信号必须给明确提示或就医指征（如「连续多日如此建议就医」）。\n\n" % "\n".join(lines))

    # HEALTH-ABC-E2: 健康顾问的组合解读上下文——个人基线 + 趋势信号 + 组合信号 + 数据质量。
    # 供 advisor_stream 的 health 场景注入，让 AI 用「组合逻辑 + 个人基线」解读，而非单指标罗列。
    def _health_advisor_context(self):
        try:
            d = self._get_health(14)
        except Exception:
            return ""
        if not isinstance(d, dict) or not any(isinstance(d.get(k), list) and d.get(k)
                                               for k in ("sleep", "hrv", "rhr", "wrist", "workouts",
                                                         "energy", "exercise", "steps", "spo2", "resp")):
            return ""

        def _base(arr):
            vals = [x.get("qty") for x in arr if x.get("qty") is not None]
            if len(vals) < 2:
                return None, None
            return vals[-1], sum(vals[:-1]) / (len(vals) - 1)

        lines = []
        hrv = d.get("hrv") or []
        rhr = d.get("rhr") or []
        wt = d.get("wrist") or []
        # 先建立扩展指标/锻炼引用；这些变量同时用于基线和组合信号。
        # 旧代码在此处直接引用 vo2/rides，导致健康顾问上下文抛 UnboundLocalError，整段被上层吞掉。
        vo2 = d.get("vo2_max") or []
        rides = d.get("workouts") or []
        ingest = d.get("ingest") or {}
        if not isinstance(ingest, dict):
            ingest = {}
        # 1) 个人基线 + 偏离
        hcur, hbase = _base(hrv)
        rcur, rbase = _base(rhr)
        wcur, wbase = _base(wt)
        bits = []
        if hcur is not None and hbase:
            dev = (hcur - hbase) / hbase * 100
            bits.append("HRV %.0fms（基线%.0f，%s%.0f%%）" % (hcur, hbase, "+" if dev >= 0 else "", dev))
        if rcur is not None and rbase:
            bits.append("静息心率 %.0fbpm（基线%.0f，%s%.0f）" % (rcur, rbase, "+" if rcur >= rbase else "", rcur - rbase))
        if wcur is not None and wbase:
            bits.append("手腕温度 %.1f℃（基线%.1f，%+.2f℃）" % (wcur, wbase, wcur - wbase))
        if vo2:
            v = vo2[-1].get("qty")
            if v is not None: bits.append("VO₂ Max %.1f ml/kg·min" % v)
        if rides:
            km = sum((x.get("distance_km") or 0) for x in rides)
            mins = sum((x.get("duration_min") or 0) for x in rides)
            if km or mins: bits.append("近14天骑行 %.1fkm/%d分钟" % (km, round(mins)))
        if bits:
            lines.append("【个人基线（近14天，排除当天）】" + "；".join(bits))
        # 数据证据边界：AI 必须知道某指标是 HAE 没推、在窗口外，还是解析映射异常，
        # 不能把缺失指标用常模或上一轮对话里的数字补猜。
        if isinstance(ingest, dict) and isinstance(ingest.get("metric_names"), list):
            raw_names = set(str(x) for x in ingest.get("metric_names") if x)
            raw_dates = ingest.get("metric_latest_data_date") or {}
            window_start = str((d.get("days") or [""])[0])[:10]
            missing_specs = (
                ("rhr", "静息心率", "resting_heart_rate"),
                ("resp", "呼吸率", "respiratory_rate"),
                ("mindful", "正念", "mindful_minutes"),
                ("vo2_max", "VO₂ Max", "vo2_max"),
                ("cycling_distance", "骑行距离指标", "cycling_distance"),
            )
            not_observed, outside, parser_gap = [], [], []
            for key, label, raw_key in missing_specs:
                if isinstance(d.get(key), list) and d.get(key):
                    continue
                if raw_key not in raw_names:
                    not_observed.append(label + "（HAE 未推送）")
                else:
                    raw_date = str(raw_dates.get(raw_key) or "")[:10]
                    if window_start and raw_date and raw_date < window_start:
                        outside.append(label + "（原始最新日期在当前窗口外）")
                    else:
                        parser_gap.append(label + "（原始有数据但当前未解析）")
            if not_observed or outside or parser_gap:
                bits = []
                if not_observed: bits.append("；".join(not_observed))
                if outside: bits.append("；".join(outside))
                if parser_gap: bits.append("；".join(parser_gap))
                lines.append("【未观测指标】" + "；".join(bits))
        # 2) 趋势信号（近5天里最近3天同向）
        def _trend(arr):
            vals = [x.get("qty") for x in arr if x.get("qty") is not None][-5:]
            if len(vals) < 3:
                return None
            seq = vals[-3:]
            if all(seq[i] < seq[i - 1] for i in range(1, len(seq))):
                return "连续%d天下降" % len(seq)
            if all(seq[i] > seq[i - 1] for i in range(1, len(seq))):
                return "连续%d天上升" % len(seq)
            return None
        t = []
        if hcur is not None:
            tv = _trend(hrv)
            if tv:
                t.append("HRV %s（%s）" % (tv, "恢复走弱" if "下降" in tv else ""))
        if rcur is not None:
            tv = _trend(rhr)
            if tv:
                t.append("静息心率 %s（%s）" % (tv, "身体在扛/生病前兆" if "上升" in tv else ""))
        if wcur is not None:
            tv = _trend(wt)
            if tv:
                t.append("手腕温度 %s" % tv)
        if t:
            lines.append("【趋势信号】" + "；".join(t))
        # 3) 组合解读信号（医学逻辑）
        combos = []
        if wcur is not None and wbase and (wcur - wbase) >= 0.5:
            c = "⚠️ 手腕温度偏离基线 ≥0.5℃"
            if hcur is not None and hbase and hcur < hbase:
                c += " + HRV 低于基线"
            if rcur is not None and rbase and rcur > rbase:
                c += " + 静息心率高于基线"
            c += " = 疑似生病/过度疲劳早期信号（早于症状），建议优先休息观察"
            combos.append(c)
        elif wcur is not None and wbase and (wcur - wbase) >= 0.3:
            combos.append("⚠️ 手腕温度偏离基线 ≥0.3℃（应激信号：生病前兆/酒精/深夜大餐/高强度训练）")
        if hcur is not None and hbase and rcur is not None and rbase and hcur >= hbase and rcur <= rbase:
            combos.append("✅ HRV 在基线之上 + 静息心率不高于基线 = 恢复良好，可正常强度")
        rides = (d.get("workouts") or [])[:3]
        if rides:
            combos.append("近3天有 %d 次骑行（最近 %s）——若次日 HRV 略降+深睡充足属正常训练适应，"
                          "HRV 连续 2-3 天走低才需减量" % (len(rides), rides[0].get("date", "")))
        sd = (d.get("derived") or {}).get("sleep_debt")
        if sd is not None and sd > 5:
            combos.append("⚠️ 睡眠债 %.1fh（>5h 高债，优先补睡，训练降强度）" % sd)
        resp = [x.get("qty") for x in (d.get("resp") or []) if x.get("qty") is not None]
        if resp and resp[-1] > 14.5:
            combos.append("⚠️ 呼吸率 %.1f 次/分（>14.5 偏快，副交感缺席，建议共振呼吸练习）" % resp[-1])
        stand = [x.get("qty") for x in (d.get("stand") or []) if x.get("qty") is not None]
        if stand and stand[-1] < 4:
            combos.append("🧍 站立仅 %.0f 小时（<4h，久坐风险，建议每 45 分钟起身 2 分钟）" % stand[-1])
        if combos:
            lines.append("【组合解读信号】" + "；".join(combos))
        # 4) 数据质量感知：与健康页覆盖诊断共用后端口径，避免 UI 与 AI 各说一套。
        lines.append("【数据质量】" + self._health_quality_context(d))
        # 训练负荷使用服务端统一口径，避免 AI 自己按“前几条记录”重新估算。
        try:
            dv = d.get("derived") or self._health_derived(d)
            loads = []
            if dv.get("training_load_7d") is not None:
                loads.append("近7天 %.1f AU" % dv["training_load_7d"])
            if dv.get("training_load_28d") is not None:
                loads.append("近28天 %.1f AU" % dv["training_load_28d"])
            if loads:
                lines.append("【训练负荷】" + "；".join(loads) + "（时长×强度，仅供相对趋势，不作医学诊断）")
        except Exception:
            pass
        fresh = []
        for label, key in (("健康", "health_fetched_at"), ("锻炼", "workout_fetched_at")):
            if d.get(key):
                fresh.append("%s %s" % (label, str(d[key])[:16]))
        if ingest.get("latest_receive_at"):
            fresh.append("原始推送 %s" % str(ingest["latest_receive_at"])[:16])
        if fresh:
            lines.append("【数据新鲜度】" + "；".join(fresh) + "；未列出的指标视为当前窗口无记录，不要补猜")
        return "\n".join(lines)


    ORACLE_FILE = _dd_expanduser_default("DASH_ORACLE_FILE", "AI夜观", "oracle.json")

    def _health_fingerprint(self):
        """健康数据指纹：最新一晚睡眠的 date+total。
        简报/神谕按日缓存据此失效——当日新一晚睡眠推上来 → 指纹变 → 重新生成解读，避免整天读旧数据。"""
        try:
            _d = self._get_health(7)
            if not isinstance(_d, dict):
                return ""
            _parts = []
            _sl = _d.get("sleep") or []
            if _sl:
                _l = _sl[-1]
                _parts.append("%s_%.1f" % (_l.get("date") or "", _f(_l.get("total")) or 0))
            # 指纹包含关键指标最新值，数据更新时触发重生成，
            # 修「上午判断晚上还挂着」：简报/神谕白天不再读旧解读
            for _fk in ("hrv", "wrist", "rhr"):
                _arr = _d.get(_fk) or []
                if _arr:
                    _v = _arr[-1].get("qty")
                    _parts.append("%s=%.1f" % (_fk, _v or 0))
            # 新锻炼到达时也必须让当日神谕失效；否则“今天刚骑完车”的负荷不会进入夜观。
            # 只取最新记录和同步时间，避免把每个高频活动点都变成一次 LLM 缓存失效。
            _wo = _d.get("workouts") or []
            if _wo:
                _w = _wo[0]
                _parts.append("workout=%s|%s|%.2f|%.1f" % (
                    _w.get("date") or "", _w.get("start") or "",
                    _w.get("distance_km") or 0, _w.get("duration_min") or 0))
            if _d.get("workout_fetched_at"):
                _parts.append("workout_fetched=%s" % str(_d["workout_fetched_at"])[:16])
            return "_".join(_parts)
        except Exception:
            pass
        return ""

    def _get_ai_oracle(self, force=False):
        """读取 AI 夜观神谕；缺失/过期/强制时自动生成（配置了 ARK 走模型，否则规则兜底）"""
        if _demo_enabled():
            return {"demo": True, "available": False, "oracle": "", "habit_review": "", "focus_tip": "",
                    "reason": "演示模式未配置 LLM。配置 LLM_API_KEY 后可生成晨间神谕。"}
        # P2-V12: force 刷新也加 10 分钟冷却，防前端连点 /api/ai/oracle/refresh 反复触发付费 LLM
        ORACLE_COOLDOWN = 600
        # 数据指纹：最新一晚睡眠变化 → 缓存失效重新生成（修"解读滞后"：凌晨生成的旧神谕整天不更新）
        _fp = self._health_fingerprint()
        if force:
            try:
                st = os.stat(self.ORACLE_FILE)
                if time.time() - st.st_mtime < ORACLE_COOLDOWN:
                    with open(self.ORACLE_FILE, encoding="utf-8") as f:
                        _d = json.load(f)
                    if _d.get("oracle") and _d.get("fp") == _fp:
                        return self._force_ppc_brief(_d)
            except Exception:
                pass
        if not force:
            try:
                with open(self.ORACLE_FILE, encoding="utf-8") as f:
                    d = json.load(f)
                if (d.get("date") == date.today().isoformat() and d.get("oracle")
                        and d.get("fp") == _fp):
                    return self._force_ppc_brief(d)
            except Exception:
                pass
        d = self._generate_oracle()
        d["fp"] = _fp
        if not _write_json(self.ORACLE_FILE, d):
            d["persistence_warning"] = "本次建议未保存，请稍后重试"
        return self._force_ppc_brief(d)

    def _force_ppc_brief(self, d):
        """统一 P/PC 口径：神谕的 ppc_brief 强制用规则卡实时值，避免与 AI 生成数字不一致"""
        try:
            _ppc = (self._dash().get("chain_health") or {}).get("ppc") or {}
            if _ppc.get("available"):
                d["ppc_brief"] = _ppc.get("diagnosis", "")
        except Exception:
            pass
        return d

    def _generate_oracle(self):
        """生成今日神谕：优先 LLM（配置了 LLM_API_KEY/LLM_API_BASE），失败/未配置则规则生成"""
        if _cfg.get("LLM_API_KEY") and _cfg.get("LLM_API_BASE"):
            try:
                return self._oracle_via_llm()
            except Exception:
                pass
        return self._oracle_rule_based()

    def _oracle_rule_based(self):
        """规则神谕：当天数据驱动的三段式（神谕/习惯点评/今日关注）"""
        today_d = date.today()
        habits, tasks = [], []
        quotes = [
            "最重要的事，是让重要的事保持重要。",
            "把时间投资在第二象限，改变才会发生。",
            "完成比完美重要，开始比完成重要。",
            "习惯是知识的复利，每天进步 1%。",
            "先处理最重要的一件事，其余都会变得更容易。",
            "你不是在等待灵感，而是在积累行动的势能。",
            "砍掉 20% 的琐事，才能专注 80% 的要事。",
            "今天的行动，是明天的回响。",
            "少即是多：删除一件不重要的事，胜过新增三件。",
            "保持简单，保持专注，保持推进。",
        ]
        oracle = quotes[today_d.toordinal() % len(quotes)]
        # 习惯点评
        habit_review = ""
        try:
            habits = self._habits().get("habits", [])
            if habits:
                top = max(habits, key=lambda h: h.get("streak", 0))
                unchecked = [h["name"] for h in habits if not h.get("checked")]
                if top and top.get("streak", 0) >= 3:
                    habit_review = f"🔥 「{top['name']}」已连续 {top['streak']} 天，继续保持。"
                elif unchecked:
                    habit_review = f"今日还有 {len(unchecked)} 项习惯未打卡：{'、'.join(unchecked[:3])}。"
                else:
                    habit_review = "今日习惯全部打卡，漂亮。"
        except Exception:
            pass
        # 今日关注
        focus_tip = ""
        try:
            tasks = (self._dash().get("tasks") or [])
            overdue = [t for t in tasks if t.get("due") and t["due"][:10] < today_d.isoformat()]
            due_today = [t for t in tasks if t.get("due") and t["due"][:10] == today_d.isoformat()]
            if overdue:
                focus_tip = f"⚠️ 有 {len(overdue)} 项逾期，先处理「{overdue[0]['title']}」或决定放弃它。"
            elif due_today:
                focus_tip = f"🎯 今日到期：「{due_today[0]['title']}」，优先完成。"
            else:
                high = [t for t in tasks if t.get("priority", 3) >= 5]
                if high:
                    focus_tip = f"⚡ 高优先级待办：「{high[0]['title']}」。"
        except Exception:
            pass
        # 晨间简报补充段：①睡眠/恢复度 ②P/PC 诊断（规则口径，数据驱动）
        sleep_brief, ppc_brief = "", ""
        try:
            _hd = self._get_health(7)
            _rd = self._health_readiness(_hd)
            if _rd.get("available"):
                _last_sl = (_hd.get("sleep") or [{}])[-1].get("total")
                sleep_brief = (f"昨晚睡眠 {_last_sl:.1f}h，今日恢复度 {_rd['score']}/100"
                               + (f"，弱项：{_rd['weak']}" if _rd.get("weak") else "")
                               + ("——睡眠不足，今天的认知型要事建议轻量化。" if _last_sl < 6.5 else ""))
        except Exception:
            pass
        try:
            _ppc = (self._dash().get("chain_health") or {}).get("ppc") or {}
            if _ppc.get("available"):
                ppc_brief = (f"产出 {_ppc.get('p')}/100 · 产能 {_ppc.get('pc')}/100"
                             f"——{_ppc.get('diagnosis', '')}")
        except Exception:
            pass
        return {"oracle": oracle, "habit_review": habit_review, "focus_tip": focus_tip,
                "sleep_brief": sleep_brief, "ppc_brief": ppc_brief,
                "date": today_d.isoformat(), "ts": datetime.now().isoformat(timespec="seconds"),
                "available": True, "source": "rule",
                "evidence": build_evidence("oracle", {"tasks": tasks, "habits": habits},
                                            window_days=7, source="rules")}

    def _oracle_via_llm(self):
        """调用统一 _call_llm（LLM_API_* 配置，json_mode）生成晨间简报神谕；失败抛异常由上层兜底
        固定三段：①昨晚睡眠/恢复度 ②P/PC 失衡诊断 ③今日 1 个焦点（高优/大石头）+ 一句话神谕"""
        today_d = date.today()
        tasks = (self._dash().get("tasks") or [])
        habits = self._habits().get("habits", [])
        roles = (self._dash().get("roles") or [])
        overdue = [t for t in tasks if t.get("due") and t["due"][:10] < today_d.isoformat()]
        habit_line = "；".join(f"{h['name']}连续{h.get('streak',0)}天" for h in habits[:6]) or "暂无习惯数据"
        try:
            _hd = self._get_health(7)
            _rd = self._health_readiness(_hd)
            _last_sl = (_hd.get("sleep") or [{}])[-1].get("total")
            health_line = ("" if not _rd.get("available")
                           else f"昨晚睡眠{_last_sl:.1f}h、今日恢复度{_rd['score']}/100"
                           + (f"（弱项{_rd['weak']}）" if _rd.get("weak") else "") + "；\n")
            # 把健康页同一套质量判断带入晨间神谕，避免缺失/陈旧数据被当成正常值解读。
            health_line += "数据质量：" + self._health_quality_context(_hd) + "；\n"
        except Exception:
            health_line = "暂无健康数据；\n"
        # P/PC 失衡诊断（复用开屏卡/管家同一算分点）
        ppc_line = ""
        try:
            ppc = (self._dash().get("chain_health") or {}).get("ppc") or {}
            if ppc.get("available"):
                ppc_line = (f"P/PC：产出{ppc.get('p')}/100、产能{ppc.get('pc')}/100，"
                            f"失衡度{ppc.get('imbalance')}（{ppc.get('state','')}）；"
                            f"诊断：{ppc.get('diagnosis','')}\n")
        except Exception:
            ppc_line = ""
        prompt = (
            prompt_header("oracle") +
            f"今天是{today_d.isoformat()}。你是{USER_NAME}的个人驾驶舱助手，给他一份「晨间简报」，"
            "固定三段：①昨晚睡眠/恢复度解读（引用睡眠小时数，给一句对今天安排的影响，如睡眠不足则提醒轻量化）；"
            "②P/PC 失衡诊断（引用给出的产出/产能数字，给一句最该补哪块产能的动作）；"
            "③今日 1 个焦点（从高优先级/逾期/大石头任务里挑一件，给具体行动）。"
            "最后附一句话「今日神谕」（温柔但有力量的中文哲理）。"
            "数据质量若提示未收到、陈旧或异常，不得把缺失数据当作正常，必须在对应解读中明确说明。\n"
            f"未完成任务 {len(tasks)} 项，其中逾期 {len(overdue)} 项"
            + (f"（如「{overdue[0]['title']}」）" if overdue else "") + "；\n"
            f"习惯：{habit_line}；\n"
            f"{health_line}"
            f"{ppc_line}"
            f"角色：{'、'.join(r['name'] for r in roles) or '无'}。\n"
            '请只输出 JSON：{"oracle":"...","sleep_brief":"...","ppc_brief":"...","focus_tip":"..."}'
        )
        r = _call_llm([{"role": "user", "content": prompt}],
                      temperature=0.8, max_tokens=1400, json_mode=True, feature="oracle")
        content = r.get("content") or ""
        if not content or "error" in r:
            raise RuntimeError(r.get("error") or "LLM 未返回内容")
        m = re.search(r"\{.*\}", content, re.DOTALL)
        obj = json.loads(m.group(0)) if m else {}
        return {"oracle": obj.get("oracle", ""), "sleep_brief": obj.get("sleep_brief", ""),
                "ppc_brief": obj.get("ppc_brief", ""), "focus_tip": obj.get("focus_tip", ""),
                "date": today_d.isoformat(),
                "ts": datetime.now().isoformat(timespec="seconds"), "available": True, "source": "llm",
                "evidence": build_evidence("oracle", {"tasks": tasks, "habits": habits, "roles": roles},
                                            window_days=7, source="dashboard_snapshot")}

    def _get_stats(self):
        """今日已完成任务（TickTick completedTime 口径）"""
        if _demo_enabled():
            d = _demo()
            done = [t for t in d.get("tasks", []) if t.get("status") == 2]
            return {"demo": True, "date": date.today().isoformat(),
                    "today_completed": len(done), "titles": [t["title"] for t in done]}
        today = date.today().isoformat()
        r = call_ticktick_mcp("list_completed_tasks_by_date", {"search": {
            "projectIds": list(PROJECT_IDS.values()),
            "startDate": today + "T00:00:00+08:00",
            "endDate": today + "T23:59:59+08:00"}})
        titles = []
        if "result" in r:
            for t in (r["result"].get("structuredContent", {}).get("result") or []):
                titles.append(t.get("title", ""))
        return {"today_completed": len(titles), "titles": titles[:12], "date": today}

    def _get_week_report(self):
        """本周战报：近7天每日完成数 + 本周完成标题"""
        if _demo_enabled():
            d = _demo()
            done = [t for t in d.get("tasks", []) if t.get("status") == 2]
            from collections import Counter
            by_day = Counter(str(t.get("completedTime", ""))[:10] for t in done)
            days = [(date.today() - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
            return {"demo": True, "days": days, "counts": [by_day.get(dd, 0) for dd in days],
                    "titles": [t["title"] for t in done]}
        today_d = date.today()
        monday = today_d - timedelta(days=today_d.weekday())
        start = monday.isoformat() + "T00:00:00+08:00"
        end = today_d.isoformat() + "T23:59:59+08:00"
        r = call_ticktick_mcp("list_completed_tasks_by_date", {"search": {
            "projectIds": list(PROJECT_IDS.values()),
            "startDate": start, "endDate": end}})
        items = []
        if "result" in r:
            items = r["result"].get("structuredContent", {}).get("result") or []
        days = [(monday + timedelta(days=i)).strftime("%m-%d") for i in range(today_d.weekday() + 1)]
        counts = {d: 0 for d in days}
        titles = []
        for t in items:
            ct = t.get("completedTime") or ""
            try:
                ct_d = (datetime.fromisoformat(ct.replace("+0000", "+00:00")).astimezone().strftime("%m-%d"))
            except Exception:
                ct_d = None
            if ct_d in counts: counts[ct_d] += 1
            titles.append(t.get("title", ""))
        return {"days": days, "counts": [counts[d] for d in days],
                "week_total": len(items), "titles": titles[:20]}

    # ── 鹅与金蛋 P/PC 平衡（后端唯一算分点：开屏卡/管家/教练共用同一份）──
    @staticmethod
    def _weeks_since(wk_str):
        """ISO 周键（如 2026-W33）距今几个整周；空/非法返回 None"""
        m = re.match(r"(\d{4})-W(\d{2})$", (wk_str or "").strip())
        if not m:
            return None
        try:
            that = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
        except Exception:
            return None
        today = date.today()
        this_mon = today - timedelta(days=today.weekday())
        return max(0, (this_mon - that).days // 7)

    def _ppc_baseline_fetch(self):
        """近 3 个完整周的平均完成数（P 的动态基线，含零周）"""
        today = date.today()
        this_mon = today - timedelta(days=today.weekday())
        start = (this_mon - timedelta(weeks=3)).isoformat() + "T00:00:00+08:00"
        end = (this_mon - timedelta(days=1)).isoformat() + "T23:59:59+08:00"
        r = call_ticktick_mcp("list_completed_tasks_by_date", {"search": {
            "projectIds": list(PROJECT_IDS.values()), "startDate": start, "endDate": end}})
        counts = {}
        for i in range(3):
            wk = (this_mon - timedelta(weeks=3 - i)).isocalendar()
            counts["%d-W%02d" % (wk[0], wk[1])] = 0
        if "error" not in r:
            for t in (r.get("result", {}).get("structuredContent", {}).get("result") or []):
                ct = t.get("completedTime") or ""
                try:
                    iso = datetime.fromisoformat(ct.replace("+0000", "+00:00")).astimezone().date().isocalendar()
                    k = "%d-W%02d" % (iso[0], iso[1])
                    if k in counts:
                        counts[k] += 1
                except Exception:
                    pass
        return round(sum(counts.values()) / 3.0, 1)

    def _ppc_scores(self, tasks, habits, gd):
        """P/PC 平衡分。P=本周完成/近3周基线（逾期减分）；PC=习惯50/关系25/复盘25 加权。
        任一数据源异常时返回 available:False，不阻塞 dashboard。"""
        try:
            DIMS = ["身体", "精神", "智力", "社会情感"]
            # 渐进式：每维 min(25, 打卡数/4*25)，打 1 次就可见移动，4 次满分
            dim_s = 0.0
            covered = 0
            for dim in DIMS:
                best = max((h.get("weekChecked", 0) for h in habits if h.get("dimension") == dim), default=0)
                dim_s += min(25.0, best * 25.0 / 4)
                if best >= 4:
                    covered += 1
            streaks = [h.get("streak", 0) for h in habits] or [0]
            avg_streak = sum(streaks) / len(streaks)
            streak_bonus = 10 if avg_streak >= 14 else (5 if avg_streak >= 7 else 0)
            habit_s = min(100, round(dim_s) + streak_bonus)

            wkey = _week_key()
            rels = [x for x in _alive(_read_json(RELATIONS_FILE, [])) if x.get("week") == wkey]
            net = sum(-x.get("amount", 0) if x.get("type") == "取款" else x.get("amount", 0) for x in rels)
            if len(rels) >= 3 and net > 0:
                rel_s = 100
            elif len(rels) >= 1 and net > 0:
                rel_s = 60
            elif len(rels) >= 1:
                rel_s = -20  # 只取款不存款
            else:
                rel_s = 0
            if gd.get("listeningThisWeek", 0) >= 1:
                rel_s += 10
            rel_s = max(0, min(100, rel_s))

            since = self._weeks_since(gd.get("lastReviewWeek"))
            rev_s = 100 if since == 0 else 60 if since == 1 else 20 if since is not None else 0

            # 健康产能（readiness 7日平滑，压单日波动——鹅的产能看趋势不看一晚）
            # 按日期对齐切片：对每个历史日 cutoff，只保留 date<=cutoff 的数据，
            # 这样 _yest 的"昨日活动"因子在历史日也能正确命中（不能只按数组下标切）
            health_s = None
            try:
                hd = self._get_health(14)
                days = hd.get("days") or []
                scores = []
                for i in range(min(7, len(days))):
                    cutoff = days[-1 - i]
                    sub = dict(hd)
                    sub["days"] = [d for d in days if d <= cutoff]
                    for k in ("sleep", "hrv", "rhr", "energy", "exercise", "mindful"):
                        sub[k] = [x for x in (hd.get(k) or []) if str(x.get("date") or "")[:10] <= cutoff]
                    rd = self._health_readiness(sub)
                    if rd.get("available"):
                        scores.append(rd["score"])
                if scores:
                    health_s = round(sum(scores) / len(scores))
            except Exception:
                pass
            if health_s is None:
                pc = max(0, min(100, round(0.5 * habit_s + 0.25 * rel_s + 0.25 * rev_s)))
                pc_detail = {"habit": habit_s, "relation": rel_s, "review": rev_s, "covered_dims": covered}
            else:
                pc = max(0, min(100, round(0.35 * habit_s + 0.30 * health_s + 0.175 * rel_s + 0.175 * rev_s)))
                pc_detail = {"habit": habit_s, "health": health_s, "relation": rel_s, "review": rev_s, "covered_dims": covered}

            wk = cached("week", self._get_week_report)
            week_total = wk.get("week_total", 0) if isinstance(wk, dict) else 0
            baseline = cached("ppc_base", self._ppc_baseline_fetch)
            p_raw = min(100, round(100.0 * week_total / baseline)) if baseline and baseline > 0 else 0
            today_s = date.today().isoformat()
            overdue = sum(1 for t in tasks if (t.get("due") or "")[:10] and t["due"][:10] < today_s)
            p = max(0, p_raw - min(25, overdue * 5))
            imb = p - pc

            if p < 35 and pc < 35:
                state, diag = "both_low", "产出 %d、产能 %d 都在低位——先做一件最小的事：给任意一个习惯打卡" % (p, pc)
            elif imb > 15:
                state, diag = "hungry_goose", "产出 %d / 产能 %d：你在光下蛋、饿着鹅——先补最弱的产能（习惯打卡）" % (p, pc)
            elif imb < -15:
                state, diag = "idle_goose", "产能 %d / 产出 %d：鹅养得不错，但还没下蛋——回到本周大石头" % (pc, p)
            else:
                state, diag = "balanced", "产出 %d / 产能 %d，P/PC 平衡" % (p, pc)
            return {"available": True, "p": p, "pc": pc, "imbalance": imb, "state": state, "diagnosis": diag,
                    "p_detail": {"week_total": week_total, "baseline": baseline, "overdue": overdue},
                    "pc_detail": pc_detail}
        except Exception:
            return {"available": False}

    WEEKLY_DRAFT_FILE = _dd_expanduser_default("DASH_WEEKLY_DRAFT_FILE", "AI夜观", "weekly_draft.json")

    @staticmethod
    def _task_role(t):
        """从任务 content 里解析角色名（与前端口径一致）"""
        m = re.search(r"角色[：:]\s*([^\n]+)", t.get("content") or "")
        return m.group(1).strip() if m else ""

    def _get_weekly_draft(self):
        """获取本周复盘草稿（按角色）；不存在或跨周则自动生成"""
        if _demo_enabled():
            return {"demo": True, "week": _demo().get("review", {}).get("week", ""),
                    "text": _demo().get("review", {}).get("text", "")}
        _iso = date.today().isocalendar()
        wk = f"{_iso[0]}-W{_iso[1]:02d}"
        try:
            with open(self.WEEKLY_DRAFT_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("week") == wk and d.get("roles"):
                return d
        except Exception:
            pass
        return self._generate_weekly_draft(wk)

    def _generate_weekly_draft(self, wk=None):
        """按角色生成周复盘草稿并写盘（供复盘视图一键载入）"""
        today_d = date.today()
        _iso = today_d.isocalendar()
        wk = wk or f"{_iso[0]}-W{_iso[1]:02d}"
        week = self._get_week_report()
        habits = self._habits().get("habits", [])
        tasks = (self._dash().get("tasks") or [])
        roles = (self._dash().get("roles") or [])
        checked = sum(1 for h in habits if h.get("checked"))
        max_streak = max((h.get("streak", 0) for h in habits), default=0)
        roles_draft = {}
        # P2-V6: 角色平衡段（各角色推进中任务数，标记空缺角色）——从前端 genReviewDraft 合并进后端
        _bal = sorted(((r2["name"], len([t for t in tasks if self._task_role(t) == r2["name"]])) for r2 in roles), key=lambda x: -x[1])
        _bal_line = "- **角色平衡**：" + ("、".join(f"{n}{c}项" for n, c in _bal if c > 0) or "本周无角色行动记录")
        _empty_roles = [n for n, c in _bal if c == 0]
        if _empty_roles:
            _bal_line += "。⚠️ 空缺角色：" + "、".join(_empty_roles)
        for r in roles:
            name = r["name"]
            rt = [t for t in tasks if self._task_role(t) == name]
            overdue = [t for t in rt if t.get("due") and t["due"][:10] < today_d.isoformat()]
            parts = []
            if rt:
                parts.append(f"推进中 {len(rt)} 项（" + "、".join(t["title"] for t in rt[:3]) + "）")
            if week.get("week_total"):
                parts.append(f"全系统本周共完成 {week['week_total']} 项")
            s = f"- **做了啥**：{(('；'.join(parts)) if parts else '本周此角色暂无行动记录')}\n"
            s += "- **卡在哪**："
            if overdue:
                s += f"有 {len(overdue)} 项逾期（{overdue[0]['title']}），需要决定是推进、委托还是删除\n"
            else:
                s += "（填写：这周遇到的阻力、没推进的原因）\n"
            s += f"- **习惯联动**：今日打卡 {checked}/{len(habits)}"
            if max_streak >= 3:
                s += f"，最长连续 {max_streak} 天 🔥"
            s += "\n" + _bal_line + "\n"
            s += "- **改进方向**：（填写：下周只做一件什么事能让这个角色前进一步？）"
            roles_draft[name] = s
        ai_outcomes = _advisor_outcome_summary()
        d = {"week": wk, "generated_at": datetime.now().isoformat(timespec="seconds"), "roles": roles_draft,
             "advisor_outcomes": {"count": ai_outcomes.get("count", 0),
                                  "counts": ai_outcomes.get("counts", {}),
                                  "usefulRate": ai_outcomes.get("usefulRate")}}
        if not _write_json(self.WEEKLY_DRAFT_FILE, d):
            d["persistence_warning"] = "本次草稿未保存，请稍后重试"
        return d

    LOCAL_STATE_FILE = _dd_expanduser_default("DASH_LOCAL_STATE_FILE", "local_state.json")
    LOCAL_STATE_BACKUP_LIMIT = 20

    def _backup_local_state(self, state):
        """保存写入前快照并只保留最近若干份；备份失败不覆盖主写入结果。"""
        if not isinstance(state, dict) or not state or not os.path.exists(self.LOCAL_STATE_FILE):
            return True
        backup_dir = getattr(self, "LOCAL_STATE_BACKUP_DIR", self.LOCAL_STATE_FILE + ".backups")
        try:
            os.makedirs(backup_dir, exist_ok=True)
            revision = int(state.get("_revision") or 0)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            target = os.path.join(backup_dir, f"local-state-r{revision}-{stamp}.json")
            if not _write_json(target, state):
                return False
            files = sorted(
                os.path.join(backup_dir, name)
                for name in os.listdir(backup_dir)
                if name.startswith("local-state-r") and name.endswith(".json")
            )
            limit = max(1, int(getattr(self, "LOCAL_STATE_BACKUP_LIMIT", 20)))
            for old_path in files[:-limit]:
                os.unlink(old_path)
            return True
        except Exception as exc:
            try:
                sys.stderr.write("[local-state-backup-fail] %s\n" % str(exc)[:200])
            except Exception:
                pass
            return False

    def _get_local_state(self):
        """读取多端同步的本地状态（completed/trash/insights 等）"""
        data = _read_json(self.LOCAL_STATE_FILE, {})
        if isinstance(data, dict):
            data.setdefault("_revision", 0)
            return data
        return {}

    @_serialized_file_update
    def _save_local_state(self, data):
        """保存多端状态：新客户端以 revision 做乐观并发控制，旧客户端兼容并集语义。"""
        if not isinstance(data, dict):
            return {"success": False, "error": "状态载荷必须是对象"}
        _junk = lambda s: "__e2e__" not in s
        old = self._get_local_state() or {}
        try:
            current_revision = int(old.get("_revision") or 0)
        except (TypeError, ValueError):
            current_revision = 0
        expected_revision = data.get("_revision")
        if expected_revision is not None:
            try:
                expected_revision = int(expected_revision)
            except (TypeError, ValueError):
                return {"success": False, "error": "_revision 必须是整数"}
        if expected_revision is not None and expected_revision != current_revision:
            return {"success": False, "conflict": True,
                    "error": "状态已被另一设备更新，请先拉取后再保存",
                    "serverRevision": current_revision, "state": old}
        # 传输元数据不能进入用户状态，尤其是幂等键否则会污染后续冲突比较。
        data = {k: v for k, v in data.items() if k not in ("_revision", "clientMutationId")}
        for k in ("completed", "trash"):
            if isinstance(data.get(k), list):
                data[k] = [x for x in data[k] if _junk(json.dumps(x, ensure_ascii=False))]
        if isinstance(data.get("habitDims"), dict):
            data["habitDims"] = {k: v for k, v in data["habitDims"].items()
                                 if _junk(k)}
        # 新客户端携带 revision：提交的是基于该版本的完整状态，允许删除真正传播；
        # 旧客户端未携带 revision 时保留历史并集语义，避免升级窗口内误删。
        if expected_revision is None:
            for k in ("completed", "trash"):
                if isinstance(data.get(k), list) and isinstance(old.get(k), list):
                    by_id = {}
                    for item in old[k] + data[k]:
                        if isinstance(item, dict) and item.get("id"):
                            by_id[item["id"]] = item  # 后提交(新)覆盖同 id
                        else:
                            by_id.setdefault(json.dumps(item, ensure_ascii=False, sort_keys=True), item)
                    data[k] = [v for v in by_id.values() if _junk(json.dumps(v, ensure_ascii=False))]
        for k, v in old.items():
            if k not in data:
                data[k] = v
        data.pop("updated_at", None)
        data["_revision"] = current_revision + 1
        data["updated_at"] = time.time()
        backup_ok = self._backup_local_state(old)
        if not _write_json(self.LOCAL_STATE_FILE, data):
            return {"success": False, "error": "本地状态写入失败"}
        result = {"success": True, "revision": data["_revision"], "updated_at": data["updated_at"],
                  "backup": bool(backup_ok)}
        if not backup_ok:
            result["warning"] = "状态已保存，但写入前备份失败"
        return result

    @_serialized_file_update
    def save_review(self, data):
        """周复盘写回 Obsidian 角色文件 ## 复盘记录"""
        role = data.get("role")
        text = (data.get("text") or "").strip()
        if not role or not text: return {"success": False, "error": "缺少角色或内容"}
        fpath = _safe_role_path(role)
        if not fpath: return {"success": False, "error": "角色名非法"}
        if not os.path.exists(fpath): return {"success": False, "error": "文件不存在"}
        _iso = date.today().isocalendar()
        heading = f"### {_iso[0]}-W{_iso[1]:02d}"
        try:
            with open(fpath, encoding="utf-8") as f:
                content = f.read()
            if "## 复盘记录" not in content:
                content = content.rstrip() + "\n\n## 复盘记录\n"
            block = f"{heading}\n{text}\n"
            if heading in content:
                pattern = rf"({re.escape(heading)}\n)(.*?)(?=\n### |\Z)"
                content = re.sub(pattern, lambda m: block, content, flags=re.DOTALL)
            else:
                content = content.rstrip() + "\n\n" + block
            if not _write_text_atomic(fpath, content):
                return {"success": False, "error": "角色复盘写入失败"}
            bust_cache()
            return {"success": True}
        except Exception as e:
            out = {"success": False}; out.update(_client_error(e)); return out

    # ─── 任务 CRUD ───
    def create_task(self, data):
        if _demo_enabled():
            return {"success": False, "error": "演示模式未连接 TickTick；配置 TICKTICK_TOKEN 后可创建真实任务。"}
        # 前端和 AI 管家都能调用此接口；服务端必须拒绝空标题，避免绕过 UI 校验制造不可用任务。
        title = str(data.get("title") or "").strip()
        if not title:
            return {"success": False, "error": "缺少标题"}
        project_id = PROJECT_IDS.get(data.get("project", "📥 收集箱"))
        if not project_id: return {"success": False, "error": f"未知项目: {data.get('project')}"}
        cp = []
        if data.get("role"): cp.append(f"角色：{data['role']}")
        if data.get("quad"): cp.append(f"象限：{data['quad']}")
        td = {"task":{"title":title,"projectId":project_id,"content":"\n".join(cp),
                      "priority":_tick_priority(data.get("priority",3)),
                      "tags":data.get("tags",[])}}
        if data.get("dueDate"): td["task"]["dueDate"] = data["dueDate"]
        if data.get("startDate"): td["task"]["startDate"] = data["startDate"]
        if data.get("reminders"): td["task"]["reminders"] = data["reminders"]
        if data.get("repeatFlag"): td["task"]["repeatFlag"] = data["repeatFlag"]
        if data.get("items"): td["task"]["items"] = data["items"]
        if data.get("tags"): td["task"]["tags"] = data["tags"]
        if data.get("content"): td["task"]["content"] = data["content"]
        r = call_ticktick_mcp("create_task", td)
        if "error" in r and r["error"]: return {"success":False,"error":r["error"]}
        bust_cache()
        res = r.get("result", {}) or {}
        sc = res.get("structuredContent") or res
        task_id = sc.get("id")
        if not task_id:
            return {"success": False, "error": "TickTick 返回成功但未提供新任务 ID，请重试"}
        # 创建接口偶尔会返回 success 但任务尚未真正落地；短暂等待并从项目列表回读。
        verified = _readback_created_task(project_id, task_id, title)
        if verified is None:
            return {"success": False, "error": "TickTick 返回成功但回读未找到新任务，请重试"}
        return {"success":True,"task":verified,"raw":res}

    def update_task(self, data):
        tid, pid = data.get("id"), data.get("projectId")
        if not tid or not pid: return {"success":False,"error":"缺少 id/projectId"}
        # 清空日期需受控尝试并回读确认：不同 MCP 版本分别接受 null 或空串，
        # 任何“返回成功但日期仍在”的结果都必须判失败，禁止假成功。
        clear_due = "dueDate" in data and data.get("dueDate") is None
        # 只发送调用方明确提供的字段——改标题时绝不携带空 content/tags，否则会抹掉象限/角色备注
        t = {"id":tid,"projectId":pid}
        if data.get("title"): t["title"] = data["title"]
        if data.get("priority") is not None and str(data.get("priority")) != "":
            t["priority"] = _tick_priority(data["priority"])
        if data.get("tags") is not None: t["tags"] = data["tags"]
        if "status" in data:
            try:
                status = int(data["status"])
            except (TypeError, ValueError):
                return {"success": False, "error": "status 必须是 0（未完成）或 2（已完成）"}
            if status not in (0, 2):
                return {"success": False, "error": "status 必须是 0（未完成）或 2（已完成）"}
            t["status"] = status
        # 可清空字段必须按“是否提供”判断，不能按 truthy 判断；否则清空日期/正文会假成功。
        if "content" in data: t["content"] = data["content"]
        if "dueDate" in data and not clear_due: t["dueDate"] = data["dueDate"]
        if clear_due: t["dueDate"] = None
        r = call_ticktick_mcp("update_task",{"task_id":tid,"task":t})
        if "error" in r and r["error"]: return {"success":False,"error":r["error"]}
        verify_fields = {k: t[k] for k in ("title", "content", "tags") if k in t}
        if "status" in t and t["status"] == 0:
            verify_fields["_active"] = True
        if clear_due:
            verified = None
            for delay in (0.25, 0.75):
                time.sleep(delay)
                check = next((x for x in _fetch_project_tasks(pid) if x.get("id") == tid), None)
                if check is not None and not check.get("dueDate") and not check.get("due"):
                    verified = check
                    break
            if verified is None:
                return {"success": False, "error": "TickTick 未确认日期已清空，请在滴答中手动清除"}
        elif verify_fields:
            verified = None
            for delay in (0.25, 0.75):
                time.sleep(delay)
                check = next((x for x in _fetch_project_tasks(pid) if x.get("id") == tid), None)
                if check is None:
                    continue
                matches = True
                for key, expected in verify_fields.items():
                    if key == "_active":
                        continue
                    actual = check.get(key)
                    if key == "tags":
                        matches = set(actual or []) == set(expected or [])
                    else:
                        matches = actual == expected
                    if not matches:
                        break
                if matches:
                    verified = check
                    break
            if verified is None:
                return {"success": False, "error": "TickTick 返回成功但回读未验证到任务更新，请重试"}
        bust_cache()
        return {"success":True, "task":verified if verify_fields else None}

    def set_task_archived(self, data, archived):
        """用 archived 标签归档/恢复 TickTick 任务，并回读验证真实状态。

        不使用 delete_task：前端的“可恢复”必须保留原任务 ID 和完整元数据。
        """
        tid, pid = data.get("id"), data.get("projectId")
        if not tid or not pid:
            return {"success": False, "error": "缺少 id/projectId"}
        current = next((t for t in _fetch_project_tasks(pid) if t.get("id") == tid), None)
        if not current:
            current = next((t for t in _fetch_completed_project_tasks(pid) if t.get("id") == tid), None)
        if not current:
            return {"success": False, "error": "TickTick 中未找到该任务，未执行归档变更"}
        tags = [str(x) for x in (current.get("tags") or []) if str(x)]
        if archived and "archived" not in tags:
            tags.append("archived")
        if not archived:
            tags = [x for x in tags if x != "archived"]
        payload = {"id": tid, "projectId": pid, "tags": tags}
        r = call_ticktick_mcp("update_task", {"task_id": tid, "task": payload})
        if "error" in r and r["error"]:
            return {"success": False, "error": r["error"]}
        # MCP 曾出现“返回成功但未落地”；短暂等待后回读两次，以权威数据确认。
        verified = None
        for delay in (0.25, 0.75):
            time.sleep(delay)
            check = next((t for t in _fetch_project_tasks(pid) if t.get("id") == tid), None)
            if not check:
                check = next((t for t in _fetch_completed_project_tasks(pid) if t.get("id") == tid), None)
            if check is not None and (("archived" in (check.get("tags") or [])) == archived):
                verified = check
                break
        if verified is None:
            return {"success": False, "error": "TickTick 返回成功但回读未验证到标签变化，请重试"}
        bust_cache()
        return {"success": True, "task": verified, "archived": archived}

    def delete_task(self, data):
        tid, pid = data.get("id"), data.get("projectId")
        if not tid or not pid: return {"success":False,"error":"缺少 id/projectId"}
        r = call_ticktick_mcp("delete_task",{"project_id":pid,"task_id":tid})
        if "error" in r and r["error"]: return {"success":False,"error":r["error"]}
        # 删除同样必须回读确认。若任一列表暂时不可达，不把“看不到”误判成已删除。
        verified_absent = False
        for delay in (0.25, 0.75):
            time.sleep(delay)
            active = _fetch_project_tasks(pid, with_status=True)
            completed = _fetch_completed_project_tasks(pid, with_status=True)
            if active is None or completed is None:
                continue
            if not any(x.get("id") == tid for x in active + completed):
                verified_absent = True
                break
        if not verified_absent:
            return {"success": False, "error": "TickTick 返回成功但回读仍找到该任务，请重试"}
        bust_cache()
        return {"success":True}

    def complete_task(self, data):
        tid, pid = data.get("id"), data.get("projectId")
        if not tid or not pid: return {"success":False,"error":"缺少 id/projectId"}
        # ⚠️ TickTick MCP 的 complete_task 工具有 bug（返回任务但不改 status）
        # 改用 update_task + status:2 确保真正完成
        r = call_ticktick_mcp("update_task", {"task_id": tid, "task": {"id": tid, "projectId": pid, "status": 2}})
        if "error" in r and r["error"]: return {"success":False,"error":r["error"]}
        # 写操作必须以权威回读确认；否则 MCP 即使返回成功，前端仍会出现“假完成”。
        verified = None
        for delay in (0.25, 0.75):
            time.sleep(delay)
            check = next((t for t in _fetch_completed_project_tasks(pid) if t.get("id") == tid), None)
            if check is None:
                continue
            try:
                status = int(check.get("status", 2))
            except (TypeError, ValueError):
                status = 2
            if status == 2:
                verified = check
                break
        if verified is None:
            return {"success": False, "error": "TickTick 返回成功但回读未验证到完成状态，请重试"}
        bust_cache()
        return {"success":True, "task":verified}

    # ─── 角色 ───
    @_serialized_file_update
    def create_role(self, data):
        """在 Obsidian 中创建新角色文件"""
        name = data.get("name")
        if not name: return {"success": False, "error": "缺少名称"}
        emoji = data.get("emoji", "📌")
        mission = data.get("mission", "")
        fpath = _safe_role_path(name)
        if not fpath: return {"success": False, "error": "角色名非法"}
        if os.path.exists(fpath): return {"success": False, "error": "角色已存在"}
        today = date.today()
        _iso = today.isocalendar()
        _year, _week, _quarter = _iso[0], _iso[1], (today.month - 1) // 3 + 1
        content = f"""---
role: {name}
emoji: {emoji}
quarter: {_year}-Q{_quarter}
---

# {emoji} {name}

## 使命宣言
{mission or '（点击编辑）'}

## 季度目标
（点击编辑）

## 关键结果
- [ ]

## 对话洞察
（暂无）

## 本周行动
- [ ]

## 复盘笔记
（点击编辑）

## 复盘记录

### {_year}-W{_week:02d}
- **做了啥**：
- **卡在哪**：
- **改进方向**：
"""
        try:
            os.makedirs(OBSIDIAN_ROLES_DIR, exist_ok=True)
            if not _write_text_atomic(fpath, content):
                return {"success": False, "error": "角色文件写入失败"}
            # 原子写成功不等于文件可被应用层读取；立刻解析回读，避免报告“成功”但角色列表仍为空。
            saved = parse_role_file(fpath)
            if not saved or saved.get("id") != name:
                return {"success": False, "error": "角色文件已写入但回读未验证到新角色"}
            bust_cache()
            return {"success": True, "role": saved}
        except Exception as e:
            out = {"success": False}; out.update(_client_error(e)); return out

    @_serialized_file_update
    def sync_role(self, data):
        """同步角色字段到 Obsidian 文件"""
        old_name = data.get("old_name") or data.get("name")
        if not old_name: return {"success": False, "error": "缺少角色名称"}
        fpath = _safe_role_path(old_name)
        if not fpath: return {"success": False, "error": "角色名非法"}
        if not os.path.exists(fpath): return {"success": False, "error": "文件不存在"}
        try:
            with open(fpath, encoding="utf-8") as f:
                text = f.read()
            if "emoji" in data:
                # Emoji 也是角色档案的持久字段；清理换行，避免用户输入破坏 YAML frontmatter。
                emoji = str(data.get("emoji") or "📌").replace("\r", " ").replace("\n", " ").strip()[:32] or "📌"
                if re.search(r"(?m)^emoji:\s*.*$", text):
                    text = re.sub(r"(?m)^emoji:\s*.*$", "emoji: " + emoji, text, count=1)
                elif text.startswith("---"):
                    end = text.find("---", 3)
                    if end > 0:
                        text = text[:end] + "emoji: " + emoji + "\n" + text[end:]
            if "mission" in data:
                text = self._ensure_section(text, "## 使命宣言")
                text = self._replace_section(text, "## 使命宣言", data["mission"])
            if "goal" in data:
                text = self._replace_section(text, "## 季度目标", data["goal"])
            if "insights" in data:
                text = self._replace_section(text, "## 对话洞察", data["insights"])
            if "review" in data:
                text = self._ensure_section(text, "## 复盘笔记")
                text = self._replace_section(text, "## 复盘笔记", data["review"])
            if "key_results" in data:
                # data["key_results"] 是列表 [{text, done}]，转成 - [ ] / - [x] 格式
                krs_text = "\n".join([f"- [{'x' if kr.get('done') else ' '}] {kr.get('text', '')}" for kr in data["key_results"] if kr.get('text')])
                text = self._replace_section(text, "## 关键结果", krs_text or "- （待添加）")
            if "actions" in data:
                # data["actions"] 是列表 [{text, done, pushed, taskId}]，转 checkbox 格式。
                # pushed 的在行尾加 HTML 注释保留 taskId 元信息，避免刷新后重复 push。
                lines = []
                for a in data["actions"]:
                    if not a.get("text"):
                        continue
                    mark = "x" if a.get("done") else " "
                    meta = ""
                    if a.get("pushed") and a.get("taskId"):
                        meta = f" <!--pushed:{a['taskId']}-->"
                    lines.append(f"- [{mark}] {a['text']}{meta}")
                acts_text = "\n".join(lines)
                text = self._replace_section(text, "## 本周行动", acts_text or "- （待添加）")
            new_name = data.get("new_name")
            if new_name and new_name != old_name:
                new_fpath = _safe_role_path(new_name)
                if not new_fpath: return {"success": False, "error": "新角色名非法"}
                if os.path.exists(new_fpath):
                    return {"success": False, "error": "目标角色已存在，未覆盖原角色文件"}
                text = re.sub(r'(role:\s*)' + re.escape(old_name), r'\1' + new_name, text)
                if not _write_text_atomic(fpath, text):
                    return {"success": False, "error": "角色文件写入失败"}
                os.rename(fpath, new_fpath)
                saved = parse_role_file(new_fpath)
                if not saved or saved.get("id") != new_name:
                    return {"success": False, "error": "角色改名后回读未验证到目标文件"}
                bust_cache()
                return {"success": True, "role": saved}
            if not _write_text_atomic(fpath, text):
                return {"success": False, "error": "角色文件写入失败"}
            saved = parse_role_file(fpath)
            if not saved or saved.get("id") != old_name:
                return {"success": False, "error": "角色文件已写入但回读未验证到变更"}
            bust_cache()
            return {"success": True, "role": saved}
        except Exception as e:
            out = {"success": False}; out.update(_client_error(e)); return out

    def _ensure_section(self, text, heading):
        """确保 ## 段落存在，不存在则在文件末尾追加"""
        if heading in text:
            return text
        return text.rstrip() + "\n\n" + heading + "\n（点击编辑）\n"

    def _replace_section(self, text, heading, new_content):
        """替换 markdown 中某个 ## 标题下的内容"""
        pattern = rf"({re.escape(heading)}\n)(.*?)(?=\n## |\Z)"
        return re.sub(pattern, lambda m: m.group(1) + new_content + "\n", text, flags=re.DOTALL)

    # ─── 习惯 ───
    def _get_habits(self):
        """获取所有习惯 + 今日打卡 + 最近7天 + 84天热力图 + 连续打卡天数"""
        if _demo_enabled():
            return {"demo": True, "habits": _demo().get("habits", [])}
        dims = _load_habit_dims()
        r = call_ticktick_mcp("list_habits", {})
        if "result" not in r or "error" in r:
            return {"habits": []}
        items = r["result"].get("structuredContent", {}).get("result", [])
        items = [h for h in items if not _is_junk_habit(h.get("name", ""))]
        if not items: return {"habits": []}
        habit_ids = [h["id"] for h in items]
        today_d = date.today()
        # 84天（12周）热力图窗口（跨月安全，用 timedelta）
        days84 = [(today_d - timedelta(days=i)).strftime("%Y%m%d") for i in range(83, -1, -1)]
        days7 = days84[-7:]
        today = int(days84[-1])
        # R1: to_stamp 曾用 today+1 做整数加法，月末（如 20260831+1=20260832）会得到不存在的日期戳；
        # 改用 timedelta 求真实的"明天"再格式化，跨月安全
        tomorrow_stamp = int((today_d + timedelta(days=1)).strftime("%Y%m%d"))
        checkins_r = call_ticktick_mcp("get_habit_checkins", {
            "habit_ids": habit_ids, "from_stamp": int(days84[0]), "to_stamp": tomorrow_stamp})
        checkins = {}  # {habitId: {stamp: value}}
        if "result" in checkins_r:
            ci = checkins_r["result"].get("structuredContent", {}).get("result", [])
            for item in ci:
                hid = item.get("habitId")
                if not hid: continue
                if hid not in checkins: checkins[hid] = {}
                for ck in (item.get("checkins") or []):
                    stamp = str(ck.get("stamp", ""))
                    if not stamp: continue
                    status = ck.get("status")
                    if status is None or status == 2:
                        checkins[hid][stamp] = ck.get("value", 1)
        habits = []
        for h in items:
            hid = h["id"]
            is_real = (h.get("type") == "Real")
            chk = checkins.get(hid, {})
            heat = {}
            for d in days84:
                if d in chk:
                    heat[d] = chk[d] if is_real else 1
                else:
                    heat[d] = 0.0 if is_real else 0
            daily = {d: heat[d] for d in days7}
            # 连续打卡天数（今天没打则从昨天数起）
            streak = 0
            cursor = today_d
            if not chk.get(cursor.strftime("%Y%m%d")):
                cursor = cursor - timedelta(days=1)
            while chk.get(cursor.strftime("%Y%m%d")):
                streak += 1
                cursor = cursor - timedelta(days=1)
            habits.append({
                "id": hid,
                "name": h["name"],
                "type": h.get("type", "Boolean"),
                "goal": h.get("goal", 1),
                "unit": h.get("unit", ""),
                "targetDays": h.get("targetDays", 21),
                "repeatRule": h.get("repeatRule", ""),
                "checked": str(today) in chk,
                "totalCheckIns": h.get("totalCheckIns", 0),  # TickTick 全量总数
                "weekChecked": sum(1 for v in daily.values() if isinstance(v, (int, float)) and v > 0),
                "weekTotal": sum(v for v in daily.values() if isinstance(v, (int, float))),
                "daily": daily,
                "heat": heat,
                "streak": streak,
                "dimension": (dims.get(h.get("name", "")) or {}).get("dimension", ""),
                "role": (dims.get(h.get("name", "")) or {}).get("role", "")
            })
        return {"habits": habits}

    def checkin_habit(self, data):
        """习惯打卡"""
        habit_id = data.get("habitId")
        if not habit_id: return {"success": False, "error": "缺少 habitId"}
        today_d = date.today()
        today = int(today_d.strftime("%Y%m%d"))
        tomorrow = int((today_d + timedelta(days=1)).strftime("%Y%m%d"))
        try:
            expected = float(data.get("value", 1))
        except (TypeError, ValueError):
            return {"success": False, "error": "打卡值必须是数字"}
        if expected < 0:
            return {"success": False, "error": "打卡值不能为负数"}
        r = call_ticktick_mcp("upsert_habit_checkins", {
            "habit_id": habit_id,
            "checkin_data": {"stamp": today, "value": expected}
        })
        if "error" in r and r["error"]: return {"success": False, "error": r["error"]}
        # 回读当天打卡，避免 MCP 返回成功但实际未切换状态。
        verified = False
        for delay in (0.25, 0.75):
            time.sleep(delay)
            cr = call_ticktick_mcp("get_habit_checkins", {
                "habit_ids": [habit_id], "from_stamp": today, "to_stamp": tomorrow})
            items = (cr.get("result", {}).get("structuredContent", {}).get("result") or []) if isinstance(cr, dict) else []
            found = None
            for item in items:
                for ck in (item.get("checkins") or []):
                    if str(ck.get("stamp", "")) == str(today):
                        found = ck
            if expected == 0:
                verified = found is None or float(found.get("value", 0) or 0) == 0
            elif found is not None:
                try: verified = float(found.get("value", 0)) >= expected
                except (TypeError, ValueError): verified = False
            if verified:
                break
        if not verified:
            return {"success": False, "error": "TickTick 返回成功但回读未验证到今日打卡，请重试"}
        bust_cache()
        return {"success": True, "value": expected}

    def create_habit(self, data):
        """创建新习惯（含习惯7·四维维度）"""
        name = str(data.get("name") or "").strip()
        if not name: return {"success": False, "error": "缺少名称"}
        if _is_junk_habit(name): return {"success": False, "error": "测试习惯名被拒绝（__e2e__ 前缀保留）"}
        try:
            goal = float(data.get("goal", 1))
            target_days = int(data.get("targetDays", 21))
        except (TypeError, ValueError):
            return {"success": False, "error": "目标值和目标天数必须是数字"}
        if goal <= 0 or target_days <= 0:
            return {"success": False, "error": "目标值和目标天数必须大于 0"}
        habit = {
            "name": name,
            "type": data.get("type", "Boolean"),
            "goal": goal,
            "unit": str(data.get("unit") or "").strip(),
            "targetDays": target_days,
            "repeatRule": "RRULE:FREQ=DAILY"
        }
        dim = data.get("dimension")
        if dim:
            habit["tags"] = [dim]  # TickTick habit 自定义字段兜底：维度写入 tags
        r = call_ticktick_mcp("create_habit", {"habit": habit})
        if "error" in r and r["error"]: return {"success": False, "error": r["error"]}
        # MCP 返回成功但未携带可定位 ID 时无法做权威回读，不能把创建报告为成功。
        result = r.get("result", {}) if isinstance(r, dict) else {}
        structured = result.get("structuredContent", {}) if isinstance(result, dict) else {}
        created = structured.get("result") if isinstance(structured, dict) else None
        if isinstance(structured, dict) and structured.get("id"):
            created = structured
        if not isinstance(created, dict) and isinstance(result.get("result"), dict):
            created = result.get("result")
        if not isinstance(created, dict):
            # 某些 MCP 响应只把 JSON 放在 content[0].text，兼容该形态但仍要求 ID。
            for item in result.get("content", []) if isinstance(result, dict) else []:
                if not isinstance(item, dict) or item.get("type") != "text":
                    continue
                try:
                    parsed = json.loads(item.get("text", ""))
                except Exception:
                    continue
                if isinstance(parsed, dict) and parsed.get("id"):
                    created = parsed
                    break
        hid = created.get("id") if isinstance(created, dict) else None
        if not hid:
            return {"success": False, "error": "TickTick 未提供新习惯 ID，无法回读确认"}
        verified = None
        for delay in (0.25, 0.75):
            time.sleep(delay)
            lr = call_ticktick_mcp("list_habits", {})
            if isinstance(lr, dict) and lr.get("error"):
                continue
            lresult = lr.get("result", {}) if isinstance(lr, dict) else {}
            items = lresult.get("structuredContent", {}).get("result", []) if isinstance(lresult, dict) else []
            candidate = next((h for h in items if h.get("id") == hid and h.get("name") == name), None)
            if candidate is not None:
                verified = candidate
                break
        if verified is None:
            return {"success": False, "error": "TickTick 返回成功但回读未验证到新习惯，请重试"}
        role = (data.get("role") or "").strip()
        warning = ""
        if dim or role:
            dims = _load_habit_dims()
            entry = {"dimension": dim or "", "role": role}
            entry = {k: v for k, v in entry.items() if v}
            dims[name] = entry
            if not _save_habit_dims(dims):
                warning = "习惯已创建，但四维/角色归属未写入"
        bust_cache()
        out = {"success": True, "habit": verified}
        if warning:
            out["warning"] = warning
        return out

    # ─── 情感账户（习惯 4/5/6：双赢·知彼解己·统合综效）───
    def get_relations(self):
        if _demo_enabled():
            return {"demo": True, "relations": _demo().get("relations", [])}
        if _mirror_stale(RELATIONS_FILE, RELATIONS_MD):
            _sync_relations_md()
        return _alive(_read_json(RELATIONS_FILE, []))

    @_serialized_file_update
    def post_relation(self, data):
        entry = data.get("entry")
        if not isinstance(entry, dict):
            return {"success": False, "error": "entry 必须是对象"}
        entry = dict(entry)
        entry["who"] = str(entry.get("who") or "").strip()
        entry["reason"] = str(entry.get("reason") or "").strip()
        entry["role"] = str(entry.get("role") or "").strip()
        if not entry["who"] or not entry["reason"]:
            return {"success": False, "error": "缺少对象或事由"}
        entry["type"] = str(entry.get("type") or "存款").strip()
        if entry["type"] not in ("存款", "取款"):
            return {"success": False, "error": "类型必须是存款或取款"}
        try:
            entry["amount"] = abs(int(entry.get("amount") or 10))
        except (TypeError, ValueError):
            return {"success": False, "error": "金额必须是数字"}
        if entry["amount"] <= 0:
            return {"success": False, "error": "金额必须大于 0"}
        entry["week"] = _week_key()
        entry["ts"] = datetime.now().isoformat(timespec="seconds")
        entry.setdefault("id", "r" + str(int(time.time() * 1000)))
        rels = _read_json(RELATIONS_FILE, [])
        rels.append(entry)
        json_ok = _write_json(RELATIONS_FILE, rels)
        mirror_ok = _sync_mirror(json_ok, _sync_relations_md, "情感账户")
        if not json_ok:
            return {"success": False, "error": "数据写入失败"}
        bust_cache()
        out = {"success": True, "count": len(rels), "entry": entry}
        if not mirror_ok:
            out["warning"] = "主数据已保存，但 Obsidian 镜像同步失败"
        return out

    # ─── 倾听笔记（习惯 5：知彼解己）───
    def get_listening(self):
        if _demo_enabled():
            return {"demo": True, "listening": _demo().get("listening", [])}
        if _mirror_stale(LISTENING_FILE, LISTENING_MD):
            _sync_listening_md()
        return _alive(_read_json(LISTENING_FILE, []))

    # ─── 健康 · 睡眠主观备注（主客观数据对齐的钥匙）───
    def get_health_notes(self):
        return [x for x in _read_json(HEALTH_NOTES_FILE, []) if not x.get("deleted")]

    @_serialized_file_update
    def post_health_note(self, data):
        if not isinstance(data.get("note"), str):
            return {"success": False, "error": "note 必须是文本"}
        note = data["note"].strip()
        if not note:
            return {"success": False, "error": "缺少 note"}
        lst = _read_json(HEALTH_NOTES_FILE, [])
        entry = {"id": "h" + str(int(time.time() * 1000)),
                 "date": (data.get("date") or date.today().isoformat()),
                 "note": note[:300],
                 "ts": datetime.now().isoformat(timespec="seconds")}
        lst.append(entry)
        json_ok = _write_json(HEALTH_NOTES_FILE, lst[-90:])   # 只留近 90 条（约 3 个月）
        if not json_ok:
            return {"success": False, "error": "数据写入失败"}
        bust_cache()
        return {"success": True, "count": len(lst[-90:]), "note": entry}

    @_serialized_file_update
    def delete_health_note(self, data):
        """按 id 或内容关键词删除睡眠备注（软删：deleted 标记 + ts 更新，保证删除随 merge 传播到另一端）。"""
        nid = (data.get("id") or "").strip()
        kw = (data.get("note") or data.get("关键词") or data.get("内容") or "").strip()
        lst = _read_json(HEALTH_NOTES_FILE, [])
        if nid:
            hits = [x for x in lst if str(x.get("id") or "") == nid and not x.get("deleted")]
        elif kw:
            hits = [x for x in lst if kw in str(x.get("note") or "") and not x.get("deleted")]
        else:
            return {"success": False, "error": "缺少 id 或内容关键词"}
        if not hits:
            return {"success": False, "error": "没有找到要删除的备注"}
        now = datetime.now().isoformat(timespec="seconds")
        for x in hits:
            x["deleted"] = True
            x["ts"] = now
        if not _write_json(HEALTH_NOTES_FILE, lst):
            return {"success": False, "error": "数据写入失败"}
        bust_cache()
        return {"success": True, "removed": len(hits)}

    # ─── 通用 CRUD 引擎（v41）：注册表实体自动获得 建/删/改/查，无需逐个硬编码 ───
    def _crud_raw(self, reg):
        if reg.get("sub"):
            d = _read_json(reg["file"], {}) or {}
            return d.setdefault(reg["sub"], [])
        return _read_json(reg["file"], [])

    def _crud_write(self, reg, items):
        if reg.get("sub"):
            d = _read_json(reg["file"], {}) or {}
            d[reg["sub"]] = items
            return _write_json(reg["file"], d)
        return _write_json(reg["file"], items)

    def _crud_find_reg(self, kind):
        for v in CRUD_VERBS:
            if kind.startswith(v):
                ent = kind[len(v):]
                for reg in CRUD_REGISTRY:
                    if reg["name"] == ent:
                        return CRUD_VERBS[v], reg
        return None, None

    @_serialized_file_update
    def _crud_execute(self, verb, reg, a):
        name = reg["name"]
        fmap = reg["fields"]
        cn2key = {v: k for k, v in fmap.items()}
        def g(*ks):
            for k in ks:
                if a.get(k):
                    return a[k]
            return ""
        def val(k):
            cn = fmap.get(k, k)
            return a.get(cn) or a.get(k)
        if verb == "list":
            items = self._crud_raw(reg)
            alive = _alive(items) if reg.get("soft") else items
            return {"success": True, "count": len(alive), "items": alive[-20:]}
        if verb == "create":
            entry = {"id": reg["id_prefix"] + str(int(time.time() * 1000)),
                     "ts": datetime.now().isoformat(timespec="seconds")}
            filled = 0
            for k in fmap:
                v = val(k)
                if v not in (None, ""):
                    entry[k] = v
                    filled += 1
            if not filled:
                return {"success": False, "error": f"缺少{name}内容（可用：{'/'.join(fmap.values())}）"}
            items = self._crud_raw(reg)
            items.append(entry)
            ok_w = self._crud_write(reg, items)
            _mf = globals().get(reg.get("mirror")) if reg.get("mirror") else None
            if _mf and ok_w:
                _sync_mirror(ok_w, _mf, name)
            if not ok_w:
                return {"success": False, "error": "数据写入失败"}
            bust_cache()
            _undo_push(f"新增{name}", "create", name, {"id": entry.get("id")})
            return {"success": True, "msg": f"已新增{name}", "entry": entry}
        if verb == "delete":
            kw = (g("内容", "关键词", "id", "标题", "对象", "text", "who", "note") or "").strip()
            if not kw:
                return {"success": False, "error": f"缺少要删除的{name}关键词/id"}
            items = self._crud_raw(reg)
            hits = [x for x in items if not (isinstance(x, dict) and x.get("deleted")) and (
                str(x.get("id")) == kw or any(kw in str(x.get(f) or "") for f in reg["search"]))]
            if not hits:
                return {"success": False, "error": f"没有找到匹配的{name}"}
            if reg.get("soft"):
                removed = _soft_mark(items, [x.get("id") for x in hits])
            else:
                ids = {x.get("id") for x in hits}
                items[:] = [x for x in items if x.get("id") not in ids]
                removed = len(hits)
            ok_w = self._crud_write(reg, items)
            _mf = globals().get(reg.get("mirror")) if reg.get("mirror") else None
            if _mf and ok_w:
                _sync_mirror(ok_w, _mf, name)
            if not ok_w:
                return {"success": False, "error": "数据写入失败"}
            bust_cache()
            _undo_push(f"删除{name}", "delete", name, {"ids": [x.get("id") for x in hits]})
            return {"success": True, "msg": f"已删除 {removed} 条{name}"}
        if verb == "update":
            rid = (g("id", "ID") or "").strip()
            kw = (g("关键词", "标题", "对象", "who", "text", "note") or "").strip()
            items = self._crud_raw(reg)
            ent = None
            if rid:
                ent = next((x for x in items if not x.get("deleted") and str(x.get("id")) == rid), None)
            elif kw:
                ent = next((x for x in items if not x.get("deleted") and any(kw in str(x.get(f) or "") for f in reg["search"])), None)
            if not ent:
                return {"success": False, "error": f"没有找到要修改的{name}"}
            changed = 0
            # 保留字段原始的 None/缺失状态，撤销时才能真正移除本次新增字段。
            _old = {k: ent.get(k) for k in fmap}
            new_val = g("新内容", "改为", "改成", "new_text", "new_value")
            if new_val:
                first_txt = next((k for k in fmap if k in ("note", "text", "who")), None) or next(iter(fmap))
                ent[first_txt] = new_val
                changed += 1
            for k in fmap:
                v = val(k)
                if v not in (None, ""):
                    ent[k] = v
                    changed += 1
            if not changed:
                return {"success": False, "error": "没有要修改的字段"}
            ok_w = self._crud_write(reg, items)
            _mf = globals().get(reg.get("mirror")) if reg.get("mirror") else None
            if _mf and ok_w:
                _sync_mirror(ok_w, _mf, name)
            if not ok_w:
                return {"success": False, "error": "数据写入失败"}
            bust_cache()
            _undo_push(f"修改{name}", "update", name, {"id": ent.get("id"), "old": _old})
            return {"success": True, "msg": f"已修改{name}", "entry": ent}
        return {"success": False, "error": f"不支持的动作：{verb}{name}"}

    # ─── 操作历史 / 一键返回（v42）───
    def undo_action(self, data=None):
        """撤销上一步写动作（精确式）：按 op 恢复注册表实体数据，弹栈。
        create→软删新增；delete→restore 软删；update→写回旧值。数据经 merge 传播到另一端。"""
        lst = _read_json(UNDO_HISTORY_FILE, [])
        if not lst:
            return {"success": False, "error": "没有可撤销的操作"}
        requested_turn = str((data or {}).get("clientTurnId") or "").strip()
        e = lst[-1]
        if requested_turn and e.get("clientTurnId") != requested_turn:
            return {"success": False, "error": "该动作之后还有更新，请先撤销最近一笔操作"}
        op, ent, pl = e.get("op"), e.get("entity"), e.get("payload") or {}
        # TickTick 任务的 AI 创建撤销：归档原任务并保留完整历史/ID。
        if ent == "TickTick任务" and op == "ticktick_archive":
            result = self.set_task_archived({"id": pl.get("id"), "projectId": pl.get("projectId")}, True)
            if not result.get("success"):
                return {"success": False, "error": result.get("error") or "任务撤销失败"}
            lst.pop()
            _write_json(UNDO_HISTORY_FILE, lst)
            if requested_turn:
                rows = _load_action_audit()
                for row in reversed(rows):
                    if isinstance(row, dict) and row.get("clientTurnId") == requested_turn:
                        row["undoed"] = True
                        break
                _write_json(_ACTION_AUDIT_FILE, rows[-_ACTION_AUDIT_MAX:])
            return {"success": True, "msg": "已撤销：管家新增任务（已归档，可恢复）", "clientTurnId": pl.get("clientTurnId")}
        reg = next((r for r in CRUD_REGISTRY if r["name"] == ent), None)
        if not reg:
            return {"success": False, "error": f"无法撤销：{ent} 不在注册表"}
        name = reg["name"]
        items = self._crud_raw(reg)
        done = False
        if op == "create":
            rid = pl.get("id")
            if rid and reg.get("soft"):
                done = _soft_mark(items, [rid]) > 0
            elif rid:
                items[:] = [x for x in items if x.get("id") != rid]
                done = True
        elif op == "delete":
            ids = pl.get("ids") or ([pl["id"]] if pl.get("id") else [])
            restored = 0
            for rid in ids:
                if _restore(items, rid):
                    restored += 1
            done = restored > 0
        elif op == "update":
            rid = pl.get("id")
            old = pl.get("old") or {}
            ent_item = next((x for x in items if x.get("id") == rid), None)
            if ent_item:
                for k, v in old.items():
                    if v is None:
                        ent_item.pop(k, None)
                    else:
                        ent_item[k] = v
                done = True
        if not done:
            return {"success": False, "error": "该操作已无法恢复（可能已被后续操作覆盖）"}
        ok_w = self._crud_write(reg, items)
        _mf = globals().get(reg.get("mirror")) if reg.get("mirror") else None
        if _mf and ok_w:
            _sync_mirror(ok_w, _mf, name)
        if not ok_w:
            return {"success": False, "error": "数据写入失败"}
        bust_cache()
        lst.pop()
        _write_json(UNDO_HISTORY_FILE, lst)
        return {"success": True, "msg": f"已撤销：{e.get('desc')}"}

    @_serialized_file_update
    def post_listening(self, data):
        note = data.get("note")
        if not isinstance(note, dict):
            return {"success": False, "error": "note 必须是对象"}
        note = dict(note)
        for key in ("who", "feeling", "restate", "third"):
            note[key] = str(note.get(key) or "").strip()
        if not note["who"]:
            return {"success": False, "error": "缺少对话对象"}
        if not any(note[key] for key in ("feeling", "restate", "third")):
            return {"success": False, "error": "至少填写一项倾听内容"}
        note["week"] = _week_key()
        note["ts"] = datetime.now().isoformat(timespec="seconds")
        note.setdefault("id", "l" + str(int(time.time() * 1000)))
        lst = _read_json(LISTENING_FILE, [])
        lst.append(note)
        json_ok = _write_json(LISTENING_FILE, lst)
        mirror_ok = _sync_mirror(json_ok, _sync_listening_md, "倾听笔记")
        if not json_ok:
            return {"success": False, "error": "数据写入失败"}
        bust_cache()
        out = {"success": True, "count": len(lst), "note": note}
        if not mirror_ok:
            out["warning"] = "主数据已保存，但 Obsidian 镜像同步失败"
        return out

    # ─── 周计划·大石头（习惯 3：要事第一，第二象限投资）───
    def get_week_plan(self):
        if _demo_enabled():
            d = _demo().get("week_plan", {})
            return {"demo": True, "week": d.get("week", ""), "big_rocks": d.get("big_rocks", []), "revision": d.get("revision", 0)}
        tasks = cached("tasks", load_ticktick_tasks) or []
        today = date.today()
        monday = (today - timedelta(days=today.weekday())).isoformat()
        wk = today.isocalendar()
        def is_rock(t):
            return ("大石头" in (t.get("content") or "")
                    or "大石头" in (t.get("tags") or []))
        big = [t for t in tasks if is_rock(t)]
        q2 = [t for t in tasks if not is_rock(t) and "象限：q2" in (t.get("content") or "")]
        due_this_week = [t for t in tasks if t.get("due") and t["due"][:10] >= monday]
        return {"week": f"{wk[0]}-W{wk[1]:02d}", "big_rocks": big, "q2": q2,
                "due_this_week": due_this_week, "all_undone": len(tasks)}

    def post_week_plan(self, data):
        title = str(data.get("title") or "").strip()
        if not title:
            return {"success": False, "error": "缺少 title"}
        project_id = PROJECT_IDS.get(data.get("project", "📥 收集箱"))
        if not project_id:
            return {"success": False, "error": f"未知项目: {data.get('project')}"}
        why = str(data.get("why") or "").strip()
        role = str(data.get("role") or "").strip()
        content = "象限：q2\n大石头：" + why
        if role:
            content += f"\n角色：{role}"
        td = {"task": {"title": title,
                       "projectId": project_id,
                       "content": content, "priority": 5, "tags": ["大石头"]}}
        if data.get("dueDate"):
            td["task"]["dueDate"] = data["dueDate"]
        r = call_ticktick_mcp("create_task", td)
        if "error" in r and r["error"]:
            return {"success": False, "error": r["error"]}
        res = r.get("result", {}) or {}
        sc = res.get("structuredContent") or res
        task_id = sc.get("id")
        if not task_id:
            return {"success": False, "error": "TickTick 返回成功但未提供大石头任务 ID，请重试"}
        verified = _readback_created_task(project_id, task_id, title)
        if verified is None:
            return {"success": False, "error": "TickTick 返回成功但回读未找到大石头任务，请重试"}
        bust_cache()
        return {"success": True, "task": verified}

    # ─── AI 管家（P0 后端直调 LLM）───
    def butler_status(self):
        base = _cfg.get("LLM_API_BASE", "")
        key = _cfg.get("LLM_API_KEY", "")
        return {"online": bool(base and key), "model": _cfg.get("LLM_MODEL", ""),
                "fallback": _cfg.get("LLM_FALLBACK", ""), "ticktick": bool(TICKTICK_TOKEN)}

    def _butler_context(self):
        """构建管家每轮可见的真实数据快照（实体清单，而非统计摘要）——
        让模型能引用具体名字、对实体做增删改，而不是凭空编造。"""
        d = cached("dash", self._get_dashboard_data)
        roles = d.get("roles") or []
        tasks = d.get("tasks") or []
        habits = d.get("habits") or []
        gd = d.get("chain_health", {})
        wp = self.get_week_plan()
        today = date.today()
        monday = (today - timedelta(days=today.weekday())).isoformat()

        # 角色 + KR + 关联习惯（让管家能判断角色是否完整、以及角色目标与实际习惯是否脱节）
        habits_by_role = {}
        for h in habits:
            rn = h.get("role") or ""
            if rn:
                habits_by_role.setdefault(rn, []).append(h)
        role_block = []
        for r in roles:
            kr = r.get("key_results") or []
            kr_titles = []
            for k in (kr if isinstance(kr, list) else [])[:4]:
                t = (k.get("title") or k.get("name") or "") if isinstance(k, dict) else str(k)
                if t:
                    kr_titles.append(t)
            rhabits = habits_by_role.get(r.get("name", ""), [])
            htxt = "、".join(
                "%s %d/7（连续%d天%s）" % (x.get("name", ""), x.get("weekChecked", 0),
                                          x.get("streak", 0), "；今日已打卡" if x.get("checked") else "")
                for x in rhabits) or "无"
            _mission = (r.get("mission") or "").strip().replace("\n", " ")
            role_block.append("  · %s（使命:%s；KR %d: %s；关联习惯：%s）" % (
                r.get("name", ""),
                _mission[:40] or "未填写",
                len(kr_titles),
                "、".join(kr_titles) or "无",
                htxt))
        role_block = "\n".join(role_block) or "  （还没有任何角色）"

        # 待办排序（v55）：逾期优先 → 到期日升序 → 优先级降序。
        # 原先按项目拉取顺序截前 14 条，逾期/紧急任务常被挤到 14 条外，管家看不见。
        def _task_key(t):
            due = (t.get("due") or "")[:10]
            try:
                pr = -int(t.get("priority") or 0)
            except (TypeError, ValueError):
                pr = 0
            return (1 if (due and due < today.isoformat()) else 0, due or "9999-12-31", pr)
        tasks = sorted(tasks, key=_task_key)
        # 待办：本周到期优先，带 角色/优先级/是否大石头 标记，最多 14 条
        task_block, shown = [], 0
        for t in tasks:
            if shown >= 14:
                break
            # title=任务真名；content=备注（象限/角色/大石头标记存这里）。显示用 title，标记检索兼顾两者
            note = t.get("content") or ""
            content = (t.get("title") or note or "").replace("\n", " ")
            due = (t.get("due") or "")[:10]
            is_rock = "大石头" in content or "大石头" in note or "大石头" in (t.get("tags") or [])
            role_m = re.search(r"角色[：:]\s*(\S+)", note + " " + content)
            flags = []
            if is_rock:
                flags.append("🪨大石头")
            if due and due >= monday:
                flags.append("本周到期%s" % due)
            elif due:
                flags.append("到期%s" % due)
            if role_m:
                flags.append("@%s" % role_m.group(1))
            if t.get("priority"):
                flags.append("P%d" % t["priority"])
            task_block.append("  · %s%s" % (content[:42], (" [" + "; ".join(flags) + "]") if flags else ""))
            shown += 1
        task_block = "\n".join(task_block) or "  （无待办）"

        # 习惯：维度/连续/本周打卡/今日
        habit_block = []
        for h in habits:
            habit_block.append("  · %s（维度:%s; 角色:%s; 连续%d天; 本周%d/7%s）" % (
                h.get("name", ""), h.get("dimension") or "未归类", h.get("role") or "无",
                h.get("streak", 0),
                h.get("weekChecked", 0), "；今日已打卡" if h.get("checked") else ""))
        habit_block = "\n".join(habit_block) or "  （无习惯）"

        rel_line = ("本周情感账户 %d 笔、倾听笔记 %d 笔；最近复盘：%s" % (
            gd.get("relationsThisWeek", 0), gd.get("listeningThisWeek", 0),
            gd.get("lastReviewWeek") or "无"))

        # 鹅与金蛋 P/PC 平衡（与开屏卡、教练卡同一份后端算分）
        ppc_block = ""
        ppc = (gd.get("ppc") or {})
        if ppc.get("available"):
            pdt = ppc.get("p_detail") or {}
            pcd = ppc.get("pc_detail") or {}
            ppc_block = ("【鹅与金蛋 P/PC】产出 %d/100（本周完成 %d 项，近3周基线 %s，逾期 %d）；"
                         "产能 %d/100（习惯 %d、关系 %d、复盘 %d）——%s。"
                         "失衡时要点破用户「角色目标与实际习惯脱节」之处，引用具体习惯名和打卡数。\n\n" % (
                             ppc.get("p", 0), pdt.get("week_total", 0),
                             ("%g" % (pdt.get("baseline") or 0)), pdt.get("overdue", 0),
                             ppc.get("pc", 0), pcd.get("habit", 0), pcd.get("relation", 0),
                             pcd.get("review", 0), ppc.get("diagnosis", "")))

        # 情感账户明细（最多 8 条；软删条目不可见）
        rels = _alive(_read_json(RELATIONS_FILE, []))
        if rels:
            rel_block = "\n".join(
                "  · %s %s%d 币：%s（%s）" % (
                    x.get("who", "?"), "取款" if x.get("type") == "取款" else "存款",
                    x.get("amount", 0), (x.get("reason") or "")[:40],
                    (x.get("date") or x.get("ts") or "")[:10])
                for x in rels[-8:][::-1])
            rel_block += "\n  （共 %d 条）" % len(rels)
        else:
            rel_block = "  （暂无情感账户记录）"

        # 倾听笔记明细（最多 6 条；软删条目不可见）
        listens = _alive(_read_json(LISTENING_FILE, []))
        if listens:
            lis_block = "\n".join(
                "  · 对「%s」：感受[%s] 复述[%s] 第三选择[%s]（%s）" % (
                    x.get("who", "?"), (x.get("feeling") or "")[:24],
                    (x.get("restate") or "")[:24], (x.get("third") or "")[:24],
                    (x.get("date") or x.get("ts") or "")[:10])
                for x in listens[-6:][::-1])
            lis_block += "\n  （共 %d 条）" % len(listens)
        else:
            lis_block = "  （暂无倾听笔记）"

        # 关注圈/影响圈（焦虑与可行动项；软删条目不可见）
        pro = self._raw_proactive()
        pro["concerns"] = _alive(pro.get("concerns") or [])
        pro["logs"] = _alive(pro.get("logs") or [])
        concerns = pro.get("concerns") or []
        if concerns:
            pro_block = "\n".join(
                "  · [%s] %s%s" % (
                    "影响圈" if c.get("circle") == "influence" else "关注圈",
                    (c.get("text") or "")[:50],
                    "（已转任务）" if c.get("converted") else "")
                for c in concerns[-8:][::-1])
            pro_block += "\n  （共 %d 条；影响圈 %d 条）" % (
                len(concerns),
                sum(1 for c in concerns if c.get("circle") == "influence"))
        else:
            pro_block = "  （暂无关注圈/影响圈条目）"
        # 消极→积极语言转换记录（最近 3 条）
        logs = (pro.get("logs") or [])
        if logs:
            log_block = "\n".join(
                "  · 「%s」→「%s」（%s）" % ((l.get("from") or "")[:24],
                                           (l.get("to") or "")[:24], (l.get("date") or "")[:10])
                for l in logs[-3:][::-1])
        else:
            log_block = "  （暂无语言转换记录）"
        # 今日主动打卡
        chk = pro.get("checkins") or {}
        checkin_line = "，今日打卡：%s" % next(iter(chk.values()))[:40] if chk else ""
        if checkin_line:
            checkin_line = checkin_line + "（%s）" % next(iter(chk.keys()))[:10]
        else:
            checkin_line = ""

        # 缺口
        gaps = self._butler_gap_analysis(d)
        gap_lines = "\n".join("  ⚠️ %s" % g for g in gaps[:6]) if gaps else "  ✅ 暂无关键缺口"

        rocks = wp.get("big_rocks") or []
        def _rock_desc(t):
            title = (t.get("title") or "").strip()
            note = (t.get("content") or "")
            m = re.search(r"大石头[：:]\s*([^\n]+)", note)
            why = m.group(1).strip() if m else ""
            return (title + ("（%s）" % why[:30] if why else "")).replace("\n", " ")
        rock_line = "、".join(_rock_desc(t)[:60] for t in rocks) or "（本周还没定大石头）"

        # 注入明确的当前时间（年月日+星期+时刻），让管家写相对日期/截止时换算有基准
        _WD = "一二三四五六日"
        _ts_line = ("【现在】%s 周%s %s（本周 %s-W%02d，周一 %s；距周末 %d 天）\n\n" % (
            today.isoformat(), _WD[today.weekday()], datetime.now().strftime("%H:%M"),
            today.isocalendar()[0], today.isocalendar()[1], monday,
            5 - today.weekday()))
        _tail = ("上面是用户的真实数据。你所有的建议和动作都必须基于这些具体条目、引用它们的名字；"
                 "绝不要编造不存在的任务/习惯/角色/关注条目。当用户要改/删某个条目时，从上面对应的名字里匹配，不要凭空造名字。")
        # 今日已完成明细（管家引用细节，不再凭计数猜）
        _done_block = ""
        try:
            _st = self._get_stats()
            if _st.get("today_completed"):
                _titles = "、".join(str(x)[:24] for x in (_st.get("titles") or [])[:8]) or "（无标题）"
                _done_block = "【今日已完成 %d 项】%s\n\n" % (_st["today_completed"], _titles)
        except Exception:
            _done_block = ""
        # 昨夜神谕注入（v55）：管家应知道夜观今晨对用户说了什么，建议与神谕同向、可引用不矛盾
        _oracle_line = ""
        try:
            _orc = _read_json(self.ORACLE_FILE, {}) or {}
            if _orc.get("available"):
                _oracle_line = ("【昨夜神谕（AI夜观，今晨已展示给用户；你的建议要与它同向，别矛盾，可自然引用）】\n"
                                "神谕：%s\n睡眠简报：%s\nP/PC 简报：%s\n今日焦点：%s\n\n" % (
                                    str(_orc.get("oracle") or "（无）")[:120],
                                    str(_orc.get("sleep_brief") or "（无）")[:80],
                                    str(_orc.get("ppc_brief") or "（无）")[:80],
                                    str(_orc.get("focus_tip") or "（无）")[:60]))
        except Exception:
            pass
        _ctx = (
            _ts_line +
            _oracle_line +
            "【本周】%s　本周大石头：%s\n\n" % (wp.get("week"), rock_line) +
            "【角色（%d）】\n%s\n\n" % (len(roles), role_block) +
            "【待办·未完成共 %d 项，节选】\n%s\n\n" % (len(tasks), task_block) +
            "【习惯（%d）】\n%s\n\n" % (len(habits), habit_block) +
            "【情感账户明细】\n%s\n\n" % rel_block +
            "【倾听笔记】\n%s\n\n" % lis_block +
            "【关注圈/影响圈】\n%s\n\n" % pro_block +
            "【语言转换记录】\n%s%s\n\n" % (log_block, checkin_line) +
            "【关系/复盘】%s\n\n" % rel_line +
            _done_block +
            ppc_block +
            "" +
            "【仪表盘缺口（优先指导用户完善）】\n%s\n\n" % gap_lines +
            "【教练建议（当前各页面最优先的引导卡，管家应据此深化而非重复）】\n%s\n\n" % self._coach_summary_for_butler()
        )
        # B7: token 预算守卫——超长时截断末尾低优先级块（明细/缺口/教练），保留角色/待办/习惯等核心
        _BUDGET = 14000
        if len(_ctx) > _BUDGET:
            _ctx = _ctx[: _BUDGET] + "\n…（上下文超长，已截断末尾的低优先级明细/缺口/教练块，核心数据保留）\n"
        return _ctx + _tail

    def _butler_gap_analysis(self, d=None):
        """分析仪表盘缺口，返回可操作的建议列表（供前端和管家共同使用）"""
        if d is None:
            d = cached("dash", self._get_dashboard_data)
        roles = d.get("roles") or []
        tasks = d.get("tasks") or []
        habits = d.get("habits") or []
        gd = d.get("chain_health", {})
        gaps = []
        # 角色层
        if not roles:
            gaps.append("还没有创建任何角色。建议从「学习者」「身体」「家人」开始，每个角色配一个使命宣言和季度目标。")
        else:
            for r in roles:
                if not r.get("key_results"):
                    gaps.append(f"角色「{r.get('name','')}」缺少关键结果(KR)——没有KR角色就只是标签，没法驱动行动。")
                if not r.get("mission"):
                    gaps.append(f"角色「{r.get('name','')}」缺少使命宣言——以终为始需要一句锚定方向的使命。")
        # 任务层
        if not tasks:
            gaps.append("还没有任何待办任务。建议从每个角色的季度目标拆出本周的一个具体行动。")
        else:
            roles_with_tasks = set()
            for t in tasks:
                content = t.get("content", "") or ""
                m = re.search(r"角色[：:]\s*(\S+)", content)
                if m:
                    roles_with_tasks.add(m.group(1))
            role_names = {r.get("name", "") for r in roles}
            for rn in role_names - roles_with_tasks:
                if rn:
                    gaps.append(f"角色「{rn}」还没有关联任务。建议为它创建一个本周可执行的具体行动。")
        # 习惯层
        if not habits:
            gaps.append("还没有创建任何习惯。从「每日阅读15分钟」「喝水8杯」这样的小习惯开始，先跑21天。")
        else:
            for h in habits:
                if not h.get("dimension"):
                    gaps.append(f"习惯「{h.get('name','')}」未归类到四维（身体/精神/智力/社会情感）。归类后才能在雷达图看到四维平衡。")
                    break  # 只报告第一条，避免重复
            # 角色↔习惯关联：有角色但没有角色被任何习惯关联，点破"角色目标与打卡习惯脱节"
            habit_roles = {h.get("role", "").strip() for h in habits if h.get("role")}
            for r in roles:
                rn = r.get("name", "").strip()
                if rn and rn not in habit_roles:
                    gaps.append(f"角色「{rn}」还没有关联习惯——角色目标要靠日常习惯落地，去习惯页把打卡习惯挂到它名下（如「早睡」→身体）。")

        # 关系层
        if gd.get("relationsThisWeek", 0) == 0:
            gaps.append("本周还没有记录情感账户。建议记一笔——哪怕只是一次认真的倾听。")
        # 复盘层
        if not gd.get("lastReviewWeek") or gd.get("lastReviewWeek") == "无":
            gaps.append("还没有做过复盘。每周花10分钟复盘，是习惯七「不断更新」的核心动作。")
        # 大石头
        wp = self.get_week_plan()
        if not (wp.get("big_rocks") or []):
            gaps.append("本周还没有定大石头。问自己：这周只做成一件事，哪件会让其他事都变简单？")
        # 影响圈
        pro = self._raw_proactive()
        if not _alive(pro.get("concerns") or []):
            gaps.append("关注圈/影响圈还是空的。建议把最近的焦虑写下来，分类到影响圈里——能控制的就行动，不能控制的就放下。")
        return gaps

    def butler_chat(self, data):
        msgs = data.get("messages", [])
        if not msgs:
            return {"success": False, "error": "缺少 messages"}
        ctx = cached("butler_ctx", lambda: self._butler_context()) + _butler_fail_text()
        prefs = _butler_prefs_text() + _butler_memory_text() + _user_profile_text() + _butler_rules_text()
        sys_content = BUTLER_SYSTEM_PROMPT + "\n" + prefs
        payload = [{"role": "system", "content": sys_content},
                   {"role": "user", "content": "以下是我的当前人生数据，请记住并在后续对话中据此给出建议：\n" + ctx}]
        payload.extend(msgs)
        r = _call_llm(payload, temperature=0.8, max_tokens=4200, tools=BUTLER_TOOLS, feature="butler")
        if "error" in r:
            # LLM 不可用：规则引擎兜底，避免直接报错
            text, actions = _butler_offline_reply(self)
            _save_butler_history(msgs + [{"role": "assistant", "content": text}])
            return {"success": True, "reply": text, "actions": actions, "offline": True}
        full = r.get("content", "")
        clean, rx_actions = _extract_action(full)
        actions = _actions_from_toolcalls(r.get("tool_calls") or []) or rx_actions
        if _butler_is_vent((msgs[-1].get("content") if msgs else "")):
            actions = []  # 纯倾诉安全网：抑制强教练人格误发的动作
        clean = clean or full
        if actions and not clean.strip():
            clean = "我已为你准备好下面的动作，确认后我来执行 ✅"
        # 持久化对话历史
        history = _load_butler_history()
        last_user = msgs[-1] if msgs else {}
        history.append({"role": "user", "content": last_user.get("content", "")})
        history.append({"role": "assistant", "content": full})
        _save_butler_history(history)
        # 自动沉淀长期记忆（后台线程：提炼本轮值得记住的 → 写入 memory；失败静默，不影响返回）
        try:
            _last_u = msgs[-1] if msgs else {}
            threading.Thread(target=_butler_memory_digest, args=([{"role": "user", "content": _last_u.get("content", "")}, {"role": "assistant", "content": full}],), daemon=True).start()
        except Exception:
            pass
        # 沉淀用户画像（后台线程，不阻塞返回；失败静默，绝不影响响应）
        try:
            _msgs = list(msgs); _reply = full
            threading.Thread(target=lambda: _update_user_profile(_msgs, _reply), daemon=True).start()
        except Exception:
            pass
        return {"success": True, "reply": clean or full, "actions": actions}

    def butler_stream(self, data):
        """SSE 流式聊天：yield SSE 格式数据块（动作走 function calling，LLM 不可用走规则兜底）"""
        msgs = data.get("messages", [])
        if not msgs:
            yield "data: " + json.dumps({"error": "缺少 messages"}, ensure_ascii=False) + "\n\n"
            return
        ctx = cached("butler_ctx", lambda: self._butler_context()) + _butler_fail_text()
        prefs = _butler_prefs_text() + _butler_memory_text() + _user_profile_text() + _butler_rules_text()
        sys_content = BUTLER_SYSTEM_PROMPT + "\n" + prefs
        payload = [{"role": "system", "content": sys_content},
                   {"role": "user", "content": "以下是我的当前人生数据，请记住并在后续对话中据此给出建议：\n" + ctx}]
        payload.extend(msgs)
        full_reply = ""
        tool_chunks = []
        errored = None
        stream = _call_llm_stream(payload, temperature=0.8, max_tokens=4200, tools=BUTLER_TOOLS, feature="butler")
        stream_finished = False
        try:
            for chunk in stream:
                if "_error" in chunk:
                    errored = chunk["_error"]
                    break
                if "delta" in chunk:
                    full_reply += chunk["delta"]
                    yield "data: " + json.dumps({"delta": chunk["delta"]}, ensure_ascii=False) + "\n\n"
                elif "tool_calls" in chunk:
                    tool_chunks = chunk["tool_calls"]
            stream_finished = True
        finally:
            try:
                if not stream_finished and full_reply:
                    # 不将截断回复沉淀到画像/长期记忆，也不生成可执行动作。
                    history = _load_butler_history()
                    history.extend([{"role": "user", "content": msgs[-1].get("content", "")},
                                    {"role": "assistant", "content": full_reply + "\n（回复中断，已保存部分）"}])
                    _save_butler_history(history)
            finally:
                close = getattr(stream, "close", None)
                if close:
                    close()
        if errored:
            # LLM 不可用：规则引擎兜底，避免白屏/卡死
            text, fb_actions = _butler_offline_reply(self)
            _save_butler_history(msgs + [{"role": "assistant", "content": text}])
            yield "data: " + json.dumps({"delta": text}, ensure_ascii=False) + "\n\n"
            yield "data: " + json.dumps({"done": True, "reply": text, "actions": fb_actions,
                                         "offline": True}, ensure_ascii=False) + "\n\n"
            return
        # 流结束：优先用 function calling 动作，缺失时回退正则
        atc = _actions_from_toolcalls(tool_chunks)
        clean, rx_actions = _extract_action(full_reply)
        actions = atc if atc else rx_actions
        if _butler_is_vent((msgs[-1].get("content") if msgs else "")):
            actions = []  # 纯倾诉安全网：抑制强教练人格误发的动作
        clean = clean or full_reply
        if actions and not clean.strip():
            # 模型只发了工具调用、没出声时的兜底，避免空白气泡（不声称已执行）
            clean = "我已为你准备好下面的动作，确认后我来执行 ✅"
        # 自动沉淀长期记忆（后台线程：提炼本轮值得记住的 → 写入 memory；失败静默，不影响 SSE）
        try:
            _last_u = msgs[-1] if msgs else {}
            threading.Thread(target=_butler_memory_digest, args=([{"role": "user", "content": _last_u.get("content", "")}, {"role": "assistant", "content": full_reply}],), daemon=True).start()
        except Exception:
            pass
        # B4: 持久化必须在 yield done 之前（客户端收到 done 即可能断连，之后代码不执行，历史会丢）
        history = _load_butler_history()
        last_user = msgs[-1] if msgs else {}
        history.append({"role": "user", "content": last_user.get("content", "")})
        history.append({"role": "assistant", "content": full_reply})
        _save_butler_history(history)
        # 沉淀用户画像（放后台线程：不阻塞 done 发送）
        try:
            _msgs = list(msgs); _reply = full_reply
            threading.Thread(target=lambda: _update_user_profile(_msgs, _reply), daemon=True).start()
        except Exception:
            pass
        yield "data: " + json.dumps({"done": True, "reply": clean, "actions": actions}, ensure_ascii=False) + "\n\n"

    def ai_draft_stream(self, data):
        """场景化 AI 起草（SSE）：data:{delta}... + data:{done,draft}"""
        scene = (data.get("scene") or "").strip()
        spec = AI_DRAFT_SCENES.get(scene)
        if not spec:
            yield "data: " + json.dumps({"error": f"未知场景 {scene}"}, ensure_ascii=False) + "\n\n"
            return
        user_input = (data.get("input") or "无").strip() or "无"
        try:
            ctx = spec["ctx"](self, data.get("role") or "")
        except Exception as e:
            ctx = f"（上下文构建失败：{e}）"
        prompt = spec["prompt"].replace("{ctx}", ctx).replace("{input}", user_input)
        # system 只放行为规范，任务+数据放 user——纯 system 在无思考模式下易被复读
        payload = [{"role": "system", "content": AI_DRAFT_COMMON},
                   {"role": "user", "content": prompt[len(AI_DRAFT_COMMON):]}]
        draft = ""
        for chunk in _call_llm_stream(payload, temperature=0.7, max_tokens=spec["max_tokens"], feature="draft"):
            if "_error" in chunk:
                yield "data: " + json.dumps({"error": chunk["_error"]}, ensure_ascii=False) + "\n\n"
                return
            if "delta" in chunk:
                draft += chunk["delta"]
                yield "data: " + json.dumps({"delta": chunk["delta"]}, ensure_ascii=False) + "\n\n"
        yield "data: " + json.dumps({"done": True, "draft": draft.strip()}, ensure_ascii=False) + "\n\n"

    def advisor_stream(self, data):
        """✦ 随行顾问：为每个内容块按需实时生成建议（SSE）。
        data: {scene, data(前端该块最新快照), input(可选，问答模式), mode(suggest|ask)}
        —— data 由前端在用户点开那一刻取该块最新内容，保证「及时」；后端只提供该块的领域理解与语气，保证「准」。"""
        scene = (data.get("scene") or "").strip()
        spec = ADVISOR_SCENES.get(scene)
        if not spec:
            yield "data: " + json.dumps({"error": f"未知场景 {scene}"}, ensure_ascii=False) + "\n\n"
            return
        mode = (data.get("mode") or "suggest").strip()
        _allow = ADVISOR_ACTIONS.get(scene) or []   # v55：本面板可发起的创建类动作白名单
        block = data.get("data") or {}
        try:
            block_s = json.dumps(block, ensure_ascii=False, default=str)
            if len(block_s) > 2200:   # 截断防止超长快照挤占 token
                block_s = block_s[:2200] + "…(截断)"
        except Exception:
            block_s = "（快照解析失败）"
        advice_id = "adv-" + hashlib.sha256((scene + "|" + str(time.time_ns()) + "|" + block_s).encode("utf-8")).hexdigest()[:20]
        _fresh = block.get("freshness") if isinstance(block, dict) else {}
        _fresh = _fresh if isinstance(_fresh, dict) else {}
        _ingest = block.get("ingest") if isinstance(block, dict) else None
        if not isinstance(_ingest, dict):
            _ingest = {"status": _fresh.get("ingest_status", ""),
                       "latest_receive_at": _fresh.get("latest_receive_at", "")}
        _fetched = (_fresh.get("fetched_at") or _fresh.get("health_fetched_at")
                    or _fresh.get("workout_fetched_at")
                    or (block.get("fetched_at") if isinstance(block, dict) else None))
        evidence = build_evidence(
            "advisor:" + scene, block,
            window_days=_fresh.get("window_days") or block.get("window_days") if isinstance(block, dict) else None,
            source=(block.get("source") if isinstance(block, dict) else None) or "dashboard_snapshot",
            fetched_at=_fetched,
            ingest=_ingest,
        )
        evidence_line = "\n\n【证据包元数据】\n" + json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
        # 健康看板板块聚焦：✦ 按钮带 focus=板块key，让 AI 重点解读该板块而非泛泛全貌
        focus_line = ""
        if scene == "health" and isinstance(block, dict):
            _fk = str(block.get("focus") or "").strip()
            if _fk in HEALTH_FOCUS:
                focus_line = ("\n\n【板块聚焦】用户此刻正盯着「" + HEALTH_FOCUS[_fk][0]
                              + "」这一张卡：" + HEALTH_FOCUS[_fk][1]
                              + " 快照里其他指标只作背景关联，主体解读与建议必须落在这块。")
        # HEALTH-ABC-E2: 健康场景注入组合解读上下文（个人基线 + 趋势 + 组合信号 + 数据质量），
        # 让 AI 用「组合逻辑 + 个人基线」解读，而非单指标罗列
        if scene == "health":
            try:
                _ctx = self._health_advisor_context()
                if _ctx:
                    focus_line += "\n\n【组合解读上下文（后端据近14天真实数据算好）】\n" + _ctx
            except Exception:
                pass
        if mode == "ask":
            user_input = (data.get("input") or "").strip()[:500] or "（未填写）"
            user_msg = (spec.get("domain", "") + evidence_line + "\n\n【本块当前数据】\n" + block_s + focus_line
                        + "\n\n" + _advisor_memory_text(scene)
                        + "用户在「" + spec.get("title", scene) + "」的提问/补充：\n" + user_input)
            # v55：ask 模式告知可发起的创建类动作（suggest 模式不给，纯建议绝不发动作）
            if _allow:
                user_msg += ("\n\n【本面板可发起的动作】用户明确要落地时才调用 do_action 工具（一次最多 2 个，仅创建类）：\n"
                             + "\n".join("· %s %s" % (k, h) for k, h in _allow)
                             + "\n用户只是提问、要建议、倾诉时绝不调用；动作参数用中文键、取自本块真实数据，别编造。")
        else:
            user_msg = (spec.get("domain", "") + evidence_line + "\n\n【本块当前数据】\n" + block_s + focus_line
                        + "\n\n" + spec.get("suggest", "请给出具体、可落地的建议。"))
        # v55：健康场景注入专业解读框架（三句式/多指标组合/恢复三档/RHR梯度/睡眠债分级/就医指征）
        # ask 模式再注入本面板动作协议（系统级遵守度高于 user 消息尾部提示）
        _sys = (ADVISOR_COMMON + ADVISOR_KERNEL
                + ("\n" + ADVISOR_HEALTH_KERNEL if scene == "health" else "")
                + ("\n" + _advisor_action_protocol(_allow) if (mode == "ask" and _allow) else "")
                + "\n" + _user_profile_text())
        payload = [{"role": "system", "content": _sys},
                   {"role": "user", "content": user_msg}]
        # v55：ask 模式挂工具（仅本面板白名单的创建类动作）；suggest 模式不挂，纯建议绝不发动作
        _tools = _build_advisor_tools(_allow) if (mode == "ask" and _allow) else None
        _mt = 700 if _tools else spec.get("max_tokens", 420)
        text = ""
        tool_chunks = []
        _llm_unavailable = False
        for chunk in _call_llm_stream(payload, temperature=0.8, max_tokens=_mt, tools=_tools, feature="advisor"):
            if "_error" in chunk:
                # LLM 未配置/不可用：不再裸 error 断流——发降级说明 + done 帧，
                # 让前端（含 regen 计数）走正常完成路径，而不是等 done 超时。
                _llm_unavailable = True
                yield "data: " + json.dumps({
                    "delta": "（AI 服务未配置或暂不可用。配置 LLM_API_BASE/LLM_API_KEY 后，"
                             "这里会基于当前数据生成建议。）",
                    "degraded": True,
                }, ensure_ascii=False) + "\n\n"
                break
            if "delta" in chunk:
                text += chunk["delta"]
                yield "data: " + json.dumps({"delta": chunk["delta"]}, ensure_ascii=False) + "\n\n"
            elif "tool_calls" in chunk:
                tool_chunks = chunk["tool_calls"]
        if _llm_unavailable:
            yield "data: " + json.dumps({
                "done": True, "text": "（AI 服务未配置或暂不可用。配置 LLM_API_BASE/LLM_API_KEY 后，这里会基于当前数据生成建议。）",
                "actions": [], "adviceId": advice_id, "proposalId": advice_id,
                "evidence": {"degraded": True, "reason": "llm_unconfigured",
                              "generated_at": datetime.now().isoformat(timespec="seconds")},
            }, ensure_ascii=False) + "\n\n"
            return
            if "delta" in chunk:
                text += chunk["delta"]
                yield "data: " + json.dumps({"delta": chunk["delta"]}, ensure_ascii=False) + "\n\n"
            elif "tool_calls" in chunk:
                tool_chunks = chunk["tool_calls"]
        if mode == "ask" and text.strip():
            _append_advisor_memory(scene, data.get("input") or "", text.strip())
        # v55：动作双通道（与管家一致）——工具优先、文本标记 <action> 兜底；白名单过滤 + 最多 2 条；
        # 正文剥离标记后输出；只发动作没出声时给兜底话术（不声称已执行）
        clean_text = text.strip()
        actions = []
        if mode == "ask" and _allow:
            _wl = {k for k, _ in _allow}
            for a in (_actions_from_toolcalls(tool_chunks) or []):
                if (a.get("kind") in _wl) and len(actions) < 2:
                    actions.append(a)
            _clean, _rx = _extract_action(text) if text else (text, [])
            for a in (_rx or []):
                if (a.get("kind") in _wl) and len(actions) < 2:
                    actions.append(a)
            if _clean.strip():
                clean_text = _clean.strip()
            elif actions:
                clean_text = "我已为你准备好下面的动作，确认后我来执行 ✅"
            # v55：单次自纠重试——落地意图明确但两条通道都空白（ark 智能路由偶发给不发工具的模型）。
            # 带纠错提示补一轮，只补动作不重写正文；仍拿不到就如实以建议收尾（不谎报）。
            if not actions and re.search(r"建成|帮我建|建任务|转成任务|设个提醒|定时提醒|加个任务|添加任务|记一下|存进|写进",
                                         (data.get("input") or "")):
                try:
                    _msgs2 = list(payload) + [
                        {"role": "assistant", "content": text or "（未发动作）"},
                        {"role": "user", "content": "系统纠错：你刚才没有调用工具、也没有输出 <action> 标记，确认卡不会出现。"
                                                     "请现在补发：调用 do_action 工具，或输出 <action>类型</action>{JSON参数}。"
                                                     "只补动作，不要重复建议。"},
                    ]
                    _t2, _tc2 = "", []
                    for chunk in _call_llm_stream(_msgs2, temperature=0.5, max_tokens=500, tools=_tools, feature="advisor_retry"):
                        if "_error" in chunk:
                            break
                        if "delta" in chunk:
                            _t2 += chunk["delta"]
                        elif "tool_calls" in chunk:
                            _tc2 = chunk["tool_calls"]
                    for a in (_actions_from_toolcalls(_tc2) or []):
                        if (a.get("kind") in _wl) and len(actions) < 2:
                            actions.append(a)
                    _c2, _rx2 = _extract_action(_t2) if _t2 else ("", [])
                    for a in (_rx2 or []):
                        if (a.get("kind") in _wl) and len(actions) < 2:
                            actions.append(a)
                except Exception:
                    pass
        evidence["generated_at"] = datetime.now().isoformat(timespec="seconds")
        evidence["generatedAt"] = evidence["generated_at"]  # 前端旧契约兼容
        yield "data: " + json.dumps({
            "done": True, "text": clean_text, "actions": actions, "adviceId": advice_id,
            "proposalId": advice_id, "evidence": evidence,
        }, ensure_ascii=False) + "\n\n"

    def advisor_fill(self, data):
        """✦ 随行顾问 · 填充模式：直接生成该块的真实内容（结构化 JSON），供前端写入区块。
        与建议(解读)不同：用户核心需求是「把内容填出来」，不是看解读。
        data: {scene, data(前端该块最新快照), count(可选, 生成条数)}
        返回 {success, scene, fill:[...], note} —— fill 的每条结构由 scene 的 fill.keys 定义。"""
        scene = (data.get("scene") or "").strip()
        spec = ADVISOR_SCENES.get(scene)
        if not spec or not spec.get("fill"):
            return {"success": False, "error": f"「{scene}」暂不支持 AI 填充"}
        block = data.get("data") or {}
        count = int(data.get("count") or 3)
        count = max(1, min(count, 8))
        try:
            block_s = json.dumps(block, ensure_ascii=False, default=str)
            if len(block_s) > 2200:
                block_s = block_s[:2200] + "…(截断)"
        except Exception:
            block_s = "（快照解析失败）"
        fill = spec["fill"]
        payload = [{"role": "system", "content": ADVISOR_COMMON + fill.get("common", "")},
                   {"role": "user", "content": (spec.get("domain", "")
                        + "\n\n【本块当前数据】\n" + block_s
                        + "\n\n" + fill["prompt"].replace("{count}", str(count)))}]
        try:
            r = _call_llm(payload, temperature=0.8, max_tokens=1000, json_mode=True, feature="advisor_fill")
        except Exception as e:
            return {"success": False, **_client_error(e, "upstream_error", "advisor_fill")}
        # P2-V4: 先透传真实 error，别吞成「AI 未返回内容」（与 ai_socratic_compile 错误分支对齐）；
        # ZC-QA-1：透传前经 _scrub_client_text 清洗（上游文本可能含 URL/路径片段）
        if isinstance(r, dict) and r.get("error"):
            # 开源版：未配置 LLM 是预期降级（HTTP 200 + success:false），不以 400 呈现
            return {"success": False, "degraded": True,
                    "error": "AI 填充未启用：配置 LLM_API_BASE / LLM_API_KEY 后可用。"}
        text = r.get("text") or r.get("content") or ""
        if not text:
            return {"success": False, "error": "AI 未返回内容，请重试"}
        items = self._parse_fill_json(text)
        if not items:
            return {"success": False, "error": "AI 返回结构无法解析，请重试", "raw": text[:300]}
        return {"success": True, "scene": scene, "count": count, "fill": items,
                "suggestionId": "fill-" + hashlib.sha256((scene + "|" + block_s).encode("utf-8")).hexdigest()[:20],
                "evidence": build_evidence("advisor_fill:" + scene, block, source="dashboard_snapshot")}

    def _munger_global_block(self, gctx):
        """B① 跨厅记忆：把前端附带的全馆动态拼成一段可注入的全局上下文。"""
        if not isinstance(gctx, dict) or not gctx:
            return ""
        try:
            g_s = json.dumps(gctx, ensure_ascii=False, default=str)
            if len(g_s) > 1400:
                g_s = g_s[:1400] + "…(截断)"
        except Exception:
            return ""
        return ("\n\n【全局上下文 · 用户在本馆其他厅的动态（跨厅记忆）】\n" + g_s +
                "\n（与本块数据冲突时以本块为准；其余场合自然沿用这些背景，让建议跨厅连贯，"
                "不要重新问用户已经填过的事）")

    def munger_advisor_stream(self, data):
        """✦ 书房随行顾问：为每个芒格工具按需实时生成建议（SSE）。
        data: {scene, data(前端该块最新快照), input(可选，问答模式), mode(suggest|ask)}
        —— 与 advisor_stream 同构，仅场景表换为 MUNGER_SCENES。"""
        scene = (data.get("scene") or "").strip()
        spec = MUNGER_SCENES.get(scene)
        if not spec:
            yield "data: " + json.dumps({"error": f"未知场景 {scene}"}, ensure_ascii=False) + "\n\n"
            return
        mode = (data.get("mode") or "suggest").strip()
        block = data.get("data") or {}
        try:
            block_s = json.dumps(block, ensure_ascii=False, default=str)
            if len(block_s) > 2200:
                block_s = block_s[:2200] + "…(截断)"
        except Exception:
            block_s = "（快照解析失败）"
        evidence = build_evidence("munger:" + scene, block, source="dashboard_snapshot")
        # B① 跨厅记忆：前端附带的全馆动态（决策台进行中/博物馆勾选/近期日志），
        # 注入后 8 个场景共享同一位顾问的记忆——不再每个厅从零开始。
        g_block = self._munger_global_block(data.get("global"))
        if mode == "ask":
            user_input = (data.get("input") or "").strip()[:500] or "（未填写）"
            user_msg = (spec.get("domain", "") + g_block + "\n\n【本块当前数据】\n" + block_s
                        + "\n\n用户在「" + spec.get("title", scene) + "」的提问/补充：\n" + user_input)
        else:
            user_msg = (spec.get("domain", "") + g_block + "\n\n【本块当前数据】\n" + block_s
                        + "\n\n" + spec.get("suggest", "请给出具体、可落地的建议。"))
        payload = [{"role": "system", "content": MUNGER_COMMON + MUNGER_KERNEL},
                   {"role": "user", "content": user_msg}]
        text = ""
        for chunk in _call_llm_stream(payload, temperature=0.8, max_tokens=spec.get("max_tokens", 420), feature="munger"):
            if "_error" in chunk:
                yield "data: " + json.dumps({"error": chunk["_error"]}, ensure_ascii=False) + "\n\n"
                return
            if "delta" in chunk:
                text += chunk["delta"]
                yield "data: " + json.dumps({"delta": chunk["delta"]}, ensure_ascii=False) + "\n\n"
        evidence["generated_at"] = datetime.now().isoformat(timespec="seconds")
        evidence["generatedAt"] = evidence["generated_at"]
        yield "data: " + json.dumps({"done": True, "text": text.strip(),
                                      "evidence": evidence}, ensure_ascii=False) + "\n\n"

    def munger_advisor_fill(self, data):
        """✦ 书房随行顾问 · 填充模式：直接生成该工具的真实内容（结构化 JSON）。
        data: {scene, data(前端该块最新快照), count(可选)}
        返回 {success, scene, count, fill:[...]} —— fill 的每条结构由 scene 的 fill.keys 定义。"""
        scene = (data.get("scene") or "").strip()
        spec = MUNGER_SCENES.get(scene)
        if not spec or not spec.get("fill"):
            return {"success": False, "error": f"「{scene}」暂不支持 AI 填充"}
        block = data.get("data") or {}
        count = int(data.get("count") or 3)
        count = max(1, min(count, 8))
        try:
            block_s = json.dumps(block, ensure_ascii=False, default=str)
            if len(block_s) > 2200:
                block_s = block_s[:2200] + "…(截断)"
        except Exception:
            block_s = "（快照解析失败）"
        g_block = self._munger_global_block(data.get("global"))
        fill = spec["fill"]
        prompt = fill["prompt"].replace("{count}", str(count))
        goal = (block.get("goal") or "").strip()
        prompt = prompt.replace("{goal}", goal or "这件事")
        payload = [{"role": "system", "content": MUNGER_COMMON + fill.get("common", "")},
                   {"role": "user", "content": (spec.get("domain", "")
                        + g_block + "\n\n【本块当前数据】\n" + block_s
                        + "\n\n" + prompt)}]
        try:
            r = _call_llm(payload, temperature=0.8, max_tokens=1400, json_mode=True, feature="munger_fill")
        except Exception as e:
            return {"success": False, "error": f"填充失败：{e}"}
        # P2-V4: 先透传真实 error，别吞成「AI 未返回内容」；ZC-QA-1：透传前清洗上游文本
        if isinstance(r, dict) and r.get("error"):
            return {"success": False, "error": _scrub_client_text(str(r["error"]), 200)}
        text = r.get("text") or r.get("content") or ""
        if not text:
            return {"success": False, "error": "AI 未返回内容，请重试"}
        items = self._parse_fill_json(text)
        if not items:
            return {"success": False, "error": "AI 返回结构无法解析，请重试", "raw": text[:300]}
        return {"success": True, "scene": scene, "count": count, "fill": items,
                "suggestionId": "fill-" + hashlib.sha256((scene + "|" + block_s).encode("utf-8")).hexdigest()[:20],
                "evidence": build_evidence("munger_fill:" + scene, block, source="dashboard_snapshot")}

    def _parse_fill_json(self, text):
        """从 AI 输出中稳健地摘出 JSON 数组（容忍 ```json 包裹/前后缀杂质/裸字符串数组）。"""
        def norm(v):
            if not isinstance(v, list):
                return []
            out = []
            for x in v:
                if isinstance(x, dict):
                    out.append(x)
                elif isinstance(x, str) and x.strip():
                    out.append({"text": x.strip()})
            return out
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
        try:
            # 直接是数组
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return norm(parsed)
            # 是对象
            if isinstance(parsed, dict):
                for k in ("fill", "items", "rocks", "data", "result", "list", "entries", "output", "text"):
                    v = parsed.get(k)
                    if isinstance(v, list):
                        got = norm(v)
                        if got:
                            return got
                # doubao json_object 模式下模型常把内容塞进任意对象字段，
                # 兜底：取第一个「值是非空数组」的字段
                for k, v in parsed.items():
                    if isinstance(v, list) and v:
                        got = norm(v)
                        if got:
                            return got
        except Exception:
            pass
        # 退化：从文本里摘第一个 [...] 数组
        m = re.search(r"\[[\s\S]*\]", text)
        if m:
            try:
                parsed = json.loads(m.group(0))
                if isinstance(parsed, list):
                    return norm(parsed)
            except Exception:
                pass
        return []

    def _suggest_task_title(self, role_name, roles):
        """从角色的 KR / 季度目标拆一个建议任务标题（纯规则，不调 LLM）"""
        for r in roles:
            if r.get("name") != role_name:
                continue
            for kr in (r.get("key_results") or []):
                if isinstance(kr, dict):
                    t = (kr.get("text") or "").strip()
                    done = kr.get("done")
                else:
                    t = str(kr or "").strip()
                    done = False
                if t and not done:
                    return ("推进：" + t)[:60]
            goal = (r.get("goal") or "").strip()
            if goal:
                first = re.split(r"[。；;\n]", goal)[0].strip()
                # 占位符/空文案防呆：不要把这些字面建进可创建的任务标题
                if first and first not in ("（点击编辑）", "点击编辑") and not first.startswith("点击编辑"):
                    return ("「" + role_name + "」本周行动：" + first)[:60]
            break
        return f"「{role_name}」本周行动：从季度目标拆解一件小事"

    def coach_cards(self):
        """强教练规则引擎：纯规则（不调 LLM），返回引导卡 + 今日焦点"""
        if _demo_enabled():
            return {"demo": True, "cards": [
                {"id": "demo-c1", "severity": "info", "insight": "演示数据：本周睡眠均值 7.0h，周二最低。",
                 "cta": None, "cid": "", "view": "health"},
                {"id": "demo-c2", "severity": "warn", "insight": "演示数据：情感账户本周出现一笔取款，建议做一笔存款对冲。",
                 "cta": {"label": "去记一笔", "view": "relations", "focus": ""}, "cid": "", "view": "relations"},
            ]}
        d = cached("dash", self._get_dashboard_data)
        roles = d.get("roles") or []
        tasks = d.get("tasks") or []
        habits = d.get("habits") or []
        gd = d.get("chain_health", {})
        cards = []
        sev = {"info": 1, "suggest": 2, "urgent": 3}

        def add(view, severity, insight, cta=None, cid=""):
            cards.append({"id": cid or f"{view}_{len(cards)}", "view": view,
                          "severity": sev.get(severity, 1), "insight": insight, "cta": cta or None})

        if not roles:
            add("roles", "urgent", "驾驶舱还没有角色——这是整个系统的地基。从「学习者」或「身体」开始建第一个。",
                {"label": "去创建角色", "modal": "mo-role", "focus": "#fr-name"})
        else:
            no_kr = [r for r in roles if not r.get("key_results")]
            if no_kr:
                add("roles", "suggest",
                    f"角色「{no_kr[0].get('name', '')}」还没有关键结果(KR)，没有 KR 的角色只是标签。",
                    {"label": "去补 KR", "view": "roles", "focus": "#role-%s" % no_kr[0].get("id", "")})
            no_mission = [r for r in roles if not r.get("mission")]
            if no_mission:
                add("roles", "suggest",
                    f"角色「{no_mission[0].get('name', '')}」缺使命宣言——以终为始需要一句锚定方向的话。",
                    {"label": "去写使命", "view": "roles", "focus": "#role-%s" % no_mission[0].get("id", "")})
        role_names = {r.get("name", "") for r in roles if r.get("name")}
        roles_with_tasks = set()
        for t in tasks:
            m = re.search(r"角色[：:]\s*(\S+)", t.get("content") or "")
            if m:
                roles_with_tasks.add(m.group(1))
        for rn in list(role_names - roles_with_tasks)[:1]:
            if rn:
                sug = self._suggest_task_title(rn, roles)
                add("actions", "suggest",
                    f"角色「{rn}」本周还没有关联任务。建议行动：{sug}",
                    {"label": "✚ 一键建任务", "api": "/api/tasks/create",
                     "body": {"title": sug, "project": "四象限", "quad": "q2",
                              "role": rn, "priority": 3},
                     "alt": {"label": "手动建", "view": "actions"}})
        dim_low = [h for h in habits if h.get("weekChecked", 0) < 4 and h.get("weekTotal", 0) >= 1]
        if habits and len(dim_low) >= 3:
            add("habits", "suggest", f"本周有 {len(dim_low)} 个习惯打卡不足 4 次，四维平衡在滑坡。",
                {"label": "去打卡", "view": "habits", "focus": ".habit-card:not(.checked)"})
        streak_good = [h for h in habits if h.get("streak", 0) >= 7]
        if streak_good:
            add("habits", "info",
                f"「{streak_good[0]['name']}」已连续 {streak_good[0]['streak']} 天——这个势能值得守住。",
                {"label": "查看", "view": "habits", "focus": ".habit-card.checked"})
        ppc = gd.get("ppc") or {}
        if ppc.get("available"):
            st = ppc.get("state")
            if st == "hungry_goose":
                add("habits", "suggest",
                    f"鹅与金蛋失衡：产出 {ppc['p']}/100 但产能只有 {ppc['pc']}/100——光下蛋、饿着鹅。先给习惯打卡。",
                    {"label": "🦢 去养鹅", "view": "habits", "focus": ".habit-card:not(.checked)"})
            elif st == "idle_goose":
                add("actions", "suggest",
                    f"鹅与金蛋失衡：产能 {ppc['pc']}/100 但产出只有 {ppc['p']}/100——鹅养得好，该下蛋了。回到本周大石头。",
                    {"label": "🥚 回大石头", "view": "actions", "focus": "#rock-title"})
        # 健康规则（Apple Watch 实测数据，纯规则秒出）
        try:
            _hd = self._get_health(7)
            _rd = self._health_readiness(_hd)
        except Exception:
            _hd, _rd = {}, {"available": False}
        if _rd.get("available"):
            _sl = (_hd.get("sleep") or [])
            _lows = [x for x in _sl if (x.get("total") or 0) < 6]
            # 睡眠调节派生指标（CBT-I 口径）：睡眠债 / 滴定熄灯 / 呼吸率
            _dv = _hd.get("derived") or {}
            _debt = _dv.get("sleep_debt")
            _rb = _dv.get("rec_bedtime")
            _fh = lambda h: "%02d:%02d" % (int(h) % 24, round((h % 1) * 60)) if h is not None else None
            if _debt is not None and _debt >= 5:
                _bed = _fh(_rb) or "23:00"
                add("today", "urgent",
                    "近 7 晚睡眠债 %.1f 小时（%d 晚不足 6h）——这不是意志力问题，是相位被推后了。锚定固定起床时间，今晚 %s 熄灯。" % (_debt, len(_lows), _bed),
                    {"label": "✚ 建睡觉任务", "api": "/api/tasks/create",
                     "body": {"title": "今晚 %s 熄灯睡觉" % _bed, "project": "四象限", "quad": "q2", "priority": 3},
                     "alt": {"label": "看睡眠滴定", "view": "health"}})
            elif len(_lows) >= 3:
                add("today", "urgent",
                    "连续 %d 晚睡眠不足 6 小时——鹅真的快饿死了。今晚 23:00 前熄灯，明天的一切都会变轻。" % len(_lows),
                    {"label": "✚ 建睡觉任务", "api": "/api/tasks/create",
                     "body": {"title": "今晚 23:00 前熄灯睡觉", "project": "四象限", "quad": "q2", "priority": 3},
                     "alt": {"label": "看健康趋势", "view": "health"}})
            if _rd.get("score", 100) < 50:
                add("health", "suggest",
                    "今日恢复度 %d/100（弱项：%s）——身体在报警，把硬仗挪到明天，今天只做轻量推进。" % (
                        _rd["score"], _rd.get("weak") or "多项"),
                    {"label": "和管家聊聊", "view": "butler"})
            _ex = [x for x in (_hd.get("exercise") or []) if (x.get("qty") or 0) > 0]
            if (_hd.get("days") or []) and len(_ex) == 0:
                add("health", "info",
                    "Apple Watch 记录到近 7 天没有锻炼时间。散步 20 分钟也算，鹅需要动一动。",
                    {"label": "✚ 建散步任务", "api": "/api/tasks/create",
                     "body": {"title": "今日散步 20 分钟", "project": "四象限", "quad": "q2", "priority": 3}})
            if _debt is not None and 2.5 <= _debt < 5:
                add("health", "suggest",
                    "近 7 晚睡眠债 %.1f 小时在累积。把今晚的硬仗挪到明天，睡前 1 小时调暗屏幕。" % _debt,
                    {"label": "看睡眠滴定", "view": "health", "focus": "#health-cbti-card"})
            if _rb is not None and _dv.get("avg_bedtime") is not None and (_dv["avg_bedtime"] - _rb) % 24 > 1.5:
                add("health", "suggest",
                    "你的平均就寝 %s，但按睡眠效率滴定（效率 %s%%），建议熄灯 %s——今晚先试这一晚，不困不上床。" % (
                        _fh(_dv["avg_bedtime"]), _dv.get("sleep_eff") or "—", _fh(_rb)),
                    {"label": "和管家聊聊", "view": "butler"})
            _rp = _dv.get("resp_avg")
            if _rp is not None and _rp >= 14.5:
                add("health", "info",
                    "近 7 天平均呼吸 %.1f 次/分（偏快，共振目标约 6 次/分）——呼吸浅快说明副交感长期缺席。看板内置呼吸引导，练 2 分钟即可。" % _rp,
                    {"label": "去呼吸引导", "view": "health", "focus": "#breath-start"})
        if gd.get("relationsThisWeek", 0) == 0:
            add("relations", "info", "本周还没有情感账户记录。哪怕一次认真的倾听，也值得记一笔。",
                {"label": "去记录", "view": "relations", "focus": "#rel-who"})
        wp = self.get_week_plan()
        if not (wp.get("big_rocks") or []):
            add("today", "suggest", "本周还没有定大石头。问自己：这周只做成一件事，哪件会让其他事都变简单？",
                {"label": "🪨 定大石头", "view": "actions", "focus": "#rock-title"})
        import datetime as _dt
        if _dt.date.today().weekday() == 6:  # 周日
            if not gd.get("lastReviewWeek") or gd.get("lastReviewWeek") == "无":
                add("review", "urgent", "周日了，本周复盘还没写。我根据你的数据准备了 AI 引导式复盘。",
                    {"label": "开始引导式复盘", "socratic": "weekly_review"})
        pro = self._raw_proactive()
        active_concerns = _alive(pro.get("concerns") or [])
        if active_concerns and not any(c.get("circle") == "influence" for c in active_concerns):
            add("proactive", "info", "关注圈里有待转化的焦虑。AI 可以帮你把每条拆成可控/不可控/最小行动。",
                {"label": "AI 转化建议", "view": "proactive", "focus": "#pro-draft"})

        cards.sort(key=lambda c: -c["severity"])
        focus = None
        if cards:
            c0 = cards[0]
            focus = {"title": "今天最该做的一件事",
                     "action": c0["insight"].split("。")[0][:60] or c0["insight"][:60],
                     "basis": c0["insight"],
                     "cta": c0.get("cta") or {"label": "去看看", "view": c0["view"]}}
        else:
            focus = {"title": "今天最该做的一件事",
                     "action": "驾驶舱各链路完整，挑一件第二象限的事投资自己",
                     "basis": "缺口分析为空：角色/KR/任务/习惯/关系/复盘链路均已就绪",
                     "cta": {"label": "看看管家", "view": "butler"}}
        return {"cards": cards[:6], "todayFocus": focus}

    def _coach_summary_for_butler(self):
        """压缩 coach cards 摘要注入管家上下文，让管家知道当前各页面有什么引导建议，以深化而非重复"""
        try:
            cc = self.coach_cards().get("cards", [])
        except Exception:
            return ""
        if not cc:
            return "（暂无活跃引导卡）"
        lines = []
        for c in cc[:5]:
            sev = {1: "提示", 2: "建议", 3: "紧急"}.get(c.get("severity", 1), "提示")
            lines.append("  [%s] %s · %s" % (c["view"], sev, c.get("insight", "")[:60]))
        return "\n".join(lines)

    def ai_socratic(self, data):
        """苏格拉底引导：返回第 step 个问题 + 数据 hint"""
        scene = (data.get("scene") or "").strip()
        flow = SOCRATIC_FLOWS.get(scene)
        if not flow:
            return {"success": False, "error": f"未知场景 {scene}"}
        step = int(data.get("step", 0) or 0)
        qs = flow["questions"]
        if step < 0 or step >= len(qs):
            return {"success": False, "error": f"step 越界（0-{len(qs)-1}）"}
        q, hint_fn = qs[step]
        role = (data.get("role") or "").strip()
        hint = ""
        try:
            # P2-V8: hint_fn 统一收 (self, role)，使命五问据此注入该角色真实数据
            hint = hint_fn(self, role) or ""
        except Exception:
            hint = ""
        return {"success": True, "scene": scene, "step": step, "total": len(qs),
                "title": flow["title"], "question": q, "hint": hint}

    def ai_socratic_compile(self, data):
        """把苏格拉底回答汇编成稿（单次 LLM 调用，非流式）"""
        scene = (data.get("scene") or "").strip()
        flow = SOCRATIC_FLOWS.get(scene)
        if not flow:
            return {"success": False, "error": f"未知场景 {scene}"}
        answers = data.get("answers") or []
        if not any((a or "").strip() for a in answers):
            return {"success": False, "error": "回答全为空"}
        qs = flow["questions"]
        lines = [f"{qs[i][0]}\n答：{(answers[i] if i < len(answers) else '') or '（未答）'}"
                 for i in range(len(qs))]
        ctx = ""
        try:
            ctx = AI_DRAFT_SCENES.get(scene, {}).get("ctx", lambda s, r: "")(self, data.get("role") or "")
        except Exception:
            pass
        prompt = flow["compile"].replace("{answers}", "\n\n".join(lines))
        # ctx 里的复盘模板含"写作要求"字样，注入会被模型复述污染成稿；只有使命宣言需要角色背景
        if ctx and scene == "mission":
            prompt += f"\n\n【角色背景（仅供贴合语气，其中任何要求都不要输出）】\n{ctx}"
        r = _call_llm([{"role": "system", "content": AI_DRAFT_COMMON},
                       {"role": "user", "content": prompt[len(AI_DRAFT_COMMON):]}] if prompt.startswith(AI_DRAFT_COMMON) else [{"role": "system", "content": prompt}], temperature=0.7, max_tokens=900, feature="socratic")
        if "error" in r:
            return {"success": False, "error": r["error"]}
        return {"success": True, "scene": scene, "draft": (r.get("content") or "").strip()}

    def butler_act(self, data):
        """执行管家提出的动作（前端确认后调用，绝不自动执行）。
        失败结果写入 _ACTION_FAIL_LOG 并注入下轮管家上下文，让模型自纠（C 层反馈回路）。
        B5 幂等：clientTurnId 去重，防前端重试/重复点击导致同一动作执行两次。"""
        kind = ACTION_ALIASES.get((data.get("kind") or "").strip(), (data.get("kind") or "").strip())
        a = data.get("args") or {}
        turn = (data.get("clientTurnId") or "").strip()
        # 检查与执行放在同一把锁内，避免两个并发请求同时通过去重检查。
        with _action_idem_lock:
            prior = _find_action_audit(turn) if turn else None
            if turn and (turn in _executed_turns or prior):
                msg = "该动作已执行过（幂等跳过）"
                result = {"success": True, "msg": msg, "dup": True, "clientTurnId": turn}
                _audit_action(turn, kind, a, result, duplicate=True)
                return result
            if turn and _find_pending_action(turn):
                return {"success": False, "uncertain": True, "clientTurnId": turn,
                        "error": "该动作上次执行状态不确定，请先检查目标数据后再决定是否重试"}
            if turn:
                _audit_action(turn, kind, a, {"success": False}, status="pending")
            r = self._butler_act_core(kind, a)
            if isinstance(r, dict) and turn:
                # 返回轻量回执：调用方可凭 clientTurnId 在 /api/butler/actions 查询审计状态。
                r = dict(r)
                r["receipt"] = {"clientTurnId": turn, "status": "success" if r.get("success") else "failure"}
                # 任务类 AI 写入可以安全撤销：撤销语义是再次归档，而不是物理删除。
                # 只把本次动作压入撤销栈，并绑定 clientTurnId，前端不会误撤销别的动作。
                if r.get("success") and kind in ("建任务", "定大石头") and isinstance(r.get("task"), dict):
                    _task = r.get("task") or {}
                    _tid, _pid = _task.get("id"), (_task.get("projectId") or _task.get("pid"))
                    if _tid and _pid:
                        _undo_ok = _undo_push("管家新增任务", "ticktick_archive", "TickTick任务",
                                              {"id": _tid, "projectId": _pid, "clientTurnId": turn}, turn)
                        if _undo_ok:
                            r["undoable"] = True
                            r["undoLabel"] = "归档这条任务"
            # 只有真实成功才记入幂等表；失败动作必须允许用同一 clientTurnId 重试。
            if turn and isinstance(r, dict) and r.get("success"):
                _executed_turns.append(turn)
            if isinstance(r, dict) and not r.get("success") and kind:
                _log_action_fail(kind, a, r.get("error") or r.get("msg") or "未知错误")
            if turn:
                _audit_action(turn, kind, a, r, status="success" if isinstance(r, dict) and r.get("success") else "failure")
            return r

    def _butler_act_core(self, kind, a):
        g = lambda *ks: next((a[k] for k in ks if a.get(k)), "")
        if _demo_enabled() and kind in ("建任务", "删任务", "改任务", "建习惯", "改习惯", "定大石头"):
            return {"success": False,
                    "error": "演示模式未连接 TickTick。配置 TICKTICK_TOKEN 与 PROJECT_IDS 后即可真实创建/修改任务。"}
        try:
            if kind == "改约束":
                rule = g("内容", "rule", "约束", "要求", "text") or ""
                if not rule:
                    return {"success": False, "error": "缺少约束内容"}
                if _save_butler_rule(rule) is None:
                    return {"success": False, "error": "约束写入失败"}
                return {"success": True, "msg": "已记住这条约束：「%s」，以后对话我都会遵守。" % rule}
            if kind == "定时提醒":
                content = g("内容", "content", "text", "提醒", "message") or ""
                spec = g("时间", "time", "schedule", "频率", "when") or ""
                if not content:
                    return {"success": False, "error": "缺少提醒内容"}
                if not spec:
                    return {"success": False, "error": "缺少提醒时间（如：每天早上8点 / 每周一 / 每隔2小时）"}
                cron_expr = _parse_nl_schedule(spec)
                if not cron_expr:
                    return {"success": False, "error": "无法识别时间「%s」，支持：每天X点 / 每周X / 每隔X小时" % spec}
                # 开源版：写入 data/reminders.json（可用 REMINDERS_FILE 覆盖），不创建系统级 cron。
                reminders_file = _dd_expanduser_default("DASH_REMINDERS_FILE", "reminders.json")
                rows = _read_json(reminders_file, [])
                if not isinstance(rows, list):
                    rows = []
                rows.append({"content": content, "spec": spec, "cron": cron_expr,
                             "created_at": datetime.now().isoformat(timespec="seconds")})
                if not _write_json(reminders_file, rows[-200:]):
                    return {"success": False, "error": "提醒写入失败"}
                return {"success": True, "msg": "已记录提醒：「%s」（%s，cron=%s）。"
                        "%s" % (content, spec, cron_expr,
                                "已配置推送目标。" if _WECHAT_DELIVER else "未配置推送通道（WECHAT_DELIVER），请自行按 cron 表达式设置系统定时器。")}

            if kind == "建任务":
                title = g("标题", "title", "名称")
                if not title:
                    return {"success": False, "error": "缺少标题"}
                _due_raw = g("截止", "dueDate", "due")
                _due_full, _need_rem = _parse_dt_full(_due_raw)
                _need_rem = _need_rem or a.get("__reminder__")
                _start_raw = g("开始", "开始时间", "startDate", "时段", "起")
                _start_full, _ = _parse_dt_full(_start_raw)
                _task_args = {
                    "title": title,
                    "project": _norm_project(g("项目", "project")) or "📥 收集箱",
                    "quad": g("象限", "quad") or "q2",
                    "role": g("角色", "role"),
                    "priority": _norm_priority(a.get("优先级") or a.get("priority") or 3),
                    "dueDate": _due_full or _norm_due(_due_raw),
                }
                # 时段支持：有「开始」→ 存 startDate（与 dueDate 构成时间段）
                if _start_full:
                    _task_args["startDate"] = _start_full
                    # 只有开始没截止时，若开始早于默认截止则让截止=开始（保证时间段合法）
                    if not _due_full and not _norm_due(_due_raw):
                        _task_args["dueDate"] = _start_full
                # 提醒：有具体时刻→到点；指定提前量→偏移；否则默认到点
                _rem = _reminder_offset(g("提醒", "提前提醒", "reminder"))
                if _rem:
                    _task_args["reminders"] = [_rem]
                elif _need_rem:
                    _task_args["reminders"] = ["TRIGGER:PT0S"]
                # 重复频率 → RRULE
                _rrule = _repeat_to_rrule(g("重复", "频率", "repeat"))
                if _rrule:
                    _task_args["repeatFlag"] = _rrule
                # 清单项（子任务列表）
                _items = a.get("清单") or a.get("清单项") or a.get("items")
                if _items:
                    if isinstance(_items, str):
                        _items = [x.strip() for x in _items.replace("，", ",").split(",") if x.strip()]
                    if isinstance(_items, list) and _items:
                        _task_args["items"] = [{"title": str(x), "status": 0} for x in _items]
                # 标签
                _tags = a.get("标签") or a.get("tags")
                if _tags:
                    if isinstance(_tags, str):
                        _tags = [x.strip() for x in _tags.replace("，", ",").split(",") if x.strip()]
                    if isinstance(_tags, list) and _tags:
                        _task_args["tags"] = _task_args.get("tags", []) + _tags
                return self.create_task(_task_args)
            if kind == "建习惯":
                name = g("名称", "name", "标题")
                if not name:
                    return {"success": False, "error": "缺少名称"}
                dim = _norm_dimension(g("维度", "dimension"), name)
                return self.create_habit({"name": name, "type": a.get("类型") or "Boolean",
                                          "goal": a.get("目标") or 1, "unit": a.get("单位") or "次",
                                          "targetDays": a.get("天数") or a.get("targetDays") or 21,
                                          "dimension": dim})
            if kind == "记关系":
                who = g("对象", "人", "who", "name")
                if not who:
                    return {"success": False, "error": "缺少对象"}
                t = g("类型", "type") or "存款"
                return self.post_relation({"entry": {
                    "who": who, "type": "取款" if "取" in t else "存款",
                    "reason": g("事由", "原因", "reason"),
                    "amount": int(a.get("金额") or a.get("amount") or 10),
                    "role": g("角色", "role"),
                }})
            if kind == "定大石头":
                title = g("标题", "title")
                if not title:
                    return {"success": False, "error": "缺少标题"}
                return self.post_week_plan({"title": title, "why": g("理由", "why", "原因"),
                                            "role": g("角色", "role"),
                                            "project": _norm_project(g("项目", "project")) or "📥 收集箱",
                                            "dueDate": _norm_due(g("截止", "dueDate"))})
            if kind == "写复盘":
                return self.save_review({"role": g("角色", "role"), "text": g("内容", "text")})
            if kind == "删任务":
                kw = (g("标题", "title", "名称", "关键词") or "").strip()
                if not kw:
                    return {"success": False, "error": "缺少标题关键词"}
                tasks = self._dash().get("tasks") or []
                # 搜 title（任务真名）+ content（备注），旧版只搜备注导致按标题永远删不到
                hits = [t for t in tasks if kw.lower() in ((t.get("title") or "") + " " + (t.get("content") or "")).lower()]
                if not hits:
                    return {"success": False, "error": f"没有找到包含「{kw}」的任务"}
                # 多命中给候选，不批量处理；单条也只归档，不做不可逆真删除。
                if len(hits) > 1:
                    _titles = "、".join(((t.get("title") or t.get("content") or "").strip()[:20]) for t in hits[:6])
                    return {"success": False, "error": f"匹配到 {len(hits)} 个任务（{_titles}），请用更精确的标题"}
                t = hits[0]
                r = self.set_task_archived({"id": t.get("id"), "projectId": t.get("projectId") or t.get("pid")}, True)
                if r.get("success"):
                    return {"success": True, "msg": f"已归档任务「{(t.get('title') or t.get('content') or '').strip()[:30]}」（可恢复）"}
                return {"success": False, "error": r.get("error", "归档失败")}
            if kind == "删关系":
                who = g("对象", "人", "who", "name")
                if not who:
                    return {"success": False, "error": "缺少对象名"}
                rels, _ = _purge_expired(_read_json(RELATIONS_FILE, []))
                hits = [x for x in _alive(rels) if who in (x.get("who") or "")]
                if not hits:
                    return {"success": False, "error": f"没有找到对象「{who}」的关系记录"}
                removed = _soft_mark(rels, [x.get("id") for x in hits])
                if not _write_json(RELATIONS_FILE, rels):
                    return {"success": False, "error": "数据写入失败"}
                _sync_relations_md()
                bust_cache()
                return {"success": True, "msg": f"已删除 {removed} 条「{who}」的关系记录（7 天内可恢复），Obsidian 已同步"}
            if kind == "删倾听":
                kw = (g("标题", "title", "内容", "关键词", "对象", "who") or "").strip()
                notes, _ = _purge_expired(_read_json(LISTENING_FILE, []))
                def _hit(x):
                    hay = " ".join(str(x.get(k) or "") for k in ("who", "feeling", "restate", "third"))
                    return kw in hay
                hits = [x for x in _alive(notes) if _hit(x)]
                if not hits:
                    return {"success": False, "error": f"没有找到包含「{kw}」的倾听笔记"}
                removed = _soft_mark(notes, [x.get("id") for x in hits])
                if not _write_json(LISTENING_FILE, notes):
                    return {"success": False, "error": "数据写入失败"}
                _sync_listening_md()
                bust_cache()
                return {"success": True, "msg": f"已删除 {removed} 条倾听笔记（7 天内可恢复），Obsidian 已同步"}
            # v41 删睡眠备注 已由通用 CRUD 引擎接管（CRUD_REGISTRY），无硬编码
            if kind == "改任务":
                kw = (g("标题", "title", "原标题", "关键词") or "").strip()
                new_title = (g("新标题", "改为", "改成", "new_title") or "").strip()
                if not kw or not new_title:
                    return {"success": False, "error": "缺少原标题关键词或新标题"}
                tasks = self._dash().get("tasks") or []
                # 搜 title（任务真名）+ content（备注），旧版只搜备注导致按标题永远改不到
                hits = [t for t in tasks if kw.lower() in ((t.get("title") or "") + " " + (t.get("content") or "")).lower()]
                if not hits:
                    return {"success": False, "error": f"没有找到包含「{kw}」的任务"}
                if len(hits) > 1:
                    names = "、".join(((t.get("title") or t.get("content") or ""))[:15] for t in hits[:5])
                    return {"success": False,
                            "error": f"匹配到 {len(hits)} 条（{names}…），请提供更精确的标题"}
                t = hits[0]
                if new_title.strip() == (t.get("title") or t.get("content") or "").strip():
                    return {"success": True, "msg": f"「{new_title}」名称未变化，无需修改"}
                r = self.update_task({"id": t.get("id"), "projectId": t.get("projectId") or t.get("pid"),
                                      "title": new_title})
                if not r.get("success"):
                    return r
                return {"success": True, "msg": f"已把「{(t.get('content') or '')[:20]}」改名为「{new_title}」"}
            if kind == "改习惯":
                name = (g("名称", "name", "原名称", "关键词") or "").strip()
                new_name = (g("新名称", "改为", "改成", "new_name") or "").strip()
                new_goal = a.get("目标") or a.get("goal")
                if not name or not (new_name or new_goal):
                    return {"success": False, "error": "缺少原名称或新值"}
                habits = (self._habits().get("habits") or [])
                hit = next((h for h in habits if name in (h.get("name") or "")), None)
                if not hit:
                    return {"success": False, "error": f"没有找到名称含「{name}」的习惯"}
                # 新名只是原名的去数字变体（如「每天喝水」vs「每天喝水8杯」）→ 视为没改名，避免误改
                if new_name and _names_similar(new_name, hit.get("name")):
                    new_name = ""
                if new_name and new_name.strip() == name.strip() and new_goal is None:
                    return {"success": True, "msg": f"「{name}」未变化，无需修改"}
                payload = {"id": hit.get("id")}
                if new_name:
                    payload["name"] = new_name
                if new_goal is not None:
                    payload["goal"] = new_goal
                return self.update_habit(payload)
            if kind == "改关系":
                kw = (g("关键词", "事由", "原因", "原事由") or "").strip()
                if not kw:
                    return {"success": False, "error": "缺少原事由关键词"}
                rels = _read_json(RELATIONS_FILE, [])
                hits = [x for x in _alive(rels) if kw in (x.get("reason") or "") or kw in (x.get("who") or "")]
                if not hits:
                    return {"success": False, "error": f"没有找到事由含「{kw}」的关系记录"}
                if len(hits) > 1:
                    return {"success": False, "error": f"匹配到 {len(hits)} 条，请提供更精确的事由"}
                ent = hits[0]
                upd = {"id": ent.get("id")}
                if g("新事由", "改为", "reason"):
                    upd["reason"] = g("新事由", "改为", "reason")
                if g("新对象", "who"):
                    upd["who"] = g("新对象", "who")
                if a.get("新额度") or a.get("amount"):
                    upd["amount"] = a.get("新额度") or a.get("amount")
                if g("新类型", "type"):
                    upd["type"] = "取款" if "取" in g("新类型", "type") else "存款"
                r = self.update_relation(upd)
                if r.get("success"):
                    r["msg"] = "已更新该条关系记录，Obsidian 已同步"
                return r
            if kind == "改倾听":
                kw = (g("关键词", "对象", "who") or "").strip()
                if not kw:
                    return {"success": False, "error": "缺少关键词"}
                notes = _read_json(LISTENING_FILE, [])
                def _hit2(x):
                    hay = " ".join(str(x.get(k) or "") for k in ("who", "feeling", "restate", "third"))
                    return kw in hay
                hits = [x for x in _alive(notes) if _hit2(x)]
                if not hits:
                    return {"success": False, "error": f"没有找到包含「{kw}」的倾听笔记"}
                if len(hits) > 1:
                    return {"success": False, "error": f"匹配到 {len(hits)} 条，请提供更精确的关键词"}
                ent = hits[0]
                note = {}
                for k, ks in (("who", ("新对象", "who")), ("feeling", ("新感受", "feeling")),
                              ("restate", ("新复述", "restate")), ("third", ("新第三选择", "third"))):
                    v = g(*ks)
                    if v:
                        note[k] = v
                if not note:
                    return {"success": False, "error": "缺少要修改的字段（新对象/新感受/新复述/新第三选择）"}
                r = self.update_listening({"id": ent.get("id"), "note": note})
                if r.get("success"):
                    r["msg"] = "已更新该条倾听笔记，Obsidian 已同步"
                return r
            if kind == "记偏好":
                key = g("key", "键", "名称")
                value = g("value", "值", "内容")
                if not key:
                    return {"success": False, "error": "缺少 key"}
                prefs = _save_butler_pref(key, value)
                if prefs is None:
                    return {"success": False, "error": "偏好写入失败"}
                return {"success": True, "prefs": prefs, "msg": f"已记住：{key} = {value}"}
            if kind == "删角色":
                name = (g("名称", "name", "角色", "标题") or "").strip()
                if not name:
                    return {"success": False, "error": "缺少角色名称"}
                # 从已加载角色里精确/模糊匹配
                d = self._dash()
                names = [r.get("name", "") for r in (d.get("roles") or [])]
                hit = next((n for n in names if n == name), None)
                if hit is None:
                    hits = [n for n in names if name in n]
                    if len(hits) == 1:
                        hit = hits[0]
                    elif len(hits) > 1:
                        return {"success": False, "error": f"匹配到多个角色（{'、'.join(hits)}），请用完整名字"}
                if hit is None:
                    return {"success": False, "error": f"没有找到角色「{name}」"}
                r = self.delete_role({"name": hit})
                if r.get("success"):
                    return {"success": True, "msg": f"已把角色「{hit}」移入回收站（可手动找回）"}
                return {"success": False, "error": r.get("error", "删除失败")}
            if kind == "删关注":
                kw = (g("关键词", "内容", "标题", "text", "名字") or "").strip()
                circle = (g("圈", "circle", "类型") or "").strip()
                if not kw:
                    return {"success": False, "error": "缺少条目关键词"}
                cur = self._raw_proactive()
                targets = []
                for c in _alive(cur.get("concerns") or []):
                    if kw in (c.get("text") or ""):
                        if circle and ((circle == "影响圈") != (c.get("circle") == "influence")):
                            continue
                        targets.append(c)
                if not targets:
                    return {"success": False, "error": f"没有找到关注圈/影响圈里包含「{kw}」的条目"}
                removed = _soft_mark(cur["concerns"], [t.get("id") for t in targets])
                json_ok = _write_json(PROACTIVE_FILE, cur)
                mirror_ok = _sync_mirror(json_ok, _sync_proactive_md, "影响圈")
                if not json_ok:
                    return {"success": False, "error": "数据写入失败"}
                bust_cache()
                return _mirror_response({"success": True,
                    "msg": f"已删除 {removed} 条「{kw}」条目（可在 7 天内恢复）"}, mirror_ok)
            if kind == "改关注":
                kw = (g("关键词", "内容", "标题", "text", "原内容") or "").strip()
                new_text = (g("新内容", "改为", "改成", "new_text") or "").strip()
                if not kw or not new_text:
                    return {"success": False, "error": "缺少原内容关键词或新内容"}
                cur = self._raw_proactive()
                hits = [c for c in _alive(cur.get("concerns") or []) if kw in (c.get("text") or "")]
                if not hits:
                    return {"success": False, "error": f"没有找到包含「{kw}」的关注条目"}
                if len(hits) > 1:
                    return {"success": False, "error": f"匹配到 {len(hits)} 条，请用更精确的关键词"}
                c = hits[0]
                orig = (c.get("text") or "")[:20]
                for cc in cur["concerns"]:
                    if cc.get("id") == c.get("id"):
                        cc["text"] = new_text
                json_ok = _write_json(PROACTIVE_FILE, cur)
                mirror_ok = _sync_mirror(json_ok, _sync_proactive_md, "影响圈")
                if not json_ok:
                    return {"success": False, "error": "数据写入失败"}
                bust_cache()
                return _mirror_response({"success": True,
                    "msg": f"已把「{orig}」改为「{new_text}」"}, mirror_ok)
        except Exception as e:
            out = {"success": False}; out.update(_client_error(e)); return out
        # v41 通用 CRUD 兜底：注册表实体（建/删/改/查 + 实体名）自动执行，无需硬编码
        try:
            _verb, _reg = self._crud_find_reg(kind)
            if _reg:
                return self._crud_execute(_verb, _reg, a)
        except Exception as e:
            out = {"success": False}; out.update(_client_error(e)); return out
        return {"success": False, "error": f"不支持的动作：{kind}"}

    @_serialized_file_update
    def delete_relation(self, data):
        """软删：标记 deleted:true（读取层不可见），7 天后惰性物理清理；可 restore 恢复"""
        rid = data.get("id")
        if not rid:
            return {"success": False, "error": "缺少 id"}
        rels, purged = _purge_expired(_read_json(RELATIONS_FILE, []))
        removed = _soft_mark(rels, [rid])
        if not removed:
            return {"success": False, "error": "记录不存在（可能已被删除）"}
        json_ok = _write_json(RELATIONS_FILE, rels)
        mirror_ok = _sync_mirror(json_ok, _sync_relations_md, "情感账户")
        if not json_ok:
            return {"success": False, "error": "数据写入失败"}
        bust_cache()
        _undo_push("删除关系", "delete", "关系", {"ids": [rid]})
        return _mirror_response({"success": True, "count": len(_alive(rels)), "removed": removed,
                "soft_deleted": True, "purged": purged}, mirror_ok)

    @_serialized_file_update
    def restore_relation(self, data):
        rid = data.get("id")
        if not rid:
            return {"success": False, "error": "缺少 id"}
        rels = _read_json(RELATIONS_FILE, [])
        if not _restore(rels, rid):
            return {"success": False, "error": "未找到该已删除记录（可能已超 %d 天被清理）" % SOFT_DELETE_TTL_DAYS}
        json_ok = _write_json(RELATIONS_FILE, rels)
        mirror_ok = _sync_mirror(json_ok, _sync_relations_md, "情感账户")
        if not json_ok:
            return {"success": False, "error": "数据写入失败"}
        bust_cache()
        return _mirror_response({"success": True, "entry": next(x for x in rels if x.get("id") == rid)}, mirror_ok)

    @_serialized_file_update
    def update_relation(self, data):
        rid = data.get("id")
        if not rid:
            return {"success": False, "error": "缺少 id"}
        rels = _read_json(RELATIONS_FILE, [])
        # 只在未软删条目里找（ent 是 rels 内对象的引用，改后写回原始列表保住软删条目）
        ent = next((x for x in _alive(rels) if x.get("id") == rid), None)
        if not ent:
            return {"success": False, "error": "记录不存在（可能已被删除）"}
        fields = ("who", "type", "amount", "reason", "role")
        if not any(k in data for k in fields):
            return {"success": False, "error": "没有要修改的字段"}
        updated = dict(ent)
        for k in ("who", "reason", "role"):
            if k in data:
                updated[k] = str(data.get(k) or "").strip()
        if not updated.get("who") or not updated.get("reason"):
            return {"success": False, "error": "对象和事由不能为空"}
        if "type" in data:
            updated["type"] = str(data.get("type") or "").strip()
        if updated.get("type") not in ("存款", "取款"):
            return {"success": False, "error": "类型必须是存款或取款"}
        try:
            updated["amount"] = abs(int(data["amount"] if "amount" in data else updated.get("amount") or 10))
        except (TypeError, ValueError):
            return {"success": False, "error": "金额必须是数字"}
        if updated["amount"] <= 0:
            return {"success": False, "error": "金额必须大于 0"}
        updated["ts"] = datetime.now().isoformat(timespec="seconds")
        ent.clear()
        ent.update(updated)
        json_ok = _write_json(RELATIONS_FILE, rels)
        mirror_ok = _sync_mirror(json_ok, _sync_relations_md, "情感账户")
        if not json_ok:
            return {"success": False, "error": "数据写入失败"}
        bust_cache()
        return _mirror_response({"success": True, "entry": ent}, mirror_ok)

    @_serialized_file_update
    def delete_listening(self, data):
        """软删：标记 deleted:true（读取层不可见），7 天后惰性物理清理；可 restore 恢复"""
        lid = data.get("id")
        if not lid:
            return {"success": False, "error": "缺少 id"}
        lst, purged = _purge_expired(_read_json(LISTENING_FILE, []))
        removed = _soft_mark(lst, [lid])
        if not removed:
            return {"success": False, "error": "笔记不存在（可能已被删除）"}
        json_ok = _write_json(LISTENING_FILE, lst)
        mirror_ok = _sync_mirror(json_ok, _sync_listening_md, "倾听笔记")
        if not json_ok:
            return {"success": False, "error": "数据写入失败"}
        bust_cache()
        _undo_push("删除倾听笔记", "delete", "倾听笔记", {"ids": [lid]})
        return _mirror_response({"success": True, "count": len(_alive(lst)), "removed": removed,
                "soft_deleted": True, "purged": purged}, mirror_ok)

    @_serialized_file_update
    def restore_listening(self, data):
        lid = data.get("id")
        if not lid:
            return {"success": False, "error": "缺少 id"}
        lst = _read_json(LISTENING_FILE, [])
        if not _restore(lst, lid):
            return {"success": False, "error": "未找到该已删除笔记（可能已超 %d 天被清理）" % SOFT_DELETE_TTL_DAYS}
        json_ok = _write_json(LISTENING_FILE, lst)
        mirror_ok = _sync_mirror(json_ok, _sync_listening_md, "倾听笔记")
        if not json_ok:
            return {"success": False, "error": "数据写入失败"}
        bust_cache()
        return _mirror_response({"success": True, "note": next(x for x in lst if x.get("id") == lid)}, mirror_ok)

    @_serialized_file_update
    def update_listening(self, data):
        lid = data.get("id")
        note = data.get("note") or {}
        if not lid:
            return {"success": False, "error": "缺少 id"}
        if not isinstance(note, dict):
            return {"success": False, "error": "note 必须是对象"}
        fields = ("who", "feeling", "restate", "third")
        if not any(k in note for k in fields):
            return {"success": False, "error": "没有要修改的字段"}
        lst = _read_json(LISTENING_FILE, [])
        # 只在未软删条目里找（ent 是 lst 内对象的引用，改后写回原始列表保住软删条目）
        ent = next((x for x in _alive(lst) if x.get("id") == lid), None)
        if not ent:
            return {"success": False, "error": "笔记不存在（可能已被删除）"}
        updated = dict(ent)
        for k in fields:
            if note.get(k) is not None:
                updated[k] = str(note[k]).strip()
        if not str(updated.get("who") or "").strip():
            return {"success": False, "error": "对话对象不能为空"}
        if not any(str(updated.get(k) or "").strip() for k in ("feeling", "restate", "third")):
            return {"success": False, "error": "至少保留一项倾听内容"}
        updated["ts"] = datetime.now().isoformat(timespec="seconds")
        ent.clear()
        ent.update(updated)
        json_ok = _write_json(LISTENING_FILE, lst)
        mirror_ok = _sync_mirror(json_ok, _sync_listening_md, "倾听笔记")
        if not json_ok:
            return {"success": False, "error": "数据写入失败"}
        bust_cache()
        return _mirror_response({"success": True, "note": ent}, mirror_ok)

    @_serialized_file_update
    def update_habit(self, data):
        """编辑习惯（名称/目标）——TickTick MCP update_habit"""
        hid = data.get("id") or data.get("habitId")
        if not hid:
            return {"success": False, "error": "缺少 id"}
        habit = {}
        if "name" in data:
            name = str(data.get("name") or "").strip()
            if not name:
                return {"success": False, "error": "名称不能为空"}
            habit["name"] = name
        if data.get("goal") is not None:
            try:
                habit["goal"] = float(data["goal"])
            except (TypeError, ValueError):
                return {"success": False, "error": "目标值必须是数字"}
            if habit["goal"] <= 0:
                return {"success": False, "error": "目标值必须大于 0"}
        if not habit:
            return {"success": False, "error": "没有要修改的字段"}
        r = call_ticktick_mcp("update_habit", {"habit_id": hid, "habit": habit})
        if r.get("error"):
            return {"success": False, "error": r["error"]}
        # MCP 写接口偶发返回成功但未落地；回读习惯列表确认名称/目标。
        verified = None
        for delay in (0.25, 0.75):
            time.sleep(delay)
            lr = call_ticktick_mcp("list_habits", {})
            items = (lr.get("result", {}).get("structuredContent", {}).get("result") or []) if isinstance(lr, dict) else []
            candidate = next((h for h in items if h.get("id") == hid), None)
            if candidate is None:
                continue
            if "name" in habit and candidate.get("name") != habit["name"]:
                continue
            if "goal" in habit:
                try:
                    if float(candidate.get("goal")) != float(habit["goal"]):
                        continue
                except (TypeError, ValueError):
                    continue
            verified = candidate
            break
        if verified is None:
            return {"success": False, "error": "TickTick 返回成功但回读未验证到习惯更新，请重试"}
        old_name = str(data.get("oldName") or "").strip()
        new_name = (habit.get("name") or "").strip()
        if old_name and new_name and old_name != new_name:
            dims = _load_habit_dims()
            if old_name in dims and new_name not in dims:
                dims[new_name] = dims.pop(old_name)
                if not _save_habit_dims(dims):
                    return {"success": False, "error": "习惯已更新，但维度归属迁移失败"}
        bust_cache()
        return {"success": True, "habit": verified}

    @_serialized_file_update
    def delete_role(self, data):
        """删除角色：Obsidian 文件移入 .回收站（可手动找回），不直接销毁"""
        name = (data.get("name") or "").strip()
        fpath = _safe_role_path(name)
        if not fpath or not os.path.exists(fpath):
            return {"success": False, "error": "角色文件不存在"}
        trash = os.path.join(OBSIDIAN_ROLES_DIR, ".回收站")
        os.makedirs(trash, exist_ok=True)
        dst = os.path.join(trash, f"{name}-{int(time.time() * 1000)}.md")
        try:
            os.replace(fpath, dst)
        except OSError as e:
            return {"success": False, "error": f"移动失败：{e}"}
        if os.path.exists(fpath) or not os.path.isfile(dst):
            return {"success": False, "error": "角色移动后回读未验证"}
        bust_cache()
        return {"success": True, "moved_to": dst}

    @_serialized_file_update
    def delete_proactive(self, data):
        """软删（concern/log 可恢复，checkin 为日期字典保持物理删——单日一句话，无可恢复价值）"""
        pid = data.get("id")
        kind = data.get("kind")  # concern | log | checkin
        if not pid and kind != "checkin":
            return {"success": False, "error": "缺少 id"}
        cur = self._raw_proactive()
        cur["concerns"], _ = _purge_expired(cur["concerns"])
        cur["logs"], _ = _purge_expired(cur["logs"])
        if kind == "log":
            removed = _soft_mark(cur["logs"], [pid])
            if not removed:
                return {"success": False, "error": "记录不存在"}
            json_ok = _write_json(PROACTIVE_FILE, cur)
            mirror_ok = _sync_mirror(json_ok, _sync_proactive_md, "影响圈")
            if not json_ok:
                return {"success": False, "error": "数据写入失败"}
            bust_cache()
            return _mirror_response({"success": True, "soft_deleted": True}, mirror_ok)
        if kind == "concern":
            removed = _soft_mark(cur["concerns"], [pid])
            if not removed:
                return {"success": False, "error": "条目不存在"}
            json_ok = _write_json(PROACTIVE_FILE, cur)
            mirror_ok = _sync_mirror(json_ok, _sync_proactive_md, "影响圈")
            if not json_ok:
                return {"success": False, "error": "数据写入失败"}
            bust_cache()
            return _mirror_response({"success": True, "soft_deleted": True}, mirror_ok)
        if kind == "checkin":
            d = data.get("date")
            if not d or d not in cur["checkins"]:
                return {"success": False, "error": "当日无记录"}
            cur["checkins"].pop(d)
            json_ok = _write_json(PROACTIVE_FILE, cur)
            mirror_ok = _sync_mirror(json_ok, _sync_proactive_md, "影响圈")
            if not json_ok:
                return {"success": False, "error": "数据写入失败"}
            bust_cache()
            return _mirror_response({"success": True}, mirror_ok)
        return {"success": False, "error": "未知类型（concern/log/checkin）"}

    @_serialized_file_update
    def restore_proactive(self, data):
        pid = data.get("id")
        kind = data.get("kind")  # concern | log
        if not pid or kind not in ("concern", "log"):
            return {"success": False, "error": "缺少 id 或 kind(concern/log)"}
        cur = self._raw_proactive()
        if not _restore(cur[kind + "s"], pid):
            return {"success": False, "error": "未找到该已删除条目（可能已超 %d 天被清理）" % SOFT_DELETE_TTL_DAYS}
        json_ok = _write_json(PROACTIVE_FILE, cur)
        mirror_ok = _sync_mirror(json_ok, _sync_proactive_md, "影响圈")
        if not json_ok:
            return {"success": False, "error": "数据写入失败"}
        bust_cache()
        return _mirror_response({"success": True}, mirror_ok)

    @_serialized_file_update
    def update_proactive(self, data):
        pid = data.get("id")
        if not pid:
            return {"success": False, "error": "缺少 id"}
        cur = self._raw_proactive()
        log = next((l for l in _alive(cur["logs"]) if l.get("id") == pid), None)
        if not log:
            return {"success": False, "error": "记录不存在"}
        if not any(data.get(k) is not None and str(data[k]).strip() for k in ("from", "to", "date")):
            return {"success": False, "error": "没有要修改的字段"}
        for k in ("from", "to", "date"):
            if data.get(k) is not None and str(data[k]).strip():
                log[k] = str(data[k]).strip()
        log["ts"] = datetime.now().isoformat(timespec="seconds")
        json_ok = _write_json(PROACTIVE_FILE, cur)
        mirror_ok = _sync_mirror(json_ok, _sync_proactive_md, "影响圈")
        if not json_ok:
            return {"success": False, "error": "数据写入失败"}
        bust_cache()
        return _mirror_response({"success": True, "log": log}, mirror_ok)

    @_serialized_file_update
    def set_habit_dimension(self, data):
        """为已存在的习惯设置四维归类 / 关联角色（均只存本地 dims，不进 TickTick）。
        字段传 None/缺省=不改；传空串=清除该字段；两字段皆空则删条目。"""
        name = str(data.get("name") or "").strip()
        if not name:
            return {"success": False, "error": "缺少名称"}
        dim = data.get("dimension")
        role = data.get("role")
        if dim is not None:
            dim = str(dim).strip()
        if role is not None:
            role = str(role).strip()
        if dim and dim not in HABIT_DIMENSIONS:
            return {"success": False, "error": f"未知维度：{dim}"}
        dims = _load_habit_dims()
        cur = dict(dims.get(name) or {})
        if dim is not None:
            cur["dimension"] = dim
        if role is not None:
            cur["role"] = role
        cur = {k: v for k, v in cur.items() if v}
        if cur:
            dims[name] = cur
        else:
            dims.pop(name, None)
        if not _save_habit_dims(dims):
            return {"success": False, "error": "习惯维度写入失败"}
        bust_cache()
        return {"success": True, "dims": dims}

    # ─── 习惯一：关注圈 / 影响圈（积极主动）───
    def _raw_proactive(self):
        """原始数据（含软删条目）——写路径专用，写回时不能丢软删条目"""
        d = _read_json(PROACTIVE_FILE, {})
        if not isinstance(d, dict):
            d = {}
        d.setdefault("concerns", [])
        d.setdefault("logs", [])
        d.setdefault("checkins", {})
        return d

    def get_proactive(self):
        """读取视图：concerns/logs 过滤软删条目（checkins 为日期字典，无软删）"""
        if _demo_enabled():
            d = _demo().get("proactive", {})
            return {"demo": True, "concerns": d.get("concerns", []), "logs": d.get("logs", []), "checkins": d.get("checkins", {})}
        if _mirror_stale(PROACTIVE_FILE, PROACTIVE_MD):
            _sync_proactive_md()
        d = self._raw_proactive()
        d["concerns"] = _alive(d["concerns"])
        d["logs"] = _alive(d["logs"])
        return d

    @_serialized_file_update
    def post_proactive(self, data):
        cur = self._raw_proactive()
        if isinstance(data.get("concerns"), list):
            if not all(isinstance(x, dict) for x in data["concerns"]):
                return {"success": False, "error": "concerns 必须是对象数组"}
            # 前端读取接口只返回活跃条目；不能用它整体覆盖原数组，否则会把
            # 回收站里的 deleted 条目物理抹掉，导致 7 天恢复窗口失效。
            incoming = data["concerns"]
            incoming_ids = {x.get("id") for x in incoming if isinstance(x, dict) and x.get("id")}
            current = cur.get("concerns", [])
            preserved_deleted = [x for x in current
                                 if isinstance(x, dict) and x.get("deleted")
                                 and (not x.get("id") or x.get("id") not in incoming_ids)]
            # 所有删除都走专用 delete 路由；因此这里可以安全保留并发设备刚写入、
            # 但当前客户端尚未看到的活跃条目，避免整表回写造成静默丢数据。
            active_ids = {x.get("id") for x in incoming if isinstance(x, dict) and x.get("id")}
            preserved_active = [x for x in current
                                if isinstance(x, dict) and not x.get("deleted")
                                and x.get("id") and x.get("id") not in active_ids]
            cur["concerns"] = preserved_deleted + incoming + preserved_active
        if isinstance(data.get("checkins"), dict):
            cur["checkins"].update(data["checkins"])
        log = data.get("log")
        if log:
            if not isinstance(log, dict):
                return {"success": False, "error": "log 必须是对象"}
            log = dict(log)
            log["week"] = _week_key()
            log["ts"] = datetime.now().isoformat(timespec="seconds")
            log.setdefault("id", "pl" + str(int(time.time() * 1000)))
            cur["logs"].append(log)
        alive_concerns = _alive(cur["concerns"])
        inf = sum(1 for c in alive_concerns if c.get("circle") == "influence")
        json_ok = _write_json(PROACTIVE_FILE, cur)
        # 任何影响圈变更都同步 Obsidian 镜像；此前 concerns-only 更新会漏同步。
        mirror_ok = True
        if isinstance(data.get("concerns"), list) or log or data.get("checkins"):
            mirror_ok = _sync_mirror(json_ok, _sync_proactive_md, "影响圈")
        if not json_ok:
            return {"success": False, "error": "数据写入失败"}
        bust_cache()
        return _mirror_response({"success": True, "concerns": len(alive_concerns), "influence": inf,
                "logs": len(cur["logs"])}, mirror_ok)

def _scheduler():
    """后台守护线程：定时生成周复盘草稿，并在启动后补齐缺失周。"""
    # 启动补偿：避免服务恰好在周日 20:05 后重启而静默漏掉整周草稿。
    try:
        h = object.__new__(DashboardHandler)
        h._get_weekly_draft()
    except Exception as exc:
        print(f"[scheduler] startup reconciliation failed: {exc}", flush=True)
    while True:
        try:
            now = datetime.now()
            if now.weekday() == 6 and now.hour == 20 and now.minute < 5:
                h = object.__new__(DashboardHandler)
                h._generate_weekly_draft()
                time.sleep(3600)  # 生成过一次后本小时内不再重复
            else:
                time.sleep(60)
        except Exception:
            time.sleep(300)

# ─── Life OS 扩展：通用辅助 ───
RELATIONS_FILE = _dd_expanduser_default("DASH_RELATIONS_FILE", "情感账户", "relations.json")
LISTENING_FILE = _dd_expanduser_default("DASH_LISTENING_FILE", "倾听笔记", "listening.json")
HEALTH_NOTES_FILE = _dd_expanduser_default("DASH_HEALTH_NOTES_FILE", "健康", "health-notes.json")
BRIEFING_CACHE_FILE = _dd_expanduser_default("DASH_BRIEFING_CACHE_FILE", "health_briefing.json")
PROACTIVE_FILE = _dd_expanduser_default("DASH_PROACTIVE_FILE", "影响圈", "proactive.json")

# ═══ 操作历史 / 一键返回 ═══
# 精确式撤销：每笔写动作记录反向操作，undo 时按 op 恢复（软删 restore / 写回旧值 / 软删新增）。
# 与 merge 联动：撤销改的是本地 json，随 merge 传播到另一端，保证双端一致。
UNDO_HISTORY_FILE = _dd_expanduser_default("DASH_UNDO_HISTORY_FILE", "undo_history.json")
UNDO_MAX = 50

def _undo_push(desc, op, entity, payload, client_turn_id=None):
    """压一条撤销记录；返回 True。op: create/delete/update；entity=注册表名。"""
    try:
        lst = _read_json(UNDO_HISTORY_FILE, [])
        row = {"id": "u" + str(int(time.time() * 1000)),
               "ts": datetime.now().isoformat(timespec="seconds"),
               "desc": desc, "op": op, "entity": entity, "payload": payload}
        if client_turn_id:
            row["clientTurnId"] = str(client_turn_id)
        lst.append(row)
        _write_json(UNDO_HISTORY_FILE, lst[-UNDO_MAX:])
        return True
    except Exception:
        return False


# ═══ 通用 CRUD 注册表 ═══
# 本地 JSON 实体统一声明：管家 do_action（建/删/改/查 + 实体名）自动获得增删改查能力，无需逐个写处理器。
# 新增实体 = 在此加一行，BUTLER_TOOLS 工具描述 / 别名 / 处理器全自动跟随。
# 字段：name=中文集合名；file=数据文件；id_prefix=id 前缀；fields={内部键:中文名}；
#       search=删除/修改时管家关键词匹配的字段；mirror=Obsidian MD 镜像函数名(None=无，延迟 globals 解析)；
#       soft=True 软删(deleted 标记)；sub=dict 型数据的子集合路径(None=顶层数组)。
CRUD_REGISTRY = [
    {"name": "睡眠备注", "file": HEALTH_NOTES_FILE, "id_prefix": "h",
     "fields": {"note": "内容", "date": "日期"}, "search": ["note"],
     "mirror": None, "soft": True, "sub": None},
    {"name": "关系", "file": RELATIONS_FILE, "id_prefix": "r",
     "fields": {"who": "对象", "type": "类型", "amount": "金额", "reason": "事由", "role": "角色"},
     "search": ["who", "reason"], "mirror": "sync_relations_md", "soft": True, "sub": None},
    {"name": "倾听笔记", "file": LISTENING_FILE, "id_prefix": "l",
     "fields": {"who": "对象", "feeling": "感受", "restate": "复述", "third": "第三方"},
     "search": ["who", "feeling"], "mirror": "sync_listening_md", "soft": True, "sub": None},
    {"name": "关注", "file": PROACTIVE_FILE, "id_prefix": "p",
     "fields": {"text": "内容", "reason": "理由"}, "search": ["text"],
     "mirror": None, "soft": True, "sub": "concerns"},
]

# 动作动词 → 通用引擎操作（kind 前缀解析：建睡眠备注 = "建" + "睡眠备注"）
CRUD_VERBS = {"建": "create", "新增": "create", "记": "create", "写": "create", "加": "create",
              "删": "delete", "删除": "delete", "移除": "delete",
              "改": "update", "编辑": "update", "修改": "update",
              "查": "list", "看": "list", "列出": "list"}
HABIT_DIMS_FILE = _dd_expanduser_default("DASH_HABIT_DIMS_FILE", "habit_dims.json")
BUTLER_HISTORY_FILE = _dd_expanduser_default("DASH_BUTLER_HISTORY_FILE", "butler_history.json")
BUTLER_MEMORY_FILE = _dd_expanduser_default("DASH_BUTLER_MEMORY_FILE", "butler_memory.json")
BUTLER_PREFS_FILE = _dd_expanduser_default("DASH_BUTLER_PREFS_FILE", "butler_prefs.json")
BUTLER_MAX_HISTORY = 40  # 对话历史最大保留条数（超出自动摘要裁剪）

# ── 用户画像（长期记忆：让管家"认识个人"，而非只看当前快照）──
USER_PROFILE_FILE = _dd_expanduser_default("DASH_USER_PROFILE_FILE", "user_profile.json")
USER_PROFILE_SEED = {
    "identity": "",
    "strategy": "",
    "core_pain": "",
    "health": "",
    "communication": "",
    "interests": "",
    "style": "",
    "environment": "",
    "learned_notes": []
}

def _load_user_profile():
    """读取用户画像；无文件则用预置种子初始化并落盘；损坏则备份后重建（自愈）"""
    real_path = os.path.realpath(USER_PROFILE_FILE)
    try:
        d = _read_json(USER_PROFILE_FILE, None)
        if isinstance(d, dict) and d:
            return d
        # 文件存在但解析失败/非对象 → 备份损坏文件，重建种子
        if os.path.exists(USER_PROFILE_FILE):
            try:
                backup = USER_PROFILE_FILE + ".corrupt-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                os.replace(USER_PROFILE_FILE, backup)
                print(f"[profile] 画像文件损坏，已备份为 {backup} 并重建", flush=True)
            except Exception as e:
                print(f"[profile] 备份损坏画像失败: {e}", flush=True)
    except Exception:
        pass
    # _read_json 会把损坏文件加入保护名单；隔离后解除保护，允许种子重新落盘。
    _corrupt_json_files.discard(real_path)
    try:
        if not _write_json(USER_PROFILE_FILE, dict(USER_PROFILE_SEED)):
            print("[profile] 画像种子写入失败，将在下次读取时重试", flush=True)
    except Exception as e:
        print(f"[profile] 画像重建失败: {e}", flush=True)
    return dict(USER_PROFILE_SEED)

def _user_profile_text():
    """把用户画像格式化为文本注入 system prompt（让管家认识当前用户）。
    learned_notes 按 confidence 降序取 top15（更可信的排前面）；旧格式无 confidence 按 0.6 处理。"""
    try:
        p = _load_user_profile()
        if not p:
            return ""
        lines = []
        for k in ("identity", "strategy", "core_pain", "health", "communication", "interests", "style", "environment"):
            v = p.get(k)
            if v:
                lines.append("- %s：%s" % (k, v))
        learned = p.get("learned_notes") or []
        if learned:
            def _conf(x):
                try:
                    return float(x.get("confidence") or 0.6) if isinstance(x, dict) else 0.6
                except (TypeError, ValueError):
                    return 0.6
            # 记忆瘦身：当日瞬时状态只保留近期记录。
            _cutoff = (date.today() - timedelta(days=7)).isoformat()
            learned = [x for x in learned
                       if not (isinstance(x, dict) and x.get("type") in ("state", "progress")
                               and (x.get("date") or "")[:10] < _cutoff)]
            top = sorted(learned, key=_conf, reverse=True)[:15]
            lines.append("- 从过往对话沉淀的关于他：%s" % "；".join(str(x.get("fact") or x) for x in top))
        return "【关于" + USER_NAME + "这个人】\n" + "\n".join(lines) + "\n" if lines else ""
    except Exception:
        return ""

_profile_lock = threading.Lock()
_last_profile_ts = 0.0
_PROFILE_THROTTLE_S = 1800  # 画像抽取节流：最多每 30 分钟一次

def _update_user_profile(user_msgs, assistant_reply):
    """对话后让 LLM 从对话里沉淀"关于当前用户的新事实"到画像的 learned_notes。
    每条带 type（preference/identity/state/decision/progress）和 confidence；
    同一 fact 再次出现 → confidence +0.2（封顶 0.95），多轮印证则更可信。
    旧格式 {fact,date} 兼容读取；只抽用户明确表达的内容，不臆测；失败静默，绝不影响管家回复。
    入口节流：距上次抽取不足 30 分钟直接跳过。"""
    global _last_profile_ts
    with _profile_lock:
        _now2 = time.time()
        if _now2 - _last_profile_ts < _PROFILE_THROTTLE_S:
            return
        _last_profile_ts = _now2
    try:
        if not user_msgs:
            return
        recent = " ".join((m.get("content") or "")[:300] for m in user_msgs[-4:])
        recent = recent.strip()
        if not recent:
            return
        prompt = ("你是用户画像整理助手。从下面的对话里，提取关于这个用户值得长期记住的新事实"
                  "（新偏好/新状态/新决定/新顾虑/新进展），每条一句，只提取用户明确表达的内容，不要臆测，"
                  "不要重复常识。每条标注 type，只能是这五个之一："
                  "preference(偏好)/identity(身份特征)/state(当前状态或顾虑)/decision(决定)/progress(进展)。"
                  '输出严格 JSON 对象，外层用 {"items":[{"fact":"...","type":"..."}]} 包裹。\n\n对话：\n'
                  + recent)
        p = _load_user_profile()
        learned = p.setdefault("learned_notes", [])
        existing_facts = [str(x.get("fact")) for x in learned if isinstance(x, dict)][:20]
        prompt2 = ""
        if existing_facts:
            prompt2 = ("\n\n画像里已有这些事实：\n" + "\n".join("- " + f for f in existing_facts)
                       + "\n若新事实与其中某条语义相同（只是再次确认），请把那条的 fact 原样一字不改地输出，"
                         "系统会提升它的置信度；语义不同才输出新句子。")
        r = _call_llm([{"role": "system", "content": '你只输出 JSON 对象 {"items":[{"fact":"...","type":"..."}]}'},
                       {"role": "user", "content": prompt + prompt2}],
                      json_mode=True, temperature=0.3, max_tokens=400, feature="profile")
        if "error" in r:
            return
        import json as _json
        obj = _json.loads(r.get("content") or "{}")
        items = obj.get("items") or []
        if not items:
            return
        VALID_TYPES = ("preference", "identity", "state", "decision", "progress")
        # 旧格式条目升级为带 type/confidence 的结构（向后兼容，读取时兜底）
        for x in learned:
            if isinstance(x, dict) and "confidence" not in x:
                x.setdefault("type", "preference")
                x["confidence"] = 0.6
        by_fact = {str(x.get("fact")): x for x in learned if isinstance(x, dict)}
        changed = False
        today = date.today().isoformat()
        for it in items:
            if not isinstance(it, dict):
                continue
            fact = (it.get("fact") or "").strip()
            if not fact or len(fact) > 120:
                continue
            typ = it.get("type") if it.get("type") in VALID_TYPES else "preference"
            if fact in by_fact:
                old = by_fact[fact]
                old["confidence"] = round(min(0.95, float(old.get("confidence") or 0.6) + 0.2), 2)
                old["date"] = today
                old.setdefault("type", typ)
                changed = True
            else:
                note = {"fact": fact, "date": today, "type": typ, "confidence": 0.6}
                learned.append(note)
                by_fact[fact] = note
                changed = True
        if changed:
            # R1: 曾用 learned[-40:] 按插入顺序砍尾——多次印证、confidence 已到 0.95 的老事实
            # 会被后来一堆新的低置信度(0.6)事实挤出去，跟"重要事实不丢"的目标相反。
            # 改成按 confidence 降序保留（_merge_user_profile 里已经这么做，这里补齐一致）。
            if len(learned) > 40:
                learned = sorted(learned, key=_note_conf, reverse=True)[:40]
            p["learned_notes"] = learned
            if not _write_json(USER_PROFILE_FILE, p):
                print("[profile] 画像沉淀写入失败，将在下次对话后重试", flush=True)
                return
            # 沉淀后触发 Mac↔VPS 画像同步（后台线程，失败静默）
            try:
                threading.Thread(target=_sync_user_profile_with_vps, daemon=True).start()
            except Exception:
                pass
    except Exception:
        pass

def _note_conf(x):
    """画像 note 的置信度，缺省 0.6"""
    try:
        return float(x.get("confidence") or 0.6) if isinstance(x, dict) else 0.6
    except (TypeError, ValueError):
        return 0.6

def _merge_user_profile(remote):
    """把远端画像并入本地画像：learned_notes 按 fact 去重取并集（保留置信度更高者），封顶 40。返回合并后的本地画像。"""
    if not isinstance(remote, dict):
        return _load_user_profile()
    local = _load_user_profile()
    lnotes = local.get("learned_notes") or []
    rnotes = remote.get("learned_notes") or []
    merged = {str(x.get("fact")): x for x in lnotes if isinstance(x, dict)}
    changed = False
    for rn in rnotes:
        if not isinstance(rn, dict):
            continue
        f = str(rn.get("fact"))
        if not f:
            continue
        if f not in merged:
            merged[f] = rn
            changed = True
        elif _note_conf(rn) > _note_conf(merged[f]):
            merged[f] = rn
            changed = True
    nlist = list(merged.values())
    if len(nlist) > 40:
        nlist = sorted(nlist, key=_note_conf, reverse=True)[:40]
    if changed or len(nlist) != len(lnotes):
        local["learned_notes"] = nlist
        try:
            _write_json(USER_PROFILE_FILE, local)
        except Exception:
            pass
    return local

def _sync_user_profile_with_vps():
    """本地端与配置的远端服务双向同步用户画像：拉取 → 并入本地 → 推回并集。
    VPS 端(ON_VPS)跳过——VPS 是主存储。失败静默，绝不影响管家。"""
    if os.environ.get("DASH_PROFILE_SYNC") != "1" or _demo_enabled():
        return
    if ON_VPS:
        return
    if not (VPS_DASH_URL and VPS_AUTH):
        return
    try:
        # P2-D9: 统一走 _vps_get（鉴权/超时/错误集中处理）
        _st, body = _vps_get("/api/butler/profile", timeout=6)
        if _st is None:
            return
        remote = json.loads(body)
        # 并入本地
        local = _merge_user_profile(remote)
        # 把并集推回 VPS
        _vps_get("/api/butler/profile", timeout=6, data=local, method="POST")
    except Exception:
        pass
# Obsidian markdown 镜像（让数据在 Obsidian 里直接可读，不只躺在 json 里）
RELATIONS_MD = _dd_expanduser_default("DASH_RELATIONS_MD", "情感账户", "情感账户.md")
LISTENING_MD = _dd_expanduser_default("DASH_LISTENING_MD", "倾听笔记", "倾听笔记.md")
PROACTIVE_MD = _dd_expanduser_default("DASH_PROACTIVE_MD", "影响圈", "影响圈.md")

HABIT_DIMENSIONS = ["身体", "精神", "智力", "社会情感"]

def _week_key(d=None):
    wk = (d or date.today()).isocalendar()
    return f"{wk[0]}-W{wk[1]:02d}"

def _md_append(path, title, line):
    """向 Obsidian markdown 追加一行（按周分小节，文件不存在则建）"""
    with _file_lock:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            head = f"## {_week_key()}"
            body = ""
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    body = f.read()
            else:
                body = f"# {title}\n\n> 由{USER_NAME}驾驶舱自动写入，可直接在 Obsidian 中编辑\n"
            if head not in body:
                body = body.rstrip() + f"\n\n{head}\n"
            body = body.rstrip() + "\n" + line.rstrip() + "\n"
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            _restrict_private_mode(path)
            return True
        except Exception as e:
            print(f"[md_append] {path} 追加失败: {e}", flush=True)
            return False

def _mirror_stale(json_path, md_path):
    """json 数据源比 md 镜像新（merge/rsync 刚更新过数据）→ 镜像需要重渲染。
    读取路径惰性自愈：合并脚本只写 json 不写 md，下次 GET 时补渲染，双端 Obsidian 视图不再滞后。"""
    try:
        jm = os.path.getmtime(json_path)
    except OSError:
        return False
    try:
        return jm > os.path.getmtime(md_path)
    except OSError:
        return True

def _md_sync(path, title, items, line_of, src_file=None):
    """全量重渲染 Obsidian 镜像：JSON 是唯一事实源，按周倒序分节。
    新增/编辑/删除后调用，镜像与数据永远一致——彻底避免「删了还有残留」。
    内容未变时不写盘，仅把 mtime 对齐到 json——避免每次重渲染都白白推高 mtime，
    避免重复写入未变化的镜像。"""
    with _file_lock:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            weeks = {}
            order = []
            for it in items:
                wk = it.get("week") or (it.get("ts") or "")[:7]
                if wk not in weeks:
                    weeks[wk] = []
                    order.append(wk)
                weeks[wk].append(it)
            body = (f"# {title}\n\n"
                    f"> 本文件由{USER_NAME}驾驶舱自动同步（数据源：驾驶舱 JSON），"
                    "编辑或删除请在驾驶舱操作；此处手动修改会在下次同步时被覆盖。\n")
            for wk in sorted(order, reverse=True):
                lines = [line_of(it) for it in sorted(weeks[wk], key=lambda x: x.get("ts") or "")]
                body += f"\n## {wk}\n" + "\n".join(l for l in lines if l) + "\n"
            try:
                with open(path, encoding="utf-8") as f:
                    if f.read() == body:
                        # 内容一致：不写盘，mtime 对齐数据源，让 _mirror_stale 收敛
                        if src_file:
                            try:
                                sm = os.path.getmtime(src_file)
                                os.utime(path, (sm, sm))
                            except OSError:
                                pass
                        _restrict_private_mode(path)
                        return True
            except OSError:
                pass
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            _restrict_private_mode(path)
            return True
        except Exception as e:
            print(f"[md_sync] {path} 同步失败: {e}", flush=True)
            return False

def _sync_relations_md():
    rels = _alive(_read_json(RELATIONS_FILE, []))
    def line_of(e):
        sign = "＋" if e.get("type") != "取款" else "－"
        role = f"（{e.get('role')}）" if e.get("role") else ""
        return (f"- **{(e.get('ts') or '')[:10]}** {e.get('who', '')} {role}{sign}{e.get('amount', 0)}"
                f"　{e.get('reason', '')}")
    return _md_sync(RELATIONS_MD, "情感账户 · 双赢", rels, line_of, src_file=RELATIONS_FILE)

def _sync_listening_md():
    notes = _alive(_read_json(LISTENING_FILE, []))
    def line_of(n):
        return (f"- **{(n.get('ts') or '')[:10]} 与 {n.get('who', '')}**\n"
                f"    - 他的感受：{n.get('feeling', '')}\n"
                f"    - 我的复述：{n.get('restate', '')}\n"
                f"    - 第三选择：{n.get('third', '')}")
    return _md_sync(LISTENING_MD, "倾听笔记 · 知彼解己", notes, line_of, src_file=LISTENING_FILE)

def _sync_proactive_md():
    """同步影响圈.md：包含「语言转换记录」和「今天的选择」两个小节，按周倒序。
    JSON 是唯一事实源，全量重渲染，避免残留；软删条目不出现在镜像。"""
    data = _read_json(PROACTIVE_FILE, {}) or {}
    logs = _alive(data.get("logs") or [])
    checkins = data.get("checkins") or {}
    concerns = _alive(data.get("concerns") or [])

    def wk_of_date(dstr):
        try:
            return _week_key(datetime.strptime(dstr, "%Y-%m-%d").date())
        except Exception:
            return (dstr or "")[:7] or "未分周"

    # 语言转换记录（logs）
    log_weeks = {}
    for l in logs:
        wk = l.get("week") or (l.get("ts") or "")[:7]
        log_weeks.setdefault(wk, []).append(l)

    # 今天的选择（checkins: {日期: 文本}）
    ck_weeks = {}
    for dstr in checkins:
        ck_weeks.setdefault(wk_of_date(dstr), []).append({"date": dstr, "text": checkins[dstr]})

    body = "# 影响圈 · 积极主动\n\n"
    body += ("> 本文件由" + USER_NAME + "驾驶舱自动同步（数据源：驾驶舱 JSON），"
             "编辑或删除请在驾驶舱操作；此处手动修改会在下次同步时被覆盖。\n")

    # 关注圈 / 影响圈小节（concerns，用 - ** 开头便于 verify_mirror 计数）
    body += "\n## 关注圈 / 影响圈\n"
    if concerns:
        body += "\n".join(
            "- **%s** %s%s" % (
                "影响圈" if c.get("circle") == "influence" else "关注圈",
                c.get("text", ""),
                "（已转任务）" if c.get("converted") else "")
            for c in concerns) + "\n"
    else:
        body += "\n（暂无关注圈/影响圈条目）\n"

    # 语言转换记录小节
    body += "\n## 语言转换记录\n"
    if log_weeks:
        for wk in sorted(log_weeks.keys(), reverse=True):
            lines = [f"- **{l.get('date', '')}** 消极：「{l.get('from', '')}」→ 积极：「{l.get('to', '')}」"
                     for l in sorted(log_weeks[wk], key=lambda x: x.get("ts") or "")]
            body += f"\n### {wk}\n" + "\n".join(l for l in lines if l) + "\n"
    else:
        body += "\n（暂无语言转换记录）\n"

    # 今天的选择小节
    body += "\n## 📌 今天的选择\n"
    if ck_weeks:
        for wk in sorted(ck_weeks.keys(), reverse=True):
            lines = [f"- **{c['date']}** {c['text']}"
                     for c in sorted(ck_weeks[wk], key=lambda x: x["date"], reverse=True)]
            body += f"\n### {wk}\n" + "\n".join(l for l in lines if l) + "\n"
    else:
        body += "\n（暂无今天的选择记录）\n"

    with _file_lock:
        try:
            os.makedirs(os.path.dirname(PROACTIVE_MD), exist_ok=True)
            # 内容一致：不写盘，mtime 对齐数据源（与 _md_sync 同一策略，防 rsync 空转）
            try:
                with open(PROACTIVE_MD, encoding="utf-8") as f:
                    if f.read() == body:
                        try:
                            sm = os.path.getmtime(PROACTIVE_FILE)
                            os.utime(PROACTIVE_MD, (sm, sm))
                        except OSError:
                            pass
                        _restrict_private_mode(PROACTIVE_MD)
                        return True
            except OSError:
                pass
            with open(PROACTIVE_MD, "w", encoding="utf-8") as f:
                f.write(body)
            _restrict_private_mode(PROACTIVE_MD)
            return True
        except Exception as e:
            print(f"[md_sync] {PROACTIVE_MD} 同步失败: {e}", flush=True)
            return False

# ─── 镜像双写一致性守卫（JSON=事实源，MD=镜像；MD 失败绝不影响 JSON）───
def _sync_mirror(json_ok, md_fn, name):
    """统一双写流程：先确认 JSON 已成功写入（_write_json 返回 True）再写 MD。
    - JSON 写失败 → 跳过 MD 同步并打日志（防止镜像领先于主数据）
    - MD 写失败 → 打日志返回 False，JSON 主数据完好，下次写入全量重渲染自愈
    返回 MD 镜像是否同步成功。"""
    if not json_ok:
        print(f"[mirror] {name}: JSON 主数据写入失败，本次跳过 MD 镜像同步", flush=True)
        return False
    try:
        ok = md_fn()  # 内部失败已自行打 [md_sync] 日志
    except Exception as e:
        print(f"[mirror] {name}: MD 镜像同步异常: {e}", flush=True)
        return False
    if not ok:
        print(f"[mirror] {name}: MD 镜像未同步成功（JSON 主数据完好，下次写入会全量重渲染）", flush=True)
    return ok

def _mirror_response(payload, mirror_ok):
    """给写接口附加镜像状态；主数据成功不应被镜像故障伪装成完整同步。"""
    if not mirror_ok:
        payload["warning"] = "主数据已保存，但 Obsidian 镜像同步失败"
    return payload

def verify_mirror():
    """校验镜像一致性：比对 JSON 可见条目数与 MD 条目行数，返回漂移清单。
    只报告不修复、不常驻——供启动日志或手动排查调用。"""
    issues = []
    for name, jf, md, subkeys in (
            ("情感账户", RELATIONS_FILE, RELATIONS_MD, None),
            ("倾听笔记", LISTENING_FILE, LISTENING_MD, None),
            ("影响圈", PROACTIVE_FILE, PROACTIVE_MD, ("logs", "concerns"))):
        try:
            if subkeys is None:
                n_json = len(_alive(_read_json(jf, [])))
            else:
                d = _read_json(jf, {}) or {}
                n_json = (sum(len(_alive(d.get(k) or [])) for k in subkeys)
                          + len(d.get("checkins") or {}))
            with open(md, encoding="utf-8") as f:
                n_md = sum(1 for l in f if l.startswith("- **"))
            if n_json != n_md:
                issues.append(f"{name}: JSON {n_json} 条 vs 镜像 MD {n_md} 条")
        except FileNotFoundError:
            issues.append(f"{name}: 镜像 MD 不存在（{md}）")
        except Exception as e:
            issues.append(f"{name}: 校验异常 {e}")
    return issues

def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        _corrupt_json_files.discard(os.path.realpath(path))
        return data
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        real = os.path.realpath(path)
        _corrupt_json_files.add(real)
        print(f"[read_json] {path} JSON 损坏，已锁定为只读并禁止空值覆盖: {e}", flush=True)
        return default
    except Exception as e:
        print(f"[read_json] {path} 读取失败: {e}", flush=True)
        return default

def _critical_json_registry():
    """可人工恢复的关键 JSON 白名单；明确排除凭据、对话全文和原始健康数据。"""
    rows = {
        "ai-oracle": (getattr(globals().get("DashboardHandler"), "ORACLE_FILE", ""), "每日 AI 建议"),
        "weekly-draft": (getattr(globals().get("DashboardHandler"), "WEEKLY_DRAFT_FILE", ""), "周复盘草稿"),
        "local-state": (getattr(globals().get("DashboardHandler"), "LOCAL_STATE_FILE", ""), "多端本地状态"),
        "relations": (globals().get("RELATIONS_FILE", ""), "情感账户"),
        "listening": (globals().get("LISTENING_FILE", ""), "倾听笔记"),
        "health-notes": (globals().get("HEALTH_NOTES_FILE", ""), "健康笔记"),
        "proactive": (globals().get("PROACTIVE_FILE", ""), "影响圈"),
        "habit-dimensions": (globals().get("HABIT_DIMS_FILE", ""), "习惯维度"),
        "butler-profile": (globals().get("USER_PROFILE_FILE", ""), "管家画像"),
        "butler-preferences": (globals().get("BUTLER_PREFS_FILE", ""), "管家偏好"),
        "advisor-outcomes": (globals().get("ADVISOR_OUTCOMES_FILE", ""), "顾问建议结果"),
    }
    return {key: {"path": os.path.expanduser(path), "label": label}
            for key, (path, label) in rows.items() if path}

def _recovery_key_for_path(path):
    real = os.path.realpath(path)
    for key, item in _critical_json_registry().items():
        if os.path.realpath(item["path"]) == real:
            return key
    return ""

def _recovery_dir(key):
    return os.path.join(DASH_RECOVERY_BACKUP_DIR, key)

def _restrict_private_mode(path):
    """Keep JSON/Markdown state owner-only after atomic replace.

    os.replace() installs a newly-created temporary file, so relying on the
    source mode would silently turn private state back into umask-derived 644.
    Permission errors are non-fatal: the data write remains atomic and callers
    can surface the issue through the normal ops audit.
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

def _snapshot_json_before_write(path):
    """写入关键 JSON 前保存最后一个可解析版本；失败只告警，不阻断主写入。"""
    key = _recovery_key_for_path(path)
    if not key or not os.path.isfile(path):
        return True
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
        backup_dir = _recovery_dir(key)
        os.makedirs(backup_dir, exist_ok=True)
        try:
            os.chmod(backup_dir, 0o700)
        except OSError:
            pass
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = os.path.join(backup_dir, "%s.json" % stamp)
        with open(target + ".tmp", "w", encoding="utf-8") as f:
            json.dump(old, f, ensure_ascii=False, indent=2)
            f.flush(); os.fsync(f.fileno())
        os.replace(target + ".tmp", target)
        _restrict_private_mode(target)
        files = sorted(os.path.join(backup_dir, name) for name in os.listdir(backup_dir)
                       if name.endswith(".json"))
        for old_path in files[:-DASH_RECOVERY_BACKUP_LIMIT]:
            os.unlink(old_path)
        return True
    except Exception as exc:
        try:
            sys.stderr.write("[recovery-snapshot-fail] %s: %s\n" % (path, str(exc)[:200]))
        except Exception:
            pass
        return False

def _recovery_status():
    rows = []
    for key, item in _critical_json_registry().items():
        path = item["path"]
        backup_dir = _recovery_dir(key)
        backups = []
        try:
            backups = sorted((name for name in os.listdir(backup_dir) if name.endswith(".json")), reverse=True)
        except FileNotFoundError:
            pass
        valid = False
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f: json.load(f)
                valid = True
            except Exception:
                valid = False
        rows.append({"key": key, "label": item["label"], "exists": os.path.isfile(path),
                     "valid": valid, "backupCount": len(backups),
                     "latestBackup": backups[0] if backups else ""})
    return {"success": True, "items": rows}

def _ensure_recovery_baselines():
    """为尚无历史快照的关键数据域建立一次基线；已有快照时绝不重复制造。"""
    created = []
    for key, item in _critical_json_registry().items():
        path = item["path"]
        backup_dir = _recovery_dir(key)
        try:
            existing = [name for name in os.listdir(backup_dir) if name.endswith(".json")]
        except FileNotFoundError:
            existing = []
        if existing or not os.path.isfile(path):
            continue
        if _snapshot_json_before_write(path):
            created.append(key)
    return created

def _ensure_recovery_domain_files():
    """Create valid empty state for critical domains that legitimately start empty."""
    path = os.path.expanduser(globals().get("ADVISOR_OUTCOMES_FILE", ""))
    if not path or os.path.exists(path):
        return []
    return ["advisor-outcomes"] if _write_json(path, []) else []

def _ops_status():
    """运维状态只返回时间、状态和计数，不暴露路径、命令输出或业务正文。"""
    deploy = _read_json(os.path.join(DASHBOARD_DIR, ".deploy-status.json"), {}) or {}
    weekly_job = _read_json(os.path.join(DASHBOARD_DIR, ".weekly-draft-job.json"), {}) or {}
    draft_path = getattr(globals().get("DashboardHandler"), "WEEKLY_DRAFT_FILE", "")
    draft = _read_json(draft_path, {}) if draft_path else {}
    timer = "development"
    if ON_VPS:
        try:
            proc = subprocess.run(["systemctl", "is-active", "dashboard-weekly-draft.timer"],
                                  capture_output=True, text=True, timeout=2)
            timer = (proc.stdout or "inactive").strip()[:32]
        except Exception:
            timer = "unknown"
    recovery = _recovery_status().get("items", [])
    return {"success": True,
            "deploy": {k: deploy.get(k) for k in
                       ("release", "deployed_at", "status", "rollback_available",
                        "backup_keep", "backup_count")},
            "weeklyDraft": {"timer": timer,
                            "week": draft.get("week") if isinstance(draft, dict) else "",
                            "generatedAt": draft.get("generated_at") if isinstance(draft, dict) else "",
                            "lastRun": {k: weekly_job.get(k) for k in
                                        ("started_at", "finished_at", "status", "exit_code")}},
            "recovery": {"domains": len(recovery),
                         "withBackups": sum(1 for item in recovery if item.get("backupCount", 0) > 0),
                         "invalid": sum(1 for item in recovery if item.get("exists") and not item.get("valid"))},
            "data": _data_authority_status()}

def _restore_json_snapshot(key, backup=""):
    item = _critical_json_registry().get(str(key or ""))
    if not item:
        return {"success": False, "error": "不允许恢复该数据域"}
    backup_dir = _recovery_dir(key)
    names = []
    try:
        names = sorted((name for name in os.listdir(backup_dir) if name.endswith(".json")), reverse=True)
    except FileNotFoundError:
        pass
    name = os.path.basename(str(backup or "")) if backup else (names[0] if names else "")
    if not name or name not in names:
        return {"success": False, "error": "没有可用的正常快照"}
    source = os.path.join(backup_dir, name)
    try:
        with open(source, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        return {"success": False, "error": "快照不可读：%s" % str(exc)[:160]}
    current_snapshot_ok = _snapshot_json_before_write(item["path"])
    # 只有用户在恢复台明确确认后才解除损坏保护；普通写入仍会继续拒绝覆盖坏文件。
    _corrupt_json_files.discard(os.path.realpath(item["path"]))
    if not _write_json(item["path"], payload):
        return {"success": False, "error": "恢复写入失败，原文件未被非原子覆盖"}
    bust_cache()
    return {"success": True, "key": key, "label": item["label"], "backup": name,
            "verified": _read_json(item["path"], None) == payload,
            "currentSnapshot": bool(current_snapshot_ok)}

def _write_json(path, data):
    with _file_lock:
        try:
            if os.path.realpath(path) in _corrupt_json_files:
                print(f"[write_json] {path} 处于损坏保护状态，拒绝覆盖原文件", flush=True)
                return False
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _snapshot_json_before_write(path)
            # 原子写：先写临时文件再 os.replace，杜绝"写一半崩溃留半截文件"
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            _restrict_private_mode(path)
            return True
        except Exception as e:
            print(f"[write_json] {path} 写入失败: {e}", flush=True)
            try:
                if os.path.exists(path + ".tmp"):
                    os.remove(path + ".tmp")
            except Exception:
                pass
            return False

def _write_text_atomic(path, text):
    """原子写入文本文件，避免 Markdown 在进程中断时留下半截内容。"""
    with _file_lock:
        tmp = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            _restrict_private_mode(path)
            return True
        except Exception as e:
            print(f"[write_text] {path} 写入失败: {e}", flush=True)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            return False

# ─── 软删回收站（关系/倾听/影响圈）：deleted:true 对读取层不可见，超 7 天惰性物理清理 ───
SOFT_DELETE_TTL_DAYS = 7

def _alive(items):
    """读取层统一过滤：软删条目不可见（数据仍在 JSON 里可恢复）"""
    return [x for x in (items or []) if not (isinstance(x, dict) and x.get("deleted"))]

def _soft_mark(items, ids):
    """把命中 id 的条目标记软删；返回命中数（不物理移除）"""
    idset = set(ids)
    hit = 0
    for x in items:
        if isinstance(x, dict) and x.get("id") in idset and not x.get("deleted"):
            x["deleted"] = True
            x["deleted_at"] = date.today().isoformat()
            x["ts"] = datetime.now().isoformat(timespec="seconds")   # 软删刷新 ts → 删除随 merge 传播到另一端（根治「删后又现+双端不同步」）
            hit += 1
    return hit

def _purge_expired(items):
    """惰性清理：软删超过 7 天的条目才物理删除；返回 (新列表, 清理数)"""
    cutoff = (date.today() - timedelta(days=SOFT_DELETE_TTL_DAYS)).isoformat()
    kept, purged = [], 0
    for x in items:
        if (isinstance(x, dict) and x.get("deleted")
                and (x.get("deleted_at") or "9999-12-31") <= cutoff):
            purged += 1
        else:
            kept.append(x)
    return kept, purged

def _restore(items, rid):
    """按 id 把软删条目恢复可见；返回是否命中"""
    for x in items:
        if isinstance(x, dict) and x.get("id") == rid and x.get("deleted"):
            x["deleted"] = False
            x.pop("deleted_at", None)
            x["ts"] = datetime.now().isoformat(timespec="seconds")   # 恢复也刷新 ts → 恢复随 merge 传播到另一端（对称根治双端不同步）
            return True
    return False

def _load_habit_dims():
    """习惯名 -> {dimension, role} 的本地映射（TickTick habit 不支持自定义字段的全量兜底）。
    读时统一归一化：旧格式值为纯字符串=维度、role 为空，下游只需按 dict 取值。"""
    raw = _read_json(HABIT_DIMS_FILE, {})
    dims = {}
    for name, v in (raw or {}).items():
        if isinstance(v, dict):
            d, role = v.get("dimension") or "", v.get("role") or ""
        elif isinstance(v, str):
            d, role = v, ""
        else:
            d, role = "", ""
        if d or role:
            dims[name] = {"dimension": d, "role": role}
    return dims

def _save_habit_dims(dims):
    return _write_json(HABIT_DIMS_FILE, dims)

# 隐藏以 __e2e__ 开头的测试习惯，避免测试数据进入页面、统计或 AI 上下文。
_E2E_HABIT_PREFIX = "__e2e__"

def _is_junk_habit(name):
    return (name or "").strip().startswith(_E2E_HABIT_PREFIX)

def _call_llm(messages, temperature=0.8, max_tokens=1400, json_mode=False, model=None, tools=None, feature="unknown"):
    """调用 OpenAI 兼容接口；主模型失败自动切兜底模型。

    Batch 5 adds metadata-only timing records.  Message bodies, prompts and
    generated text are deliberately never sent to the telemetry ledger.
    """
    base = _cfg.get("LLM_API_BASE", "").rstrip("/")
    key = _cfg.get("LLM_API_KEY", "")
    if not base or not key:
        record_call(feature, model or _cfg.get("LLM_MODEL", ""), False, 0,
                    error_code="not_configured")
        return {"error": "LLM 未配置（缺少 LLM_API_BASE / LLM_API_KEY）"}
    primary = model or _cfg.get("LLM_MODEL", "")
    fallback = _cfg.get("LLM_FALLBACK", "")
    last_err = None
    for attempt, mdl in enumerate([primary, fallback]):
        if not mdl:
            continue
        started = time.perf_counter()
        try:
            body = {"model": mdl, "messages": messages, "temperature": temperature,
                    "max_tokens": max_tokens}
            if mdl.startswith(("deepseek-", "doubao-seed-2-1-", "glm-")):
                body["thinking"] = {"type": "disabled"}
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            if tools:
                body["tools"] = tools
                # 优先让模型自主决定是否调用 do_action 工具（auto）；
                # 同时保留 <action> 文本指令作为兜底通道，任一路命中即视为动作。
                body["tool_choice"] = "auto"
            req = urllib.request.Request(base + "/chat/completions",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = json.loads(resp.read())
            msg = raw["choices"][0]["message"]
            record_call(feature, mdl, True, (time.perf_counter() - started) * 1000,
                        fallback_reason=("primary_failed" if attempt else ""))
            return {"content": msg.get("content") or "", "tool_calls": msg.get("tool_calls") or []}
        except Exception as e:
            last_err = str(e)
            record_call(feature, mdl, False, (time.perf_counter() - started) * 1000,
                        fallback_reason=("primary_failed" if attempt == 0 and fallback else ""),
                        error_code=type(e).__name__)
    return {"error": last_err or "LLM 调用失败"}

def _call_llm_stream(messages, temperature=0.8, max_tokens=1400, tools=None, feature="unknown"):
    """流式调用 LLM，yield 逐块内容；失败 yield {"_error": "..."}

    2026-08-18 切到火山方舟 Coding Plan（ark-code-latest 智能路由），
    走 LLM_API_BASE 配置的 /api/coding/v3，套餐内 token 不额外计费。

    优先级（按配置顺序回退）：
      1. LLM_MODEL（ark-code-latest 智能路由）
      2. LLM_FALLBACK（doubao-seed-2-1-turbo 显式旗舰）
    都不通则 yield {"_error"}。

    SSE 帧解析：按 \\n\\n 标准分帧，过滤 reasoning_content（思考链不要塞给用户）。"""
    base = _cfg.get("LLM_API_BASE", "").rstrip("/")
    key = _cfg.get("LLM_API_KEY", "")
    if not base or not key:
        record_call(feature, _cfg.get("LLM_MODEL", ""), False, 0,
                    error_code="not_configured")
        yield {"_error": "LLM 未配置（LLM_API_BASE / LLM_API_KEY 缺失）"}
        return
    primary = _cfg.get("LLM_MODEL", "")
    fallback = _cfg.get("LLM_FALLBACK", "")
    models = [m for m in [primary, fallback] if m]
    if not models:
        record_call(feature, "", False, 0, error_code="model_not_configured")
        yield {"_error": "LLM 未配置（LLM_MODEL / LLM_FALLBACK 缺失）"}
        return
    last_err = None
    for attempt, mdl in enumerate(models):
        started = time.perf_counter()
        first_byte_at = None
        try:
            body = {"model": mdl, "messages": messages, "temperature": temperature,
                    "max_tokens": max_tokens, "stream": True}
            # 管家动作抽取走 function calling（tools），由调用方传入；不传则纯文本
            if tools:
                body["tools"] = tools
                # 优先让模型自主决定是否调用 do_action 工具（auto）；
                # 同时保留 <action> 文本指令作为兜底通道，任一路命中即视为动作。
                body["tool_choice"] = "auto"
            # DeepSeek V4 / doubao-seed-2-1 默认带思考链，非流式要等思考全部完成（turbo 实测 25.8s→2.5s）；
            # 管家深度靠 System Prompt 注入的驾驶舱数据，不依赖思考链，禁用换速度。
            if mdl.startswith(("deepseek-", "doubao-seed-2-1-", "glm-")):
                body["thinking"] = {"type": "disabled"}
            acc_tools = {}
            req = urllib.request.Request(base + "/chat/completions",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
            with urllib.request.urlopen(req, timeout=90) as resp:
                buf = b""
                pending = b""
                for chunk in iter(lambda: resp.read(2048), b""):
                    pending += chunk
                    # 标准 SSE 帧按 \n\n 分隔
                    while b"\n\n" in pending:
                        frame, pending = pending.split(b"\n\n", 1)
                        for line in frame.split(b"\n"):
                            line = line.strip()
                            if not line or line.startswith(b":") or line == b"data: [DONE]":
                                continue
                            if not line.startswith(b"data:"):
                                continue
                            payload = line[5:].strip()
                            if not payload or payload == b"[DONE]":
                                continue
                            try:
                                obj = json.loads(payload)
                                delta = obj.get("choices", [{}])[0].get("delta", {})
                                # 关键：只取 content，过滤 reasoning_content（思考链）
                                content = delta.get("content") or ""
                                if content:
                                    if first_byte_at is None:
                                        first_byte_at = time.perf_counter()
                                    yield {"delta": content}
                                # 累积 function calling 的 tool_calls（流式分片按 index 拼接）
                                for tc in (delta.get("tool_calls") or []):
                                    idx = tc.get("index", 0)
                                    slot = acc_tools.setdefault(idx, {"name": "", "arguments": ""})
                                    fn = tc.get("function", {}) or {}
                                    if fn.get("name"):
                                        slot["name"] = fn["name"]
                                    if fn.get("arguments"):
                                        slot["arguments"] += fn["arguments"]
                            except Exception:
                                continue
                # 流尾 flush：处理无尾换行的最后一帧
                tail = pending.strip()
                if tail.startswith(b"data:") and tail != b"data: [DONE]":
                    p = tail[5:].strip()
                    if p and p != b"[DONE]":
                        try:
                            obj = json.loads(p)
                            delta = obj.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content") or ""
                            if content:
                                if first_byte_at is None:
                                    first_byte_at = time.perf_counter()
                                yield {"delta": content}
                        except Exception:
                            pass
            # 流式结束：把累积的 tool_calls 一并发出，供调用方组装动作（无工具调用则不发）
            if acc_tools:
                if first_byte_at is None:
                    first_byte_at = time.perf_counter()
                yield {"tool_calls": [acc_tools[i] for i in sorted(acc_tools)]}
            record_call(feature, mdl, True, (time.perf_counter() - started) * 1000,
                        first_byte_ms=((first_byte_at - started) * 1000 if first_byte_at else None),
                        fallback_reason=("primary_failed" if attempt else ""))
            return
        except Exception as e:
            last_err = f"{mdl}: {e}"
            record_call(feature, mdl, False, (time.perf_counter() - started) * 1000,
                        first_byte_ms=((first_byte_at - started) * 1000 if first_byte_at else None),
                        fallback_reason=("primary_failed" if attempt == 0 and len(models) > 1 else ""),
                        error_code=type(e).__name__)
            continue
    yield {"_error": last_err or "LLM 流式调用失败"}

def _load_butler_history():
    """读取 AI 管家对话历史（最多 BUTLER_MAX_HISTORY 条）"""
    data = _read_json(BUTLER_HISTORY_FILE, [])
    return data if isinstance(data, list) else []

def _save_butler_history(msgs):
    """保存对话历史，超限自动裁剪尾部（保留最近的），被裁旧内容沉淀为长期记忆。
    B9: 连续去重——重试时同一 user/assistant 消息被重复 append，相邻相同 role+content 只留一条。"""
    deduped = []
    for m in msgs:
        if (deduped and deduped[-1].get("role") == m.get("role")
                and deduped[-1].get("content") == m.get("content")):
            continue
        deduped.append(m)
    msgs = deduped
    if len(msgs) > BUTLER_MAX_HISTORY:
        _append_butler_memory(msgs[:-BUTLER_MAX_HISTORY])
        msgs = msgs[-BUTLER_MAX_HISTORY:]
    _write_json(BUTLER_HISTORY_FILE, msgs)

def _clear_butler_history():
    """清空对话历史"""
    return _write_json(BUTLER_HISTORY_FILE, [])

def _load_butler_prefs():
    """读取用户偏好/记忆"""
    data = _read_json(BUTLER_PREFS_FILE, {})
    return data if isinstance(data, dict) else {}

def _save_butler_pref(key, value):
    """保存单条用户偏好"""
    prefs = _load_butler_prefs()
    prefs[key] = value
    if not _write_json(BUTLER_PREFS_FILE, prefs):
        return None
    return prefs

def _butler_prefs_text():
    """把用户偏好格式化为文本注入 system prompt"""
    prefs = _load_butler_prefs()
    if not prefs:
        return ""
    lines = [f"- {k}：{v}" for k, v in prefs.items()]
    return "【用户偏好/记忆】\n" + "\n".join(lines) + "\n"

def _last_review_week(roles):
    """从角色文件复盘段里找最近一次复盘周号（如 2026-W33）"""
    import re as _re
    mx = ""
    for r in (roles or []):
        rev = (r.get("review") or "") + "\n" + (r.get("review_log") or "")
        for m in _re.findall(r"###\s*(\d{4}-W\d{2})", rev):
            if m > mx:
                mx = m
    return mx

ACTION_ALIASES = {
    "create_task": "建任务", "建任务": "建任务", "新建任务": "建任务", "加任务": "建任务",
    "create_habit": "建习惯", "建习惯": "建习惯", "新建习惯": "建习惯",
    "add_relation": "记关系", "记关系": "记关系", "情感账户": "记关系",
    "write_review": "写复盘", "写复盘": "写复盘", "复盘": "写复盘",
    "add_big_rock": "定大石头", "定大石头": "定大石头", "大石头": "定大石头",
    "save_pref": "记偏好", "记偏好": "记偏好", "记住": "记偏好", "偏好": "记偏好",
    "delete_task": "删任务", "删任务": "删任务", "删除任务": "删任务", "清理任务": "删任务",
    "delete_relation": "删关系", "删关系": "删关系", "删除关系": "删关系",
    "delete_listening": "删倾听", "删倾听": "删倾听", "删除倾听": "删倾听", "删笔记": "删倾听",
    "delete_health_note": "删睡眠备注", "删睡眠备注": "删睡眠备注", "删除睡眠备注": "删睡眠备注", "删备注": "删睡眠备注", "睡眠备注": "删睡眠备注", "健康备注": "删睡眠备注",
    "update_task": "改任务", "改任务": "改任务", "编辑任务": "改任务", "修改任务": "改任务", "重命名任务": "改任务",
    "update_habit": "改习惯", "改习惯": "改习惯", "编辑习惯": "改习惯", "修改习惯": "改习惯",
    "update_relation": "改关系", "改关系": "改关系", "编辑关系": "改关系", "修改关系": "改关系",
    "update_listening": "改倾听", "改倾听": "改倾听", "编辑倾听": "改倾听", "修改倾听": "改倾听", "改笔记": "改倾听",
    "delete_role": "删角色", "删角色": "删角色", "删除角色": "删角色", "删掉角色": "删角色", "移除角色": "删角色", "注销角色": "删角色",
    "delete_concern": "删关注", "删关注": "删关注", "删关注圈": "删关注", "删影响圈": "删关注", "删除关注": "删关注",
    "删焦虑": "删关注", "删掉焦虑": "删关注", "删掉关注": "删关注", "删除影响圈": "删关注", "删条": "删关注", "删条目": "删关注",
    "update_concern": "改关注", "改关注": "改关注", "改关注圈": "改关注", "改影响圈": "改关注", "修改关注": "改关注", "改焦虑": "改关注",
    # P2-B11: 补上 BUTLER_TOOLS 枚举里有、但文本 <action> 通道缺失的两个动作，
    # 否则模型走文本通道（非 function calling）时这两个动作会被 ACTION_ALIASES.get 查不到而丢弃。
    "set_reminder": "定时提醒", "定时提醒": "定时提醒",
    "set_constraint": "改约束", "改约束": "改约束",
}

# v41：注册表实体别名自动扩展（不覆盖已有键）——建/删/改/查 + 实体名 → 标准 kind
_crud_alias_add = {}
for _reg in CRUD_REGISTRY:
    _n = _reg["name"]
    for _cn, _v in (("建", "建"), ("新增", "建"), ("记", "建"), ("写", "建"), ("加", "建"),
                    ("删", "删"), ("删除", "删"), ("移除", "删"),
                    ("改", "改"), ("编辑", "改"), ("修改", "改"),
                    ("查", "查"), ("看", "查"), ("列出", "查")):
        _key = _cn + _n
        if _key not in ACTION_ALIASES:
            _crud_alias_add[_key] = _v + _n
ACTION_ALIASES.update(_crud_alias_add)

# 管家动作抽取用 function calling schema（单一工具：kind 枚举 + 中文参数对象）
# 与 butler_act 的中文键约定一致（标题/项目/象限/角色/优先级/截止 等）
def _build_butler_tools():
    """v41：BUTLER_TOOLS 动态构建——静态 kind + 注册表实体自动生成 建/删/改/查 kind 与字段说明。
    新增实体只需在 CRUD_REGISTRY 加一行，管家工具描述即自动包含该实体全部操作。"""
    static_kinds = ["建任务", "建习惯", "记关系", "写复盘", "定大石头", "记偏好",
                    "删任务", "删关系", "删倾听", "改任务", "改习惯", "改关系", "改倾听",
                    "删角色", "删关注", "改关注", "定时提醒", "改约束", "删睡眠备注"]
    dyn = []
    for reg in CRUD_REGISTRY:
        n = reg["name"]
        for v in ("建", "删", "改", "查"):
            dyn.append(v + n)
    kinds = []
    for k in static_kinds + dyn:
        if k not in kinds:
            kinds.append(k)
    ent_desc = "；".join(f"{reg['name']}可用 {'/'.join(reg['fields'].values())}" for reg in CRUD_REGISTRY)
    return [{
        "type": "function",
        "function": {
            "name": "do_action",
            "description": "当用户明确要执行一个驾驶舱动作（建任务/建习惯/记关系/写复盘/定大石头/记偏好/删改各类条目/增删改查注册表实体）时调用；普通聊天不要调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": kinds},
                    "args": {"type": "object", "description": (
                        "动作参数对象，键用中文：建任务可用 标题/项目/象限(q1-q4)/角色/优先级(1-4)/开始/截止；"
                        "「X点提醒我Y」：一次性具体时刻（今天8点/今晚8点/明天早上9点/周五下午3点/8月25日晚上10点）→ 用 建任务，标题=Y、截止=该时刻，到点自动推送提醒；只有重复性（每天X点/每周X/每隔X小时）才用 定时提醒；"
                        "建任务若用户给了时段（如\"明天9点到11点\"）→ 开始=明天9点、截止=明天11点，用开始+截止存成时间段；用户没给具体时刻只说了日期时→先主动问\"几点做？还是全天？\"，别默认存成0点；"
                        "建任务额外自由字段（可选）：重复(每天/每周五/每月15号/每两周/工作日)、提前提醒(提前10分钟/提前1小时)、清单项(用顿号或逗号分隔的子任务列表)、标签、备注——你理解用户的话后自由填这些字段，系统会正确落地；"
                        "建习惯可用 名称/维度(身体|精神|智力|社会情感)/天数/目标/单位；"
                        "记关系可用 对象/类型(存款|取款)/事由/金额/角色；写复盘可用 角色/内容；"
                        "定大石头可用 标题/理由/角色/截止；记偏好可用 key/value；定时提醒可用 内容/时间（时间支持：每天早上X点 / 每周X / 每隔X小时）；改约束可用 内容（用户给管家的行为要求，如「回复简短点」）；"
                        "删角色可用 名称(精确角色名)；删关注可用 关键词(关注圈/影响圈条目的文字)；"
                        "改关注可用 关键词(原条目文字)+新内容；"
                        f"【通用实体（建/删/改/查 + 实体名，如 查睡眠备注/建关系/删关注/改倾听笔记）：{ent_desc}；查X 列出该实体最近条目；删除按 内容 关键词或 id 定位；修改用 关键词(定位原条目)+新内容(新值)】"
                    )}
                },
                "required": ["kind", "args"]
            }
        }
    }]


BUTLER_TOOLS = _build_butler_tools()
def _norm_priority(v):
    """把优先级归一为 0-5 整数：兼容数字、'高/中/低'、'P3'、'紧急' 等写法"""
    if isinstance(v, bool):
        return 3
    if isinstance(v, (int, float)):
        return max(0, min(5, int(round(v))))
    s = (str(v) or "").strip().lstrip("Pp")
    table = {"最高": 5, "很高": 5, "紧急": 5, "高": 4, "中高": 4, "偏高": 4,
             "中": 3, "普通": 3, "一般": 3, "中低": 2, "偏低": 2, "低": 1, "最低": 0}
    if s in table:
        return table[s]
    if s.isdigit():
        return max(0, min(5, int(s)))
    return 3


def _norm_quad(v):
    """把四象限的中文说法归一为 q1-q4：重要不紧急→q2 等"""
    if not v:
        return ""
    s = str(v).strip().lower()
    if s in ("q1", "q2", "q3", "q4"):
        return s
    if "重要" in s and "紧急" in s:
        # 重要紧急 → q1；重要不紧急 → q2
        if "不紧急" in s or "不急" in s or "非紧急" in s:
            return "q2"
        if "紧急" in s:
            return "q1"
    if "紧急" in s and "不重要" in s:
        return "q3"
    if "不重要" in s and "不紧急" in s:
        return "q4"
    if "重要" in s:
        return "q2"
    if "紧急" in s:
        return "q1"
    return ""


def _norm_project(v):
    """把项目名归一为 PROJECT_IDS 的合法键；认不出返回空（调用方回落收集箱）"""
    if not v:
        return ""
    s = str(v).strip()
    if s in PROJECT_IDS:
        return s
    table = {"收集": "📥 收集箱", "inbox": "📥 收集箱", "工作": "🖥️ 工作安排",
             "生活": "🏠 值得生活", "四象限": "四象限", "矩阵": "四象限"}
    for k, dst in table.items():
        if k in s.lower():
            return dst
    return ""


_DIM_ALIASES = {
    "身体": "身体", "运动": "身体", "健康": "身体", "健身": "身体", "体能": "身体", "锻炼": "身体", "睡眠": "身体", "饮食": "身体",
    "精神": "精神", "心灵": "精神", "灵性": "精神", "冥想": "精神", "意志": "精神", "情绪": "精神", "心态": "精神",
    "智力": "智力", "心智": "智力", "脑力": "智力", "学习": "智力", "思维": "智力", "阅读": "智力", "读书": "智力", "写作": "智力", "成长": "智力",
    "社会情感": "社会情感", "社交": "社会情感", "情感": "社会情感", "关系": "社会情感", "社会": "社会情感", "沟通": "社会情感", "陪伴": "社会情感", "家庭": "社会情感",
}


def _norm_dimension(v, name=""):
    """把习惯维度归一为四维之一；先精确/别名匹配，再用名称关键词兜底"""
    s = str(v or "").strip()
    if s in HABIT_DIMENSIONS:
        return s
    if s in _DIM_ALIASES:
        return _DIM_ALIASES[s]
    hay = s + " " + str(name or "")
    for kw, dim in (("跑", "身体"), ("步", "身体"), ("锻炼", "身体"), ("健身", "身体"), ("喝水", "身体"),
                    ("睡眠", "身体"), ("睡觉", "身体"), ("俯卧撑", "身体"), ("拉伸", "身体"), ("饮食", "身体"),
                    ("读", "智力"), ("学", "智力"), ("背", "智力"), ("写", "智力"), ("课", "智力"), ("英语", "智力"), ("代码", "智力"),
                    ("冥想", "精神"), ("反思", "精神"), ("感恩", "精神"), ("呼吸", "精神"), ("日记", "精神"),
                    ("联系", "社会情感"), ("聊天", "社会情感"), ("问候", "社会情感"), ("陪", "社会情感"), ("见", "社会情感")):
        if kw in hay:
            return dim
    return ""




def _parse_dt_full(v):
    """把「今天8点/今晚8点/明天早上9点/周五下午3点」这类具体时刻归一为
    完整时间戳 YYYY-MM-DDTHH:MM:SS+08:00，并标记是否需要 reminders 到点提醒。
    返回 (due_ts, need_reminder)；认不出具体时刻返回 ('', False)。
    注意：只有「每天/每周/每隔」这种重复才走 cron；这里处理一次性具体时间。"""
    import re as _r
    if not v:
        return "", False
    s = str(v).strip()
    low = s.lower()
    # 重复性时间（每天/每日/每周/每隔/每小时/每个）不属于一次性具体提醒，走 cron
    if _r.search(r"(每[天日周晚早中午]|每\d+小时|每个?小时)", low) or low.startswith("每"):
        return "", False
    today = date.today()
    # 1) 提取日期基准（今天/明天/后天/周X/下周三/8月20日/N天后/N周后）
    day_delta = None
    base_date = None
    m = _r.search(r"([0-9]+|[一二两三四五六七八九十]+)个?[天日]后", s)
    if m:
        n = _cn_int(m.group(1))
        if n:
            base_date = today + timedelta(days=n)
    if base_date is None:
        m = _r.search(r"([0-9]+|[一二两三四五六七八九十]+)个?[周星期]后", s)
        if m:
            n = _cn_int(m.group(1))
            if n:
                base_date = today + timedelta(weeks=n)
    if base_date is None:
        for kw, delta in (("大后天", 3), ("后天", 2), ("明天", 1), ("明晚", 1), ("明早", 1),
                          ("今天", 0), ("今日", 0), ("今晚", 0), ("今早", 0)):
            if low.startswith(kw):
                base_date = today + timedelta(days=delta)
                break
    if base_date is None:
        wd_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
        m = _r.search(r"下周([一二三四五六日天])", s)
        if m:
            base_date = today + timedelta(days=(7 - today.weekday() + wd_map[m.group(1)]) % 7 or 7)
        else:
            m = _r.search(r"(?:这|本)?(?:周|星期|礼拜)([一二三四五六日天])", s)
            if m:
                delta = (wd_map[m.group(1)] - today.weekday()) % 7
                base_date = today + timedelta(days=delta if delta else 7)
            else:
                m = _r.search(r"(\d{1,2})月(\d{1,2})日?", s)
                if m:
                    try:
                        base_date = today.replace(month=int(m.group(1)), day=int(m.group(2)))
                    except ValueError:
                        base_date = None
    if base_date is None:
        base_date = today
    # 2) 提取时刻（X点 / X点Y分 / 上午/下午/晚上/中午/凌晨 前缀）
    hour, minute = None, 0
    period = None
    if "凌晨" in s: period = "凌晨"
    elif "早上" in s or "早晨" in s or "上午" in s: period = "上午"
    elif "中午" in s: period = "中午"
    elif "下午" in s: period = "下午"
    elif "晚上" in s or "傍晚" in s or "今晚" in s: period = "晚上"
    m = _r.search(r"([0-9]{1,2})[点:：时](半|\d{1,2})?分?", s)
    if not m:
        # 纯「8点」无日期前缀 → 今天
        return "", False
    hour = int(m.group(1))
    _mm = m.group(2)
    minute = 30 if (_mm == "半") else int(_mm or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return "", False
    # 3) 12 小时制时段修正（下午3点→15点；晚上12点→次日0点；凌晨12点→0点等）
    if period == "下午" and hour < 12: hour += 12
    elif period == "晚上":
        if hour == 12:
            hour = 0
            base_date = base_date + timedelta(days=1)  # 晚上12点 = 次日0点
        elif hour >= 1:
            hour += 12
    elif period == "凌晨":
        if hour == 12:
            hour = 0  # 凌晨12点 = 0点
    elif period == "中午" and hour < 12: hour += 12  # 中午12点前视为12点附近
    # 4) 组装完整时间戳
    try:
        dt = datetime.combine(base_date, datetime.min.time()).replace(hour=hour, minute=minute)
    except Exception:
        return "", False
    return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00"), True



_WD_RRULE = {"一": "MO", "二": "TU", "三": "WE", "四": "TH", "五": "FR", "六": "SA", "日": "SU", "天": "SU"}

def _repeat_to_rrule(text):
    """把中文重复频率转成 TickTick repeatFlag 的 RRULE。认不出返回 ''。
    支持：每天/每日/每个工作日/每周X/每两周/每月X号/每月/每年X月X日"""
    import re as _r
    if not text:
        return ""
    s = str(text).strip()
    # 每个工作日 / 工作日
    if "工作日" in s:
        return "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
    # 每天 / 每日 / 天天
    if _r.search(r"(每天|每日|天天|每一天)", s):
        return "RRULE:FREQ=DAILY"
    # 每两周
    if "两周" in s or "半月" in s:
        return "RRULE:FREQ=WEEKLY;INTERVAL=2"
    # 每周X / 每星期X
    m = _r.search(r"每(?:周|星期|礼拜)([一二三四五六日天])", s)
    if m and m.group(1) in _WD_RRULE:
        return "RRULE:FREQ=WEEKLY;BYDAY=%s" % _WD_RRULE[m.group(1)]
    # 每年X月X日（必须在周X之前，避免「日」被周匹配）
    m = _r.search(r"每?年([0-9]{1,2})月([0-9]{1,2})[号日]?", s)
    if m:
        return "RRULE:FREQ=YEARLY;BYMONTH=%d;BYMONTHDAY=%d" % (int(m.group(1)), int(m.group(2)))
    # 每年
    if "每年" in s or "每一年" in s:
        return "RRULE:FREQ=YEARLY"
    # 周X / 每星期一三五
    m = _r.search(r"([一二三四五六日天]{1,5})", s)
    if m and all(c in _WD_RRULE for c in m.group(1)):
        days = ",".join(_WD_RRULE[c] for c in m.group(1))
        return "RRULE:FREQ=WEEKLY;BYDAY=%s" % days
    # 每月X号 / 每月X日
    m = _r.search(r"每?月([0-9]{1,2})[号日]", s)
    if m:
        return "RRULE:FREQ=MONTHLY;BYMONTHDAY=%d" % int(m.group(1))
    # 每月
    if "每月" in s or "每个月" in s:
        return "RRULE:FREQ=MONTHLY"
    return ""


def _reminder_offset(text):
    """把「提前X分钟/小时提醒」转成 TRIGGER 偏移；没有则返回 None（默认到点提醒）。"""
    import re as _r
    if not text:
        return None
    s = str(text).strip()
    m = _r.search(r"提前\s*([0-9]+)\s*分钟", s)
    if m:
        return "TRIGGER:-PT%dM" % int(m.group(1))
    m = _r.search(r"提前\s*([0-9]+(?:\.[0-9]+)?)\s*小时", s)
    if m:
        return "TRIGGER:-PT%gH" % (float(m.group(1)))
    m = _r.search(r"提前\s*([0-9]+)\s*天", s)
    if m:
        return "TRIGGER:-P%dD" % int(m.group(1))
    return None

def _norm_due(v):
    """把自然语言日期归一为 YYYY-MM-DD（滴答可接受）；认不出返回 ''（调用方按无截止处理）"""
    if v in (None, ""):
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        ts = float(v)
        if ts > 1e12:
            ts /= 1000
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except Exception:
            return ""
    s = str(v).strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return ""
    today = date.today()
    low = s.lower()
    # N天后 / N周后 / 一周后 / 两周后（纯日期，无时刻）
    m = re.match(r"^([0-9]+|[一二两三四五六七八九十]+)个?[天日]后$", s)
    if m:
        n = _cn_int(m.group(1))
        if n:
            return (today + timedelta(days=n)).isoformat()
    m = re.match(r"^([0-9]+|[一二两三四五六七八九十]+)个?[周星期]后$", s)
    if m:
        n = _cn_int(m.group(1))
        if n:
            return (today + timedelta(weeks=n)).isoformat()
    m = re.match(r"^([0-9]+|[一二两三四五六七八九十]+)个?月后$", s)
    if m:
        n = _cn_int(m.group(1))
        if n:
            nm = today.month + n
            ny = today.year + (nm - 1) // 12
            nm = (nm - 1) % 12 + 1
            try:
                return date(ny, nm, today.day).isoformat()
            except ValueError:
                return ""
    # 今天/明天/后天/今晚/明晚 支持后缀（"明天下午""今晚8点"都按当天/次日归一）
    for kw, delta in (("大后天", 3), ("后天", 2), ("明天", 1), ("明晚", 1), ("明早", 1),
                      ("今天", 0), ("今日", 0), ("今晚", 0), ("今早", 0), ("today", 0), ("tomorrow", 1)):
        if low.startswith(kw):
            return (today + timedelta(days=delta)).isoformat()
    if low in ("后天", "后日"):
        return (today + timedelta(days=2)).isoformat()
    wd_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    # 下下周五 / 下下周X：比「下周X」再晚 7 天
    m = re.match(r"^下下(?:周|星期|礼拜)([一二三四五六日天])$", s)
    if m:
        return (today + timedelta(days=(14 - today.weekday() + wd_map[m.group(1)]) % 7 or 14)).isoformat()
    # 这周五 / 本周五：本周内（若已过则顺延下周）
    m = re.match(r"^(?:这|本)(?:周|星期|礼拜)([一二三四五六日天])$", s)
    if m:
        delta = (wd_map[m.group(1)] - today.weekday()) % 7
        return (today + timedelta(days=delta if delta else 7)).isoformat()
    # 月底 / 月末
    if low in ("月底", "月末", "本月底", "本月末"):
        import calendar as _cal
        last = _cal.monthrange(today.year, today.month)[1]
        return today.replace(day=last).isoformat()
    # 下个月 / 下月X号
    m = re.match(r"^下个?月(?:(\d{1,2})[日号]?)?$", low)
    if m and m.group(1) is None and low in ("下月", "下个月"):
        import calendar as _cal2
        ny = today.year + (1 if today.month == 12 else 0)
        nm = 1 if today.month == 12 else today.month + 1
        last = _cal2.monthrange(ny, nm)[1]
        return date(ny, nm, last).isoformat()
    m = re.match(r"^下个?月(\d{1,2})[日号]?$", low)
    if m:
        ny = today.year + (1 if today.month == 12 else 0)
        nm = 1 if today.month == 12 else today.month + 1
        try:
            return date(ny, nm, int(m.group(1))).isoformat()
        except ValueError:
            return ""
    # 周末
    if low in ("周末", "这周末", "本周末"):
        delta = (5 - today.weekday()) % 7
        return (today + timedelta(days=delta if delta else 7)).isoformat()
    # 本周/这周 → 本周日
    if low in ("本周", "这周", "这星期", "本星期"):
        delta = (6 - today.weekday()) % 7
        return (today + timedelta(days=delta if delta else 7)).isoformat()
    if s.startswith("下周") and len(s) >= 3 and s[2] in wd_map:
        return (today + timedelta(days=(7 - today.weekday() + wd_map[s[2]]) % 7 or 7)).isoformat()
    if (s.startswith("周") or s.startswith("星期") or s.startswith("礼拜")) and s[-1] in wd_map:
        delta = (wd_map[s[-1]] - today.weekday()) % 7 or 7
        return (today + timedelta(days=delta)).isoformat()
    m = re.match(r"^(\d{1,2})月(\d{1,2})日?$", s)
    if m:
        try:
            return today.replace(month=int(m.group(1)), day=int(m.group(2))).isoformat()
        except ValueError:
            return ""
    return ""


# 最近动作失败环形日志：注入管家下轮上下文，让模型看见失败原因并自纠（C 层反馈回路）
_ACTION_FAIL_LOG = deque(maxlen=8)
_executed_turns = deque(maxlen=200)  # B5 幂等：记录最近执行的管家动作 clientTurnId，防重复执行
_action_idem_lock = threading.RLock()
# B5.1 持久化幂等/审计：内存去重在服务重启后会丢失，网络重试可能再次执行写动作。
# 只保存最近 200 条、且不写入对话正文；可用环境变量指定测试文件。
_ACTION_AUDIT_FILE = _dd_expanduser_default("DASH_ACTION_AUDIT_FILE", "butler_actions.json")
_ACTION_AUDIT_MAX = 200

# 通用写操作幂等账本：客户端可携带 clientMutationId，服务重启后仍能识别已完成请求。
# 只保存成功响应摘要，TTL 7 天；调用方不提供 key 时保持原有行为。
MUTATION_ROUTES = {
    "/api/tasks/create", "/api/tasks/update", "/api/tasks/archive", "/api/tasks/unarchive",
    "/api/tasks/delete", "/api/tasks/complete", "/api/roles/create", "/api/roles/sync",
    "/api/roles/delete", "/api/habits/checkin", "/api/habits/create", "/api/habits/update",
    "/api/habits/dimension", "/api/review/save", "/api/local-state", "/api/relations",
    "/api/relations/delete", "/api/relations/restore", "/api/relations/update",
    "/api/listening", "/api/listening/delete", "/api/listening/restore", "/api/listening/update",
    "/api/health/notes", "/api/health/notes/delete", "/api/week-plan", "/api/proactive",
    "/api/proactive/delete", "/api/proactive/restore", "/api/proactive/update", "/api/sync/conflicts/ack",
    "/api/butler/profile", "/api/butler/prefs/save", "/api/butler/history/clear", "/api/butler/undo",
    "/api/butler/act", "/api/recovery/restore", "/api/ai/oracle/refresh",
    "/api/review/draft/refresh", "/api/advisor/outcomes",
}

# Every POST route must be classified. Non-mutation routes are still explicit:
# they either read/stream, update only a derived read model, emit diagnostics,
# or invoke an operator-controlled deployment. This prevents a new endpoint
# from silently bypassing the write/readback policy.
POST_READ_ONLY_ROUTES = {
    "/api/advisor/fill", "/api/advisor/stream", "/api/ai/draft",
    "/api/ai/socratic", "/api/ai/socratic/compile", "/api/butler/chat",
    "/api/butler/stream", "/api/butler/vision", "/api/munger/advisor",
    "/api/munger/fill", "/api/review/draft",
}
POST_READ_MODEL_ROUTES = {"/api/shadow/reconcile"}
POST_DIAGNOSTIC_ROUTES = {"/api/kb-diag"}
POST_OPERATOR_ROUTES = {"/api/invalidate", "/api/sync-vps"}
POST_ROUTE_CLASSES = {
    **{route: "mutation" for route in MUTATION_ROUTES},
    **{route: "read-only" for route in POST_READ_ONLY_ROUTES},
    **{route: "read-model" for route in POST_READ_MODEL_ROUTES},
    **{route: "diagnostic" for route in POST_DIAGNOSTIC_ROUTES},
    **{route: "operator" for route in POST_OPERATOR_ROUTES},
}
_MUTATION_LEDGER_FILE = _dd_expanduser_default("DASH_MUTATION_LEDGER_FILE", "mutations.json")
_MUTATION_LEDGER_TTL = 7 * 86400
_MUTATION_LEDGER_MAX = 500
_mutation_runtime_lock = threading.RLock()
_active_mutations = set()

def _mutation_domain_source(route):
    if route.startswith("/api/tasks/"):
        return "tasks", "TickTick"
    if route.startswith("/api/habits/"):
        return "habits", "TickTick"
    if route.startswith("/api/roles/") or route.startswith("/api/review/"):
        return "roles", "Obsidian"
    if route.startswith("/api/health/"):
        return "health", "VPS JSON"
    if route.startswith("/api/butler/"):
        return "ai", "AI 动作审计"
    if route.startswith("/api/recovery/"):
        return "recovery", "本地快照"
    return "local", "VPS JSON"

def _result_entity_id(result, request_data=None):
    result = result if isinstance(result, dict) else {}
    request_data = request_data if isinstance(request_data, dict) else {}
    for key in ("task", "habit", "entry", "role"):
        value = result.get(key)
        if isinstance(value, dict) and value.get("id"):
            return str(value.get("id"))[:160]
    for value in (result.get("entityId"), result.get("id"), request_data.get("id"),
                  request_data.get("habitId"), request_data.get("clientTurnId")):
        if value:
            return str(value)[:160]
    return ""

def _standard_mutation_receipt(route, key, result, request_data=None):
    domain, source = _mutation_domain_source(route)
    verified = bool(result.get("verified", True)) if isinstance(result, dict) else False
    return {"route": route, "mutationId": key, "domain": domain, "source": source,
            "entityId": _result_entity_id(result, request_data), "verified": verified,
            "serverTime": datetime.now().isoformat(timespec="seconds"),
            "message": "已写入并回读确认" if verified else "已写入，尚未完成回读"}

def _recent_mutation_receipts(limit=40):
    rows = _read_json(_MUTATION_LEDGER_FILE, [])
    if not isinstance(rows, list):
        rows = []
    out = []
    for row in rows[-max(1, min(int(limit), 100)):]:
        if not isinstance(row, dict):
            continue
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        receipt = result.get("receipt") if isinstance(result.get("receipt"), dict) else {}
        domain, source = _mutation_domain_source(str(row.get("route") or ""))
        out.append({"route": row.get("route", ""), "mutationId": row.get("key", ""),
                    "ts": row.get("ts", 0), "domain": receipt.get("domain", domain),
                    "source": receipt.get("source", source), "entityId": receipt.get("entityId", ""),
                    "verified": receipt.get("verified", True), "duplicate": bool(result.get("duplicate"))})
    return {"success": True, "receipts": out, "count": len(out)}

def _find_mutation_result(route, key):
    if not route or not key:
        return None
    now = time.time()
    rows = _read_json(_MUTATION_LEDGER_FILE, [])
    if not isinstance(rows, list):
        return None
    kept = []
    for x in rows:
        if not isinstance(x, dict):
            continue
        try:
            fresh = now - float(x.get("ts", 0) or 0) < _MUTATION_LEDGER_TTL
        except (TypeError, ValueError):
            fresh = False
        if fresh:
            kept.append(x)
    if len(kept) != len(rows):
        _write_json(_MUTATION_LEDGER_FILE, kept[-_MUTATION_LEDGER_MAX:])
    for row in reversed(kept):
        if row.get("route") == route and row.get("key") == key:
            result = row.get("result")
            return dict(result) if isinstance(result, dict) else None
    return None

def _store_mutation_result(route, key, result):
    if not route or not key or not isinstance(result, dict):
        return
    rows = _read_json(_MUTATION_LEDGER_FILE, [])
    if not isinstance(rows, list):
        rows = []
    rows = [x for x in rows if not (isinstance(x, dict) and x.get("route") == route and x.get("key") == key)]
    safe = dict(result)
    safe.pop("raw", None)  # TickTick 原始响应可能很大，不进入幂等账本
    rows.append({"route": route, "key": key, "ts": time.time(), "result": safe})
    _write_json(_MUTATION_LEDGER_FILE, rows[-_MUTATION_LEDGER_MAX:])

def _load_action_audit():
    rows = _read_json(_ACTION_AUDIT_FILE, [])
    return rows if isinstance(rows, list) else []

def _audit_action(turn, kind, args, result, duplicate=False, status=None):
    """记录受 clientTurnId 保护的动作阶段，供排错/审计；失败也记录，便于判断是否重试。"""
    if not turn:
        return
    try:
        rows = _load_action_audit()
        rows.append({
            "clientTurnId": turn,
            "kind": kind,
            "args": _redact_action_args(args),
            "success": bool(isinstance(result, dict) and result.get("success")),
            "status": status or ("success" if isinstance(result, dict) and result.get("success") else "failure"),
            "duplicate": bool(duplicate),
            "ts": datetime.now().isoformat(timespec="seconds"),
            "error": (result.get("error") or "")[:240] if isinstance(result, dict) else "",
            "message": (result.get("msg") or result.get("warning") or "")[:240] if isinstance(result, dict) else "",
            "entityId": ((result.get("task") or result.get("habit") or result.get("entry") or {}).get("id")
                         if isinstance(result, dict) and isinstance((result.get("task") or result.get("habit") or result.get("entry")), dict) else ""),
            "undoable": bool(isinstance(result, dict) and result.get("undoable")),
            "undoed": bool(isinstance(result, dict) and result.get("undoed")),
        })
        _write_json(_ACTION_AUDIT_FILE, rows[-_ACTION_AUDIT_MAX:])
    except Exception as exc:
        print("[action-audit] write failed: %s" % str(exc)[:160], flush=True)

def _redact_action_args(args):
    """审计只保留可排错的短元数据，不把任务正文、图片或长回答写入磁盘。"""
    if not isinstance(args, dict):
        return {}
    hidden = {"content", "正文", "原文", "note", "备注", "answers", "回答", "image", "图片"}
    out = {}
    for key, value in list(args.items())[:24]:
        if str(key) in hidden:
            out[str(key)] = "[已隐藏]"
        elif isinstance(value, (dict, list)):
            out[str(key)] = "[结构化数据]"
        else:
            text = str(value)
            out[str(key)] = text[:120] + ("…" if len(text) > 120 else "")
    return out

def _find_action_audit(turn):
    if not turn:
        return None
    for row in reversed(_load_action_audit()):
        if isinstance(row, dict) and row.get("clientTurnId") == turn and row.get("success") and not row.get("duplicate"):
            return row
    return None

def _find_pending_action(turn):
    """返回最近仍处于 pending 的动作；完成/失败记录会覆盖旧 pending。"""
    if not turn:
        return None
    for row in reversed(_load_action_audit()):
        if isinstance(row, dict) and row.get("clientTurnId") == turn:
            return row if row.get("status") == "pending" else None
    return None


def _log_action_fail(kind, args, error):
    a = args if isinstance(args, dict) else {}
    brief = " ".join("%s=%s" % (k, str(a[k])[:24]) for k in list(a)[:4])
    _ACTION_FAIL_LOG.append("[%s] %s（%s）失败：%s" % (
        datetime.now().strftime("%m-%d %H:%M"), kind, brief or "无参数", str(error)[:80]))


def _butler_fail_text():
    if not _ACTION_FAIL_LOG:
        return ""
    lines = "\n".join("  " + x for x in list(_ACTION_FAIL_LOG)[-5:])
    return ("\n\n【最近动作失败——你之前发起的动作没存成功，本轮必须修正参数后重新发起，别原样重发】\n"
            + lines)


def _norm_action_args(kind, args):
    """把模型输出的自然语言键名/值归一为 butler_act 期望的标准键。
    不同模型（doubao/deepseek/glm）对参数键的命名习惯不同，这里统一兜底。"""
    if not isinstance(args, dict):
        return args
    a = dict(args)

    def _ren(src, dst, *candidates):
        for k in candidates:
            if a.get(k) is not None and str(a[k]).strip():
                a[dst] = a[k]
                if k != dst:
                    a.pop(k, None)
                return

    # 任务：所属项目→项目，截止时间/到期→截止
    if kind == "建任务":
        _ren("项目", "项目", "所属项目", "目标项目", "清单")
        _ren("象限", "象限", "象限名", "四象限")
        _ren("截止", "截止", "截止时间", "截止日期", "到期时间", "到期", "due")
        _ren("开始", "开始", "开始时间", "开始日期", "startDate", "起", "时段")
        _ren("优先级", "优先级", "priority", "重要程度")
        # 优先级若写的是象限描述（如"重要不紧急"）→ 补进象限；数字/高低的优先级必须保留，
        # 绝不能被象限吞掉（旧版 bug：优先级"高"被改名为象限后，真实优先级丢失）
        prio = str(a.get("优先级") or "").strip()
        if prio and not str(a.get("象限") or "").strip():
            q = _norm_quad(prio)
            if q:
                a["象限"] = q
        if str(a.get("象限") or "").strip():
            q = _norm_quad(a["象限"])
            if q:
                a["象限"] = q
        if a.get("项目"):
            a["项目"] = _norm_project(a["项目"]) or "📥 收集箱"
        if a.get("截止"):
            # 先试完整时间戳（今天8点/明天早上9点/周五下午3点等具体时刻），
            # 保留时间并在 __reminder__ 标记到点提醒；认不出才回落纯日期
            _dt, _rem = _parse_dt_full(a["截止"])
            if _dt:
                a["截止"] = _dt
                if _rem:
                    a["__reminder__"] = True
            else:
                due = _norm_due(a["截止"])
                if due:
                    a["截止"] = due
    elif kind == "建习惯":
        _ren("名称", "名称", "名字", "habit_name")
        _ren("维度", "维度", "分类", "类别")
        _ren("天数", "天数", "目标天数", "周期")
        if a.get("维度") or a.get("名称"):
            d = _norm_dimension(a.get("维度"), a.get("名称") or "")
            if d:
                a["维度"] = d
    elif kind == "记关系":
        _ren("对象", "对象", "谁", "人物", "relation_who")
        _ren("事由", "事由", "原因", "理由", "备注")
    elif kind == "定大石头":
        _ren("标题", "标题", "名称", "事情")
        _ren("理由", "理由", "为什么", "原因")
        _ren("项目", "项目", "所属项目", "清单")
        _ren("截止", "截止", "截止时间", "截止日期", "到期时间", "due")
        if a.get("项目"):
            a["项目"] = _norm_project(a["项目"]) or "📥 收集箱"
        if a.get("截止"):
            due = _norm_due(a["截止"])
            if due:
                a["截止"] = due
    elif kind == "写复盘":
        _ren("角色", "角色", "场景")
        _ren("内容", "内容", "正文", "text")
    elif kind == "删任务":
        _ren("标题", "标题", "名称", "关键词", "内容")
    elif kind == "改任务":
        _ren("标题", "标题", "原标题", "名称", "关键词")
        _ren("新标题", "新标题", "新名称", "改为", "改成", "标题")
    elif kind == "删角色":
        _ren("名称", "名称", "名字", "角色名")
    elif kind == "删关注":
        _ren("关键词", "关键词", "内容", "名字", "text")
    elif kind == "改关注":
        _ren("关键词", "关键词", "内容", "原内容", "text")
        _ren("新内容", "新内容", "内容", "改为", "改成", "new_text")
    return a


def _butler_is_vent(text):
    """纯倾诉安全网：用户只在宣泄情绪、没有任何执行意图时返回 True，用于抑制误触发动作。
    若同时出现明确执行意图信号词，则不算纯倾诉。"""
    if not text:
        return False
    if not _VENT_RE.search(text):
        return False
    if _ACTION_SIGNAL_RE.search(text):
        return False
    return True


_VENT_RE = re.compile(r"(好累|没动力|没劲|好烦|烦躁|焦虑|压力大|丧|emo|想躺|不知道怎么办|"
                      r"提不起|心累|疲惫|崩溃|想放弃|无力|很闷|压抑|委屈)")
_ACTION_SIGNAL_RE = re.compile(r"(帮我(建|创|记|定|写|删|改|加)|建个|建一|创建一个|记一笔|定个|"
                                r"写个|删掉|改一下|提醒我|安排|做个任务|加个习惯|存一下|"
                                r"这周就开始|从今天开始|马上做|现在就)")


def _names_similar(a, b):
    """去掉数字后比较两名字是否实质相同（防「每天喝水」误改「每天喝水8杯」）"""
    na = re.sub(r"\d+", "", (a or "")).strip()
    nb = re.sub(r"\d+", "", (b or "")).strip()
    return bool(na) and na == nb


def _extract_action(text):
    """从 LLM 回复里抽取 <action>类型</action>{JSON} 动作指令；返回 (清洗后的正文, [动作])"""
    if not text:
        return "", []
    actions = []
    pattern = re.compile(r"<action>\s*([^<>\n]+?)\s*</action>\s*(\{.*?\})?", re.S)
    for m in pattern.finditer(text):
        raw_kind = (m.group(1) or "").strip()
        # LLM 可能写成 "建习惯|记关系" 的枚举，取第一个真实动作
        kind_token = raw_kind.split("|")[0].strip()
        kind = ACTION_ALIASES.get(kind_token)
        if not kind:
            continue
        args = {}
        if m.group(2):
            try:
                args = json.loads(m.group(2))
            except Exception:
                # 容忍单引号 / 尾随逗号
                try:
                    fixed = re.sub(r",\s*([}\]])", r"\1", m.group(2).replace("'", '"'))
                    args = json.loads(fixed)
                except Exception:
                    args = {}
        args = _norm_action_args(kind, args)
        actions.append({"kind": kind, "args": args})
    clean = pattern.sub("", text).strip()
    return clean, actions


# 各动作类型必备的核心字段；缺失/为空视为模型幻觉或残缺动作，直接丢弃，不渲染确认卡
_ACTION_REQUIRED = {
    "建任务": ["标题"], "建习惯": ["名称"], "记关系": ["对象"], "定大石头": ["标题"],
    "删任务": ["标题"], "删关系": ["关键词"], "删倾听": ["关键词"],
    "改任务": ["标题"], "改习惯": ["名称"], "改关系": ["关键词"], "改倾听": ["关键词"],
    "删角色": ["名称"], "删关注": ["关键词"], "改关注": ["关键词", "新内容"],
    # 记偏好需要 key+value；写复盘 角色/内容 任一即可
    "记偏好": ["key", "value"],
}


def _action_complete(kind, args):
    """校验动作是否具备落地所需的核心字段；返回 False 表示残缺/幻觉，应丢弃"""
    req = _ACTION_REQUIRED.get(kind)
    if not req:
        if kind == "写复盘":
            return bool(str(args.get("角色") or "").strip() or str(args.get("内容") or "").strip())
        return True  # 未知类型不拦截

    def _field_ok(v):
        s = str(v or "").strip()
        if not s:
            return False
        # P2-B16: 只拦截「纯占位符模板」，不再因字段里含括号/问号就误杀合法内容。
        # 旧规则（含 "（"/"?" 即判残缺）会误杀「给妈妈买生日礼物（周日）」「写周报？」这类真实输入。
        if s in ("（此处填写）", "（此处）", "此处填写", "待定", "某人", "待确认",
                 "xxx", "XXX", "?", "？", "（此处填写内容）"):
            return False
        # 去掉括号内的注记（时间/备注）后仍为空 → 才算纯占位符残缺
        core = re.sub(r"[（(][^）)]*[）)]", "", s).strip()
        return bool(core)

    if kind == "写复盘":
        return _field_ok(args.get("角色")) or _field_ok(args.get("内容"))
    if kind == "记偏好":
        return _field_ok(args.get("key")) and _field_ok(args.get("value"))
    return all(_field_ok(args.get(f)) for f in req)


def _actions_from_toolcalls(tool_chunks):
    """把流式/非流式 tool_calls 转成 butler_act 兼容的 actions 列表；解析失败静默跳过

    兼容两种结构：
      - 非流式 OpenAI 原生：{id, type, function:{name, arguments}}
      - 流式自累积（_call_llm_stream 产物）：{name, arguments}  ← 无 function 包裹"""
    actions = []
    for tc in (tool_chunks or []):
        fn = tc.get("function") or {}
        raw = (fn.get("arguments") if isinstance(fn, dict) else None) or tc.get("arguments") or ""
        if not raw:
            continue
        try:
            fa = json.loads(raw)
        except Exception:
            m = re.search(r"\{.*\}", raw, re.S)
            if not m:
                continue
            try:
                fa = json.loads(m.group(0))
            except Exception:
                continue
        if not isinstance(fa, dict):
            continue
        kind = (fa.get("kind") or "").strip()
        args = fa.get("args") or {}
        if not kind or not isinstance(args, dict):
            continue
        # 归一化模型输出的自然语言键名（截止时间→截止、所属项目→项目、重要不紧急→q2 等）
        args = _norm_action_args(kind, args)
        if not _action_complete(kind, args):
            # 残缺动作（如纯聊天场景模型误发的空对象关系存款）：丢弃，不弹确认卡；记入失败日志供模型自纠
            _log_action_fail(kind, args, "字段残缺被丢弃（缺必填字段或含占位符）")
            continue
        # 建任务优先级归一为整数，确认卡显示干净（执行端 butler_act 也会再归一一次）
        if kind == "建任务" and "优先级" in args:
            args["优先级"] = _norm_priority(args["优先级"])
        actions.append({"kind": kind, "args": args})
    return actions


def _load_butler_memory():
    data = _read_json(BUTLER_MEMORY_FILE, [])
    return data if isinstance(data, list) else []


# ── 随行顾问 · 场景级短期记忆（每个场景独立文件，记住最近 2-3 轮，不跨场景串扰）──
ADVISOR_MEM_MAX_ROUNDS = 3
ADVISOR_OUTCOMES_FILE = _dd_expanduser_default("DASH_ADVISOR_OUTCOMES_FILE", "advisor_outcomes.json")
ADVISOR_OUTCOME_VERDICTS = {"useful", "actioned", "not_useful", "later"}

def _record_advisor_outcome(data):
    """只保存建议编号与结果标签，不保存用户快照、问题或 AI 正文。"""
    advice_id = str((data or {}).get("adviceId") or "").strip()
    scene = str((data or {}).get("scene") or "").strip()
    verdict = str((data or {}).get("verdict") or "").strip()
    if not re.fullmatch(r"adv-[a-f0-9]{20}", advice_id):
        return {"success": False, "error": "建议编号无效"}
    if scene not in ADVISOR_SCENES or verdict not in ADVISOR_OUTCOME_VERDICTS:
        return {"success": False, "error": "结果标签无效"}
    rows = _read_json(ADVISOR_OUTCOMES_FILE, [])
    if not isinstance(rows, list):
        rows = []
    now = datetime.now().isoformat(timespec="seconds")
    row = next((x for x in rows if isinstance(x, dict) and x.get("adviceId") == advice_id), None)
    if row is None:
        row = {"adviceId": advice_id, "scene": scene, "createdAt": now}
        rows.append(row)
    row.update({"verdict": verdict, "updatedAt": now})
    action_id = str((data or {}).get("actionId") or "").strip()[:80]
    if action_id:
        row["actionId"] = action_id
    if not _write_json(ADVISOR_OUTCOMES_FILE, rows[-500:]):
        return {"success": False, "error": "建议结果写入失败"}
    return {"success": True, "adviceId": advice_id, "verdict": verdict}

def _advisor_outcome_summary():
    rows = [x for x in _read_json(ADVISOR_OUTCOMES_FILE, []) if isinstance(x, dict)]
    counts = {key: sum(1 for x in rows if x.get("verdict") == key) for key in ADVISOR_OUTCOME_VERDICTS}
    evaluated = counts["useful"] + counts["actioned"] + counts["not_useful"]
    return {"success": True, "count": len(rows), "counts": counts,
            "usefulRate": round((counts["useful"] + counts["actioned"]) / evaluated, 3) if evaluated else None,
            "recent": [{k: x.get(k) for k in ("adviceId", "scene", "verdict", "createdAt", "updatedAt", "actionId")}
                       for x in rows[-20:]]}

def _advisor_mem_file(scene):
    return _dd("advisor_%s.json" % (re.sub(r"[^A-Za-z0-9_-]", "_", scene or "unknown")))

def _load_advisor_memory(scene):
    """读取某场景最近记忆轮次 [{role, content}]；读不到/损坏视为无记忆，不报错"""
    try:
        d = _read_json(_advisor_mem_file(scene), {})
        arr = d.get(scene, []) if isinstance(d, dict) else []
        return arr if isinstance(arr, list) else []
    except Exception:
        return []

def _advisor_memory_text(scene, limit=ADVISOR_MEM_MAX_ROUNDS):
    """把最近对话轮次拼成回顾文本（拼到 user_msg 前）"""
    arr = _load_advisor_memory(scene)
    if not arr:
        return ""
    lines = ["（近期本场景对话回顾）"]
    for m in arr[-limit * 2:]:
        role = "用户" if m.get("role") == "user" else "我"
        c = (m.get("content") or "").strip().replace("\n", " ")[:160]
        if c:
            lines.append(f"{role}：{c}")
    return "\n".join(lines) + "\n\n" if len(lines) > 1 else ""

def _append_advisor_memory(scene, user_q, assistant_a):
    """LLM 成功后把本轮 (user, assistant) 追加进场景文件，裁剪到最近 N 轮"""
    if not scene:
        return
    try:
        arr = _load_advisor_memory(scene)
        arr.append({"role": "user", "content": (user_q or "")[:500]})
        arr.append({"role": "assistant", "content": (assistant_a or "")[:600]})
        arr = arr[-ADVISOR_MEM_MAX_ROUNDS * 2:]
        _write_json(_advisor_mem_file(scene), {scene: arr})
    except Exception:
        pass


def _append_butler_memory(items):
    """被裁掉的旧对话由 LLM 提炼为长期记忆要点（异步执行，不阻塞返回）。
    提炼式而非原文堆砌：只留"值得长期记住的事"（新事实/决定/约定/未尽事项），
    每条带日期，去重后上限 60 条；LLM 失败回退原文截取（保底不丢）。"""
    items = [m for m in (items or []) if (m.get("content") or "").strip()]
    if not items:
        return
    threading.Thread(target=_butler_memory_digest, args=(items,), daemon=True).start()


_SENSITIVE_RE = re.compile(r"(密码|口令|token|api[ _-]?key|身份证|银行卡|验证码|手机号|住址|账号|密码)")

_mem_digest_lock = threading.Lock()
_last_mem_digest_ts = 0.0
_MEM_DIGEST_THROTTLE_S = 600  # 记忆提炼节流：最多每 10 分钟一次

def _butler_memory_digest(items):
    """后台线程：LLM 提炼被裁对话 → 合并入长期记忆；失败回退原文截取。
    入口节流：距上次提炼不足 10 分钟直接跳过（历史保留 40 条不会被冲掉，
    关键信息会在下一次提炼时沉淀），避免每轮对话都白跑一次 LLM"""
    global _last_mem_digest_ts
    with _mem_digest_lock:
        _now = time.time()
        if _now - _last_mem_digest_ts < _MEM_DIGEST_THROTTLE_S:
            return
        _last_mem_digest_ts = _now
    # P2-B14: 提炼失败不再原样回退「用户+管家各前80字」（可能沉淀敏感/噪音），
    # 改为只存用户消息、过滤明显敏感词、截短到 40 字。宁缺毋滥。
    fallback = []
    for m in items:
        if m.get("role") != "user":
            continue
        c = (m.get("content") or "").strip()
        if c and not _SENSITIVE_RE.search(c):
            fallback.append("用户：" + c[:40])
    try:
        convo = "\n".join(("用户：" if m.get("role") == "user" else "管家：")
                          + (m.get("content") or "")[:400] for m in items)
        prompt = ("从下面这段管家对话里，提炼出值得管家长期记住的要点（用户的新事实/偏好/决定/约定、"
                  "未完成的事、重要数字）。每条一句话（≤40字），格式严格为 JSON：{\"points\":[\"...\"]}。"
                  "只提炼真正值得跨会话记住的内容；没有就返回空数组。不要复述对话原文。\n\n对话：\n" + convo[:4000])
        r = _call_llm([{"role": "system", "content": "你只输出 JSON 对象 {\"points\":[...]}"},
                       {"role": "user", "content": prompt}],
                      json_mode=True, temperature=0.3, max_tokens=400, feature="memory_digest")
        import json as _json
        pts = _json.loads(r.get("content") or "{}").get("points") or []
        new_lines = [("[%s] " % date.today().isoformat()[5:] + str(p).strip()[:60])
                     for p in pts if isinstance(p, str) and p.strip()]
    except Exception:
        new_lines = []
    if not new_lines:
        new_lines = fallback
    if not new_lines:
        return
    # B6: load→modify→write 包进同一把锁，防两个 digest 线程并发后写覆盖丢行
    with _memory_lock:
        mem = _load_butler_memory()
        for line in new_lines:
            if line not in mem:
                mem.append(line)
        if mem:
            _write_json(BUTLER_MEMORY_FILE, mem[-60:])


def _butler_memory_text():
    mem = _load_butler_memory()
    if not mem:
        return ""
    # 记忆瘦身：过期的瞬时状态不注入提示词。
    try:
        import datetime as _dt
        _today = _dt.date.today()
        def _keep(x):
            m = re.match(r"\[(\d{2})-(\d{2})\]", x)
            if not m:
                return True
            try:
                d = _dt.date(_today.year, int(m.group(1)), int(m.group(2)))
                if (d - _today).days > 200:  # 跨年：比今天晚 200 天以上说明是去年
                    d = d.replace(year=_today.year - 1)
                return (_today - d).days <= 30
            except (ValueError, TypeError):
                return True
        mem = [x for x in mem if _keep(x)]
    except Exception:
        pass
    return "【长期记忆（过往对话沉淀的要点，带日期）】\n" + "\n".join("· " + x for x in mem) + "\n"


def _butler_offline_reply(h):
    """LLM 不可用时的规则引擎兜底回复（基于缺口分析 + 教练卡，不烧 token）"""
    try:
        cards = h.coach_cards().get("cards", [])
        gaps = h._butler_gap_analysis()
    except Exception:
        cards, gaps = [], []
    if _demo_enabled():
        lines = ["（演示模式：未配置 LLM。以下为内置规则引导；配置 LLM_API_KEY 后管家可结合数据对话）"]
    else:
        lines = ["（AI 推理暂时不可用，以下基于你的真实数据给出引导）"]
    src = gaps[:3] if gaps else [c.get("insight", "") for c in cards[:3]]
    for g in src:
        if g:
            lines.append("· " + g)
    if len(lines) == 1:
        lines.append("· 你的驾驶舱数据目前看起来挺完整，先保持节奏，有事随时叫我。")
    return "\n".join(lines), []


BUTLER_SYSTEM_PROMPT = prompt_header("butler") + (
    "你是「" + USER_NAME + "驾驶舱」的 AI 管家，也是这个人生操作系统的灵魂。\n"
    "你的核心使命不是回答问题，而是**基于用户的真实数据，主动带他一步步把驾驶舱跑起来**——"
    "从骨架到真正运转的人生操作系统。\n\n"
    "【人设】温柔但有力量、有主见的教练。你既懂他，也敢推他、敢点破矛盾。用中文，说人话，不端着。\n"
    "【教练模式自动路由】每条回复第一行先独占一行输出模式标注，格式严格为【当前模式：X】，X 按用户当轮情境四选一：\n"
    "  神谕——方向迷茫：想不清大方向、问「这样对吗」「往哪走」等长期性问题；\n"
    "  芒格——决策困境：在具体选项间纠结（A 还是 B、要不要做）、要判断和取舍；\n"
    "  教练卡——情绪卡点：累/焦虑/没动力/自我怀疑，情绪先于问题本身；\n"
    "  苏格拉底——思维误区：想当然、归因偏差、需要被提问点破的固有认知。\n"
    "  拿不准时标【当前模式：教练卡】。标注后正文照常按下方三层结构回复，不受影响。\n"
    "【回复结构】分三层，自然过渡，不要编号：\n"
    "  1. 共情/接住现状（1句）——先让他感觉被看见。\n"
    "  2. 给判断（2-3句）——必须引用【仪表盘数据】里的具体名字和数字，给出你的真实看法，敢下判断。\n"
    "  3. 推一步（1个具体动作）——一个马上能做的、落实到某个真实条目的动作，附带动作指令。\n"
    "  内容要充实完整：把判断、依据、建议讲透（一般 4-6 段；用户明确要极简或纯倾诉时才缩短）。不要空话套话、不要重复，但信息要厚实。\n\n"
    "【七习惯框架】每个建议落到一个习惯上：\n"
    "1 积极主动（关注圈→影响圈） 2 以终为始（角色使命→季度目标） 3 要事第一（四象限+大石头）\n"
    "4 双赢（关系账户） 5 知彼解己（倾听） 6 统合综效 7 不断更新（四维平衡+周复盘）\n\n"
    "【苏格拉底式引导——与七习惯结合】用户迷茫、想不清优先级或取舍（「我该不该做X」「先做哪个」「感觉乱」）时：\n"
    "先在第二层判断里用 1 个精准提问引导他自己想清楚，问句指向他的目标和代价，而不是替他下结论。例如：\n"
    "- 「这件事属于哪个习惯的范畴？它推的是哪个角色的 KR？」（用七习惯做判断坐标）\n"
    "- 「如果这周只做成一件，你选哪件？那另一件的代价你接受吗？」（要事第一）\n"
    "提问之后仍要给出你基于数据的判断——提问是引导，不是踢皮球。\n"
    "【三种情形的优先判定】① 用户纯倾诉情绪（无任何请求）→ 只共情+点破矛盾，绝不推动作（见下方红线）；\n"
    "② 用户明确要答案、要执行（「帮我建」「直接说」「删掉它」）→ 直接给判断/直接推动作，不提问；\n"
    "③ 用户迷茫、要取舍 → 苏格拉底式提问先行，判断跟上。\n"
    "判定顺序从①到③：先排除情绪倾诉，再看是否明确指令，剩下的才走提问引导。\n\n"
    "【数据驱动——这是你和「傻聊天机器人」的根本区别】\n"
    "系统消息里有一段【仪表盘数据】，是用户此刻的真实状态：角色与 KR、待办清单、习惯及打卡、本周大石头、关系/复盘。\n"
    "你必须：\n"
    "- 每条洞察都点名具体条目（如「『每日阅读』本周才 1/7，要掉了」「角色『身体』的 KR 还是空的」）。\n"
    "- 主动带练：用户没明确意图时，从【缺口】里挑当前最重要的一条，结合真实数据给出下一步，并直接推一个动作。\n"
    "- 点破矛盾：数据和行为不一致时直接说（如「你说要晨跑，但身体维度习惯本周 0/7——卡点在哪？」），但语气是并肩作战不是说教。\n"
    "- 改/删某条目时，从上面对应的名字里精确匹配；拿不准就列出候选让用户选，绝不造名字。\n"
    "- 缺口为空时转向「优化/取舍」：帮他排优先级、合并、砍掉，而不是继续填。\n\n"
    "【逐步引导优先级】① 角色有使命+KR → ② 角色有本周任务 → ③ 习惯归到四维且本周达标 → ④ 本周关系/倾听 → ⑤ 周复盘。\n"
    "用户说「帮我完善/看看还差什么」时，按缺口给出带名字的清单，让他选一个再推动作。\n\n"
    "【指导原则】\n"
    "- 引用具体数据，给可执行的判断（做什么/何时/怎么做），而不是正确的废话。\n"
    "- 信息不足先问 1 个澄清问题，别猜。\n"
    "- 结合用户偏好/记忆（如「上次你说想早起跑步」）做连续性建议。\n\n"
    "【诚实红线——最高优先级，任何情况不可违反】\n"
    "你没有任何绕过确认直接改数据的能力——你自己无法直接执行任何改动，也不会真的改任何数据。\n"
    "所有改动只能通过动作指令发起（二选一）：优先调用 do_action 工具（function calling）；\n"
    "若当轮未调用工具，则在回复末尾输出 <action>类型</action>{JSON} 文本标记。两条通道都会被解析成确认卡，用户点确认后才真正执行。\n"
    "因此：\n"
    "- 严禁完成时态描述操作：「我已经删了」「我刚清理了」全是谎言——你从未执行过。\n"
    "- 严禁虚构执行结果。用户说「还有残留」时，先在【仪表盘数据】里找到那条原样引用，再推对应动作；找不到就如实说「我的数据里没看到，你在哪个页面看到的？」。\n"
    "- 习惯不能靠动作删除（TickTick 限制），需用户在 App 手动删；如实告知，说「可以帮你发起改名/改目标」。\n"
    "- 宁可少说，不可说谎。用户在验证你说过的话是否兑现。\n\n"
    "【执行动作】用户明确要落地某操作时，调用 do_action 工具（一次可并行多个）。\n"
    "调用工具前先写 1-2 句正文（为什么推这个动作），别只发工具调用不出声，否则用户会看到空白气泡。\n"
    "【绝不误触发——这条优先级高于「点破矛盾」】用户只是倾诉情绪、没提出任何要做的请求时\n"
    "（「今天好累」「完全没动力」「好烦」「很焦虑」「不知道怎么办」），你只能共情 + 给方向\n"
    "（可以点破数据矛盾），但绝不调用工具、绝不输出 <action>、绝不编动作。\n"
    "示例：用户说「今天好累没动力」——你可以指出「身体维度连一个习惯都没有，难怪没劲」，\n"
    "但必须等用户说「那帮我建一个」才推动作。没有用户明确意图时，再想推也忍住。\n"
    "动作参数必须是用户真实给出的具体信息；拿不准的字段先问，别用「（某人不）」「（待定）」占位符填空。\n"
    "kind 枚举：建任务 / 建习惯 / 记关系 / 写复盘 / 定大石头 / 记偏好 / 删任务 / 删关系 / 删倾听 / 改任务 / 改习惯 / 改关系 / 改倾听 / 删角色 / 删关注 / 改关注 / 定时提醒 / 改约束。\n"
    "args 中文键：\n"
    "  建任务：{标题, 项目, 象限(q1-q4), 角色, 优先级(1-5或 高/中/低), 开始, 截止}\n"
    "    · 建任务别默认存 0 点：用户给了具体时刻（今天8点/明天9点）就写进 截止 或 开始；给了时段（明天9点到11点）用 开始=明天9点、截止=明天11点 存成时间段；只说了日期没给时刻 → 先主动问「几点做？还是全天？」再建。\n"
    "    · 项目必须精确使用这四个之一：🖥️ 工作安排、🏠 值得生活、📥 收集箱、四象限；拿不准就写 📥 收集箱，禁止自创项目名。\n"
        "    · 截止写 YYYY-MM-DD；也可写 明天/后天/周五/下周三/8月20日 这类相对日期（系统会按【现在】里的今天准确换算）。换算责任在你：\n"
    "       系统消息【现在】已给出今天是几月几日星期几。说「周五」= 本周五（若今天已是周五或已过，则顺延到下周周五）；「下周三」= 下周的周三；「8月20日」= 今年该日（若已过则明年）。\n"
    "       拿不准时写相对词（明天/周五/下周三）交给系统换算，**绝不自己硬编一个数字日期**——这是最常见的写错来源。\n"
    "  建习惯：{名称, 维度, 天数, 目标, 单位}\n"
    "    · 维度必须四选一：身体 / 精神 / 智力 / 社会情感（运动健身睡眠饮食→身体；读写学背→智力；冥想反思感恩→精神；联系陪伴沟通→社会情感）。拿不准宁可不传也别编。\n"
    "  记关系：{对象, 类型(存款|取款), 事由, 金额, 角色}\n"
    "  定大石头：{标题, 理由, 角色, 截止}（项目规则同建任务）\n"
    "  写复盘：{角色, 内容}  记偏好：{key, value}\n"
    "  删任务：{标题(模糊匹配)}  改任务：{标题, 新标题}  改习惯：{名称, 新名称, 目标}  改关系：{关键词, 新事由, 新额度}  改倾听：{关键词, 新对象/新感受/新复述/新第三选择}\n"
    "  删角色：{名称(角色的完整名字)}  删关注：{关键词(圈内条目文字), 圈(可选:关注圈/影响圈)}  改关注：{关键词(原条目文字), 新内容}\n"
    "  定时提醒：{内容, 时间(每天早上X点/每周X/每隔X小时)}  改约束：{内容(用户给管家的行为要求)}\n"
    "  重要：改习惯/改任务只改用户明确要改的字段——用户说「目标改成10杯」就只传 目标，不要动 名称、不要传 新名称；"
    "用户没说改名，就绝不传 新名称（否则会把习惯名改掉）。删/改时先从【仪表盘数据】里挑真实名字填 名称。\n"
    "  删角色会把角色文件移入 Obsidian 回收站，可手动找回，但删前务必向用户确认这是要删除的角色（引用名字）。\n"
    "没有动作时不要调用工具、也不要输出 action 文本，照常聊天即可。用户只看到你的正文，动作由前端渲染成确认卡片。\n"
    "【动作失败自纠】仪表盘数据里若出现「最近动作失败」区块，说明你之前的动作参数格式错了（项目名不合法/缺必填字段等）——"
    "本轮要用合法参数重新发起同一动作（换合法项目名、补齐字段），绝不原样重发。\n"
    "（文本通道格式示例：<action>建任务</action>{\"标题\":\"写周报\",\"项目\":\"📥收集箱\",\"象限\":\"q2\",\"角色\":\"\",\"截止\":\"\",\"优先级\":3}，每条独占一行；JSON 可在下一行。）"
)

# ═══════════════════════════════════════════════════════════════
# P1 · AI 原生化三件套：场景起草 / 教练卡 / 苏格拉底引导
# ═══════════════════════════════════════════════════════════════

AI_DRAFT_COMMON = prompt_header("draft") + (
    "你是「" + USER_NAME + "驾驶舱」的内嵌写作助手。规则：\n"
    "- 全部用中文；直接输出正文，不要 markdown 代码块包裹，不要标题符号堆砌。\n"
    "- 语气克制、具体、有画面感，不喊口号不鸡汤。\n"
    "- 只输出最终稿件本身，不要任何前言（如「好的，以下是…」）或后缀解释。\n"
)

def _draft_role(self, name):
    for r in (self._dash().get("roles") or []):
        if r.get("name") == name or r.get("id") == name:
            return r
    return None

def _draft_ctx_mission(self, role):
    r = _draft_role(self, role) or {}
    return (f"角色名：{r.get('name') or role}\n现有使命宣言：{r.get('mission') or '（空）'}\n"
            f"季度目标：{r.get('goal') or '（空）'}\n关键结果：{'；'.join(r.get('key_results') or []) or '（空）'}")

def _draft_ctx_quarter(self, role):
    r = _draft_role(self, role) or {}
    # P2-V11: 走路由缓存，避免每次起草直连外部 TickTick（与页面周数据同源）
    wk = cached("week", self._get_week_report)
    titles = "、".join(t[:20] for t in (wk.get("titles") or [])[:8]) or "无"
    return (f"角色名：{r.get('name') or role}\n使命宣言：{r.get('mission') or '（空）'}\n"
            f"本季度已完成任务：{titles}")

def _draft_ctx_kr(self, role):
    r = _draft_role(self, role) or {}
    tasks = self._dash().get("tasks") or []
    rn = r.get("name") or role
    n = sum(1 for t in tasks if rn in (t.get("content") or ""))
    return f"角色名：{rn}\n季度目标：{r.get('goal') or '（空）'}\n该角色当前关联未完成任务：{n} 项"

def _draft_ctx_review(self, role):
    r = _draft_role(self, role) or {}
    # P2-V11: 走路由缓存，避免每次起草直连外部 TickTick（与页面周数据同源）
    wk = cached("week", self._get_week_report)
    per_day = "、".join(f"{d}:{c}" for d, c in zip(wk.get("days") or [], wk.get("counts") or [])) or "无"
    titles = "、".join(t[:18] for t in (wk.get("titles") or [])[:10]) or "无"
    habits = self._habits().get("habits", [])
    hline = "；".join(f"{h['name']}本周{h.get('weekChecked',0)}/7" for h in habits[:6]) or "无"
    feedback = _advisor_outcome_summary()
    fline = "；".join(f"{k}:{v}" for k, v in (feedback.get("counts") or {}).items() if v) or "暂无"
    return (f"角色名：{r.get('name') or role}\n本周每日完成数：{per_day}\n"
            f"本周完成事项：{titles}\n习惯周打卡：{hline}\n上周复盘：{r.get('review') or '（空）'}\n"
            f"随行顾问反馈（本周）：{fline}")

def _draft_ctx_listening(self, _role):
    ls = _read_json(LISTENING_FILE, []) or []
    recent = "；".join(((x.get("who") or "") + ":" + (x.get("feeling") or ""))[:30] for x in ls[-5:]) or "无"
    return f"最近5条倾听笔记：{recent}"

def _draft_ctx_proactive(self, _role):
    pro = _read_json(PROACTIVE_FILE, {}) or {}
    cs = pro.get("concerns") or []
    cline = "；".join((c.get("content") or c.get("text") or str(c))[:30] for c in cs[:8]) or "无"
    return f"当前关注圈条目：{cline}"

# P2-V5: 已删 _draft_ctx_oracle 与 AI_DRAFT_SCENES["oracle"]（前端无 scene:'oracle' 入口，
# 且若触发会把整个 _butler_context 塞进 max_tokens=350 的一句话神谕，与 /api/ai/oracle 重复且最贵）。

AI_DRAFT_SCENES = {
    "mission": {
        "max_tokens": 500,
        "ctx": _draft_ctx_mission,
        "prompt": AI_DRAFT_COMMON +
            "任务：为下面这个人生角色起草一段使命宣言（80-150字，第一人称）。\n"
            "要求：有画面感（这个角色做到了会是什么样子）；立足于现有目标与关键结果，不虚构；\n"
            "结尾不要句号堆叠的排比口号。只输出宣言正文。\n\n【角色数据】\n{ctx}\n"
            "用户补充想法：{input}",
    },
    "quarter": {
        "max_tokens": 700,
        "ctx": _draft_ctx_quarter,
        "prompt": AI_DRAFT_COMMON +
            "任务：为下面这个角色起草本季度目标：1 个总目标 + 3 个可衡量子目标。\n"
            "格式：第一行总目标（一句话）；随后 3 行子目标，每行以「- 」开头，带数字口径（次数/时长/产出物）。\n"
            "只输出这 4 行。\n\n【角色数据】\n{ctx}\n用户补充想法：{input}",
    },
    "kr": {
        "max_tokens": 600,
        "ctx": _draft_ctx_kr,
        "prompt": AI_DRAFT_COMMON +
            "任务：为下面这个角色的季度目标起草 3-5 条关键结果(KR)。\n"
            "每行一条，以「- 」开头，包含：动词 + 可量化结果 + 时间范围（如「- 读完《xxx》前3章并输出9条笔记，Q3内」）。\n"
            "只输出 KR 列表。\n\n【角色数据】\n{ctx}\n用户补充想法：{input}",
    },
    "weekly_review": {
        "max_tokens": 900,
        "ctx": _draft_ctx_review,
        "prompt": AI_DRAFT_COMMON +
            "任务：基于本周真实数据，为角色起草一份周复盘草稿，按 4F 结构：\n"
            "事实(Facts)：本周实际完成了什么（引用数据）。\n"
            "感受(Feelings)：哪个瞬间有成就感/哪里疲惫。\n"
            "发现(Findings)：数据与预期的差距说明了什么。\n"
            "下一步(Future)：下周只改一件事，改什么。\n"
            "每部分 2-3 句，小标题用「事实：」「感受：」「发现：」「下一步：」开头，不加重号。\n"
            "只输出四段正文。\n\n【本周数据】\n{ctx}\n用户补充想法：{input}",
    },
    "listening": {
        "max_tokens": 500,
        "ctx": _draft_ctx_listening,
        "prompt": AI_DRAFT_COMMON +
            "任务：把用户的倾听碎片整理成结构化笔记，四段式：\n"
            "对方说了什么：\n对方真正的需求：\n我的回应：\n下次可以做的行动：\n"
            "每段 1-2 句。只输出四段。\n\n【背景】\n{ctx}\n用户原始记录：{input}",
    },
    "proactive_note": {
        "max_tokens": 600,
        "ctx": _draft_ctx_proactive,
        "prompt": AI_DRAFT_COMMON +
            "任务：对关注圈里的每条焦虑做「影响圈转化」分析。\n"
            "每条格式：\n「条目」→ 可控部分：… / 不可控部分：… / 最小行动：…（一句、24小时内可做）\n"
            "最多处理 5 条。只输出列表。\n\n【关注圈】\n{ctx}\n用户补充想法：{input}",
    },
}

# ── ✦ 随行顾问 · 每块细分领域理解 ──
# 健康看板板块聚焦表：✦ 按钮 openAdvisor('health', key) → (板块名, 聚焦解读指令)
HEALTH_FOCUS = {
    "cbti": ("睡眠调节·CBT-I滴定",
             "重点解读睡眠债规模与还法、睡眠效率决定收紧还是放宽、滴定熄灯时刻的推导逻辑，"
             "以及从滴定点到目标相位的每周前移节奏；回复里必须写出今晚熄灯的具体时刻（如「今晚 01:22 熄灯」）"
             "和一个今晚就执行的动作。"
             "医学口径：成人推荐 7-9h/晚；睡眠效率<85% 收紧在床窗口、≥90% 才放宽；固定起床时间比早睡更有效。"),
    "readiness": ("今日恢复度",
                  "重点解读今日恢复度总分与各因子得分，指出最拖后腿的弱项因子，"
                  "并据此明确今天的强度建议（轻量化/正常/可上强度）。"
                  "医学口径：≥85 可上强度 / 70-84 正常练 / <70 降强度或主动恢复（Google/Oura 通用阈值）；"
                  "HRV/静息心率/手腕温度是「身体响应」主角，睡眠/活动只作上下文——避免双重惩罚。"),
    "sleep": ("睡眠构成",
              "重点解读昨晚与近7晚的深睡、REM、核心睡眠占比与总时长；深睡管身体修复、REM管情绪记忆，"
              "指出结构问题（如深睡充足但总长不够）并给今晚的改善动作。"
              "医学口径：成人参考 深睡≈15-25%(前半夜为主)、REM≈20-25%(后半夜为主)、浅睡≈50%；"
              "深睡长期<10% 提示修复不足，REM 突增常是睡眠债恢复信号。"),
    "recovery": ("恢复组·HRV与静息心率",
                 "重点解读 HRV 与静息心率相对个人基线的偏离和连日趋势——HRV 连续低于基线=自主神经没缓过来，"
                 "静息心率高于基线=身体还在踩油门；HRV 和静息心率两个都要提到，判断当前是过载、正常还是恢复中，给回升路径。"
                 "医学口径：成人 HRV(rMSSD) 20-70ms 个体差异极大，「好HRV」=相对自己 14-60 天基线稳定而非越高越好；"
                 "静息心率连续 3-5 天较基线 +5~10bpm=身体硬扛恢复或生病前兆，应降训练强度。"),
    "heart": ("心率区间",
              "重点解读平均心率的日常水平与逐日波动；结合静息心率趋势判断心肺状态，异常波动时提醒可能的原因（睡眠债、咖啡因、压力）。"),
    "bedtime": ("睡眠规律·就寝/起床",
                "重点解读就寝/起床相位的一致性与漂移（工作日 vs 周末的社交时差）、就寝规律的得分构成，"
                "以及为什么固定起床时间是锚点、相位该怎么每周前移 15-20 分钟。"),
    "energy": ("活动能量",
               "重点解读日均活动消耗与步数水平；区分运动消耗与 NEAT（日常碎动），"
               "久坐日给出不依赖整块时间的消耗补法（饭后走 10 分钟、站着开会等）。"),
    "exercise": ("锻炼与正念",
                 "重点解读锻炼分钟与正念分钟是否达到养鹅标准（锻炼≠步数，要连续中高强度），"
                 "结合恢复度给本周运动安排（哪天上强度、哪天主动恢复），正念给最小起步量。"),
    "resp": ("呼吸·呼吸率",
             "重点解读呼吸率趋势与偏快程度（≥14次/分=副交感长期缺席），"
             "讲清共振呼吸（吸4呼6≈6次/分）和 4-7-8（吸4屏7呼8）各适合什么场景、怎么练、练多久见效。"
             "医学口径：成人静息呼吸率 12-20 次/分、睡眠中更低；持续偏快+晨起口干/打鼾提示留意睡眠呼吸暂停"
             "（AHI<5 正常 / 5-15 轻 / 15-30 中 / >30 重；夜间血氧反复<90% 也提示）。"),
    "rides": ("骑行记录",
              "重点解读骑行里程、时长、配速的负荷水平与频率；判断是否过量（结合恢复度和睡眠债），"
              "给骑行日与恢复日的交替安排建议。"
              "医学口径：80/20 极化训练——80% 里程在 Z1-Z2 打有氧基础，20% 在 Z4-Z5 上强度，少在 Z3 舒适区；"
              "周末长骑后次日晨间 HRV 回到基线且静息心率没涨=恢复好可正常练，HRV 连续 2-3 天走低=减量；"
              "心率区间用储备心率法 Karvonen 个人化（Z2 有氧 60-70% / Z4 阈值 80-90% / Z5 VO2max 90-100%）。"),
    # HEALTH-ABC-C2: 新增板块聚焦——手腕温度 / 站立·身体负荷（前端 ✦ 按钮已接）
    "wrist": ("手腕温度",
              "重点解读手腕温度相对个人基线（近14天均值）的偏离——偏离 ≥0.3℃ 提示身体应激（生病前兆/酒精/深夜大餐/高强度训练），"
              "≥0.5℃ 且伴 HRV 下降、静息心率升高多为生病或过度疲劳早期信号，早于症状出现；"
              "给观察点（结合 HRV/静息心率/睡眠判断是否真生病）与就医指征（持续偏离+发热乏力）。"),
    "activity": ("站立·身体负荷",
                 "重点解读站立小时（Apple Fitness 蓝环，日目标 ≥6-8h 抵消久坐）与身体负荷（Apple 当日运动强度指标）水平；"
                 "久坐日给出打破坐姿的具体动作（每 30-45 分钟起身 2 分钟、站立会议、饭后走动），"
                 "负荷异常（连续走高但恢复度走低）提示过度训练风险。"),
}

ADVISOR_COMMON = prompt_header("advisor") + (
    "你是「" + USER_NAME + "驾驶舱」的随行顾问——在每个工作区块里给出精准、可落地的建议。通用规则：\n"
    "- 全部用中文。语气克制、具体、有画面感，不喊口号不鸡汤；像老朋友一样直给，不绕弯子。\n"
    "- 建议必须脚踏实地：优先引用【本块当前数据】里的具体条目；给到的下一步要是今天/这周就能做的最小动作。\n"
    "- 不要复述用户全部数据，只挑真正要紧、可操作的点。\n"
    "- 不要用「你可以…」「不妨…」来回铺垫，直接说「做 X / 先做 X / 砍掉 X」。\n"
    "- 输出 5-8 行以内、短句分行；可用「▪ 」引导要点，不用 markdown 代码块包裹。\n"
    "- 不要任何前言（如「好的，以下是…」）或结尾寒暄。\n"
    "- 事实边界：只能把【本块当前数据】中明确出现的内容写成事实；基于模式的判断必须标为「推断」，下一步必须标为「建议」。缺数据时直说「暂无证据」，严禁补数值或假装确定。\n"
)

# 统一内核：七习惯 + 苏格拉底式提问。
# ⚠️ 只用于 advisor_stream（解读建议），严禁拼进 advisor_fill——填充模式要求严格 JSON，
# 「优先提问引导」会和「直接生成、不要任何解说」正面冲突，污染全部 9 个场景的填充输出。
ADVISOR_KERNEL = (
    "【系统内核 · 七习惯 + 苏格拉底】\n"
    "本驾驶舱基于《高效能人士的七个习惯》：1 积极主动（关注圈→影响圈） 2 以终为始（角色使命→季度KR）\n"
    "3 要事第一（四象限+每周大石头） 4 双赢思维（情感账户） 5 知彼解己（倾听）\n"
    "6 统合综效（第三选择） 7 不断更新（四维平衡+周复盘）。\n"
    "给建议时用七习惯做坐标：先判断这块数据卡在哪个习惯上，再从该习惯的视角给解法（可以点出习惯名，但别每条都贴标签）。\n"
    "用户卡在取舍/迷茫（「该不该做X」「先做哪个」）时，先用 1 个精准的苏格拉底式提问引导他自己想清\n"
    "（问他的目标或代价，如「这周只做成一件，你选哪件？」），随后仍要给出你基于数据的判断。\n"
    "注意：提问至多 1 个，是建议的引子不是替代——每轮回复里仍必须保留 2-3 条可直接执行的建议。\n"
)

# ═══ 随行顾问能力：健康框架 + 面板内创建类动作 ═══
# 健康专业框架：源自 WHOOP/Oura/Bevel 恢复度模型与 HRV 循证综述的调研提炼
# （三句式解读 / 多指标组合 / 恢复三档 / RHR 梯度 / 睡眠债分级 / 训练联动 / 就医指征）。
ADVISOR_HEALTH_KERNEL = (
    "【健康解读专业框架（输出保持克制短句，内容按此组织）】\n"
    "1. 三句式解读关键指标：当前值 → 与个人14天基线/近5天趋势的偏离 → 生理上意味着什么。\n"
    "2. 多指标组合优先于单指标罗列：HRV下降+静息心率升高+睡眠债高=恢复不足；手腕温度偏高+HRV下降+静息心率升高=疑似生病早期或应激；单一指标异常没有其他信号佐证时，直说「不足以单独下结论」。\n"
    "3. 恢复度建议三档：≥85 可上强度 / 70-84 正常练 / <70 降强度或主动恢复；必须点名最拖后腿的因子，不只报总分。\n"
    "4. 静息心率梯度：较基线 +5~8bpm=疲劳或压力信号；持续 +10bpm 以上=身体硬扛或生病前兆，应安排恢复。\n"
    "5. HRV 以个人基线为锚：较基线 -10~-15%=恢复压力增加；-20% 以上=明确恢复不足；HRV 日间波动大本身就是生活/训练不规律的信号。\n"
    "6. 睡眠债分级动作：<2h 维持规律；2-5h 连续2-3天提前20-30分钟上床；>5h 今天降强度优先补睡，别指望周末一次还清。\n"
    "7. 训练联动：连续2-3天 HRV 走低→主动减量；骑行次日 HRV 回基线且静息心率未升=恢复良好可正常练；每3-6周可安排一个减量周（训练量-30~50%）。\n"
    "8. 就医指征仅在信号出现时提：持续发热、手腕温度持续偏高伴乏力、静息心率持续+10bpm、夜间血氧反复<90%、长期睡眠<6h且白天嗜睡。\n"
    "9. 建议分三层收尾：今天做什么（带具体时刻/分钟）→ 本周怎么排 → 什么情况要警惕。每条带数字依据，不空喊。\n"
    "10. 证据边界：看到【未观测指标】时，先明确是 HAE 未推送、原始数据在窗口外还是驾驶舱解析缺口；缺失项只能说‘暂无证据’，严禁用常模、旧对话或猜测补齐数值。\n"
)

# 各面板可发起的动作白名单：仅创建类（调研结论：窄上下文顾问不开删改，避免误伤数据）。
ADVISOR_ACTIONS = {
    "rocks": [("定大石头", "{标题, 理由, 角色, 截止}"), ("建任务", "{标题, 项目, 象限(q1-q4), 优先级, 截止}")],
    "today": [("建任务", "{标题, 项目(🖥️ 工作安排|🏠 值得生活|📥 收集箱), 象限, 优先级, 截止}")],
    "actions": [("建任务", "{标题, 项目, 象限(q1-q4), 优先级, 截止}")],
    "habits": [("建习惯", "{名称, 维度(身体|精神|智力|社会情感), 角色, 目标, 单位}")],
    "proactive": [("建任务", "{标题, 项目(📥 收集箱), 象限, 截止}")],
    "relations": [("记关系", "{对象, 类型(存款|取款), 事由, 金额}")],
    "listening": [("建任务", "{标题, 项目, 截止}")],
    "role": [("定大石头", "{标题, 理由, 角色, 截止}"), ("建任务", "{标题, 角色, 截止}")],
    "review": [("写复盘", "{角色, 内容}")],
    "health": [("建任务", "{标题, 项目(🏠 值得生活), 截止}"), ("定时提醒", "{内容, 时间(HH:MM)}")],
}

def _build_advisor_tools(kind_hints):
    """随行顾问的 do_action 工具（比管家窄：只开本面板的创建类动作）。
    调研结论：窄上下文 agent 只给创建类 + 前端确认卡 + 一次最多2条，删改交给管家。"""
    return [{
        "type": "function",
        "function": {
            "name": "do_action",
            "description": "当用户在本面板明确要落地一个动作（%s）时调用；用户只是提问、要建议、倾诉时绝不调用；一次最多 2 个。" % "、".join(k for k, _ in kind_hints),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": [k for k, _ in kind_hints], "description": "动作类型（仅限本面板白名单）"},
                    "args": {"type": "object", "description": "动作参数（中文键）。可用动作：\n" + "\n".join("· %s %s" % (k, h) for k, h in kind_hints)},
                },
                "required": ["kind", "args"],
            },
        },
    }]

def _advisor_action_protocol(kind_hints):
    """随行顾问动作协议（v55）：照搬管家已验证的配方——诚实红线 + 具体 JSON 示例 +
    工具/文本标记双通道 + args 必须是 JSON 对象（DeepSeek 易传空串，示例压制）。"""
    lines = "\n".join("· %s %s" % (k, h) for k, h in kind_hints)
    return (
        "【本面板动作协议（与管家同一套确认机制，最高优先级遵守）】\n"
        "你没有任何直接执行能力，改不了任何数据。用户明确要落地某个操作时（一次最多 2 个，"
        "仅限本面板创建类动作），调用 do_action 工具，args 必须是 JSON 对象、键值来自用户给的真实信息：\n"
        "调用工具前先写 1-2 句正文（为什么推这个动作），别只发工具不出声，否则用户会看到空白面板。\n"
        '调用示例：{"kind":"建任务","args":{"标题":"今晚23:00熄灯·恢复优先","项目":"🏠 值得生活","截止":"今晚"}}\n'
        "若未调用工具，则在回复末尾输出文本标记（每条独占一行），两条通道都会变成确认卡，用户点确认后才真正执行。\n"
        "文本通道示例：<action>建任务</action>{\"标题\":\"写周报\",\"项目\":\"📥 收集箱\",\"象限\":\"q2\",\"截止\":\"明天\"}\n"
        "【反偷懒——最高优先级】用户明确要落地时，你必须真的发出其中一条通道：调了工具、或正文里有 <action> 标记，"
        "二者必有其一。只在嘴上说「已为你准备好确认卡」而两条通道都空白 = 等于什么都没做，用户只会看到空面板。\n"
        "【诚实红线】严禁完成时态谎报：「已建好」「已存进」「已完成」全是谎言——你从未执行过任何操作。"
        "发起了动作就说「已为你准备好确认卡」；没发起就别提执行结果。\n"
        "用户只是提问、要建议、倾诉时，绝不调用工具、绝不输出 <action>。\n"
        "本面板可用动作（仅限这些）：\n" + lines + "\n"
    )

ADVISOR_SCENES = {
    "rocks": {
        "title": "本周大石头", "icon": "🪨", "max_tokens": 400,
        "domain": "这是「本周大石头」：用户每周只挑 1-3 件真正推动人生的事，最需要做减法、排优先级。\n"
                  "看数据要判断：哪些石头短期能落地、哪块其实该砍掉、石头之间是否冲突、有没有更值得做却被漏掉的。",
        "suggest": "基于下方大石头清单给 2-3 条推进建议：本周先动哪一块、哪块建议合并或砍掉、哪块缺截止时间。",
        "quick": ["本周先推哪一块？", "哪块其实可以砍掉？", "哪块缺了落地条件？"],
        "fill": {"common": "\n\n（现在是「填充模式」：用户要的是你把本周大石头直接填出来，不是给建议。）",
                 "prompt": "直接生成 {count} 条本周大石头，让用户这一周有真实可推的事。输出一个严格 JSON 数组，每条：{title(一件具体、可实现、这一周做完成就感强的事), why(一句话，说明它为什么真正推动人生), role(可空字符串，若有角色上下文就从上面角色里挑最贴的)}。不要任何解说，只输出 JSON 数组。"},
    },
    "today": {
        "title": "今日焦点", "icon": "🎯", "max_tokens": 400,
        "domain": "这是「今日焦点」：把最重要的行动放进今天。要看：任务是否过多、是否贪多、有没有被忽略的「要大石头」任务、\n"
                  "结合今日已完成数、待办、逾期与习惯打卡率，判断今天是否失衡、该砍掉哪些、把注意力摁在哪一件上。",
        "suggest": "基于今日焦点、待办、逾期和习惯打卡率，给 2-3 条今天的具体执行建议：今天只该扑在哪 1-2 件事上、先砍掉哪件。",
        "quick": ["今天我只该做哪一件事？", "哪件我今天可以放一放？", "我的逾期该怎么处理？"],
        "fill": {"common": "\n\n（现在是「填充模式」：用户要的是把今天真正值得做的行动直接填出来，不是给建议。）",
                 "prompt": "基于上方今日焦点的待办/逾期/习惯数据，直接生成 {count} 条「今天最该做的事」。输出严格 JSON 对象，外层用 {\"items\": [...]} 包裹数组，每条：{title(一件今天具体可做的行动，动词开头、关键词在 6-16 字内、能做完成就感强), why(一句话：为什么是今天/投入产出比), role(可空字符串，若有角色上下文就挑最贴的角色名)}。优先从【本块当前数据】里已有的高优先/逾期任务里挑，但标题要改成今天就能执行的口吻；不要生成鸡汤。不要任何解说，只输出 JSON 数组。"},
    },
    "actions": {
        "title": "行动指挥台", "icon": "📋", "max_tokens": 420,
        "domain": "这是「行动四象限」：要事第一。要看各象限分布是否健康（是否第四象限太多、第二象限太少）、\n"
                  "有没有该归档/删掉的任务、有没有该分解的大任务、拖延的逾期任务重新怎么排队。",
        "suggest": "基于四象限任务清单，给 2-3 条整理建议：哪几件可以归档或删掉、哪件要拆小、第二象限该补充什么。",
        "quick": ["我的时间都花对了地方吗？", "哪些任务可以果断删掉？", "哪件大事该拆成小步？"],
        "fill": {"common": "\n\n（现在是「填充模式」：用户要的是把行动清单里真正该补的任务直接填出来，不是给建议。）",
                 "prompt": "基于上方行动四象限的现有任务，找出缺口，直接生成 {count} 条「现在最该补进清单的行动」。输出严格 JSON 对象，外层用 {\"items\": [...]} 包裹数组，每条：{title(一件具体行动，动词开头、关键词在 6-16 字内), why(一句话：为什么补这条/补上后补了哪个缺口), quad(固定填\"q2\"), role(可空字符串，若有角色上下文就挑最贴的角色名)}。优先补第二象限（重要不紧急）——如果现有 q2 任务太少，优先生成能长期复利的行动；不要和现有任务重复。不要任何解说，只输出 JSON 数组。"},
    },
    "habits": {
        "title": "习惯", "icon": "🌱", "max_tokens": 380,
        "domain": "这是「习惯系统·不断更新」：习惯是复利。要看哪些习惯该加码、哪几个是假坚持（连坐打卡却无成果）、\n"
                  "四维（身体/心智/情感/精神）是否平衡、单点习惯是否太多、有没有缺的最小的那一枚。",
        "suggest": "基于习惯打卡情况，给 2-3 条习惯经营建议：哪段连坐最值得加码、哪维失衡、是否需要砍掉一个低价值习惯。",
        "quick": ["我哪个习惯最有复利？", "四维里是不是偏科了？", "要不要砍掉一个习惯？"],
        "fill": {"common": "\n\n（现在是「填充模式」：用户要的是把该新建的习惯直接生成出来，不是给建议。）",
                 "prompt": "基于上方习惯清单和四维分布，找出最薄弱的维度，直接生成 {count} 条「值得新建的习惯」。输出严格 JSON 对象，外层用 {\"items\": [...]} 包裹数组，每条：{name(习惯名，简洁有力，6-14 字内), why(一句话：为什么是它/它补哪个维度), dimension(从 身体/精神/智力/社会情感 四选一，优先挑四维里最失衡的), goal(数字目标值，默认 1), unit(单位，默认 \"次\")}。避免和现有习惯重复；优先最小可坚持的习惯。不要任何解说，只输出 JSON 数组。"},
    },
    "proactive": {
        "title": "积极主动 · 影响圈", "icon": "💡", "max_tokens": 400,
        "domain": "这是「积极主动·影响圈」：把注意力从关注圈挪到影响圈。看：关注圈里哪条最值得做「影响圈转化」、\n"
                  "影响圈里哪步最小最可立即执行、今天的「选择」记录是否真的做到了主动。",
        "suggest": "基于关注圈/影响圈条目，给 2-3 条转化建议：最该拆的一条焦虑、它可控与不可控的部分、现在就能做的最小一步。",
        "quick": ["哪条焦虑最该转化成行动？", "我现在能做的最小一步是什么？", "今天的我选得主动吗？"],
        "fill": {"common": "\n\n（现在是「填充模式」：用户要的是把关注圈里的焦虑，直接变成能执行的行动任务，不是给建议。）",
                 "prompt": "把上方关注圈（concerns）里最值得转化的焦虑，拆成 {count} 条用户现在就能执行的最小行动。输出严格 JSON 对象，外层用 {\"items\": [...]} 包裹数组，每条：{title(一个可立即执行的具体行动标题，动词开头、关键词在 6-14 字内), from(它转化自哪条焦虑；若库里没有焦虑，则填「」)}。不要任何解说，只输出 JSON 数组。"},
    },
    "relations": {
        "title": "情感账户", "icon": "🏦", "max_tokens": 400,
        "domain": "这是「情感账户」：关系是存款/取款。要看谁的账户透支了（取款多于存款）、谁的关系该立刻补存款、\n"
                  "哪笔取款最近、账户里是否总在取款而从不往里存。",
        "suggest": "基于各人的存款/取款余额，给 2-3 条关系经营建议：最需要加码补存的是谁、对谁该先道歉/修账、本周该怎么分配精力。",
        "quick": ["谁的账户要亮红灯了？", "我该怎么补这笔存款？", "关系里我是不是只取不存？"],
        "fill": {"common": "\n\n（现在是「填充模式」：用户要的是把该记的一笔笔情感账直接拟好，不是给建议。）",
                 "prompt": "基于上方情感账户的余额和最近记录，找出最需要补存款的人，直接生成 {count} 条「可执行的情感存款/修账」。输出严格 JSON 对象，外层用 {\"items\": [...]} 包裹数组，每条：{who(对象名，必须从上面 people 里挑；若为空则该条不生成并在 items 里补一个 need_people:true 标记，提示先创建对象，绝不编造一个人名), type(固定填\"存款\"), reason(一句话具体事由：为什么存这笔，要有真实感，不要空泛), amount(数字 10-100), title(同 reason 的一句话版，给用户预览)}。优先写给余额透支或取款最多的对象；事由要具体到能直接照着做。不要任何解说，只输出 JSON 数组。"},
    },
    "listening": {
        "title": "倾听笔记", "icon": "👂", "max_tokens": 380,
        "domain": "这是「倾听笔记·知彼解己」：先理解再求被理解。看：笔记里是否只是「记录事实」而没抓「对方真正的感受」、\n"
                  "是否有很多「我认为」而少「他在意」、有没有可转化为下次行动的关键对话。",
        "suggest": "基于倾听笔记，给 2-3 条理解与行动建议：指出哪条最可能没听懂对方、对方的真实需求可能是什么、下次怎么复述。",
        "quick": ["我是不是没真听懂对方？", "他真正的需求是什么？", "这次对话该怎么跟进？"],
        "fill": {"common": "\n\n（现在是「填充模式」：用户要的是把一次倾听/一段关键对话的笔记直接写好，不是给建议。）",
                 "prompt": "基于上方倾听笔记风格和最近记录，直接生成 {count} 条「高质量的倾听笔记草稿」。输出严格 JSON 对象，外层用 {\"items\": [...]} 包裹数组，每条：{who(对方称呼), feeling(对方真正的感受，2-3 句，抓情绪不抓事实), restate(我对他话的复述，体现我理解了他), third(第三选择：我们共同找到的第三条路/下一步), title(一句话：这次倾听的核心，给用户预览)}。语气要真诚、具体，能直接照着存入。不要任何解说，只输出 JSON 数组。"},
    },
    "role": {
        "title": "角色 · 以终为始", "icon": "🎭", "max_tokens": 420,
        "domain": "这是「人生角色 · 以终为始」：你现在聚焦的是【本块当前数据】里那一个角色（role.name），\n"
                  "围绕它开展工作：使命宣言是否清晰有力、季度 KR 是否可执行可验证、它名下本周有没有推进中的行动、\n"
                  "这个角色是否被长期搁置（review 空着、KR 久未动）。若数据里有 role.habits（该角色关联的习惯及本周打卡），\n"
                  "必须结合它们判断：习惯打卡是否在支撑这个角色的 KR——低打卡率（如 2/7、0/7）就是角色目标与实际行动脱节的直接证据，\n"
                  "要当着用户点破。你的建议只针对这一个角色，\n"
                  "不做跨角色的全局统筹（那是管家的事）。角色间协调只在确有冲突时补一句。",
        "suggest": "围绕这个角色给 2-3 条建议：使命/KR 哪个先补、哪个 KR 该更新或砍掉、这周为它推一件什么事；\n"
                  "若它有关联习惯且打卡低迷，优先点破脱节并给出最小可恢复动作。\n"
                  "卡在取舍时先用 1 个提问引导（如「这个角色十年后要留下什么？」），再给判断。",
        "quick": ["哪个角色被忽略了？", "哪个目标该砍掉或更新？", "哪个角色最该这周发力？"],
        "fill": {"common": "\n\n（现在是「填充模式」：用户要的是把角色该有的使命/KR直接拟好，不是给建议。）",
                 "prompt": "基于上方角色的使命/目标/KR现状，直接生成 {count} 条「高质量的角色使命宣言草稿」。输出严格 JSON 对象，外层用 {\"items\": [...]} 包裹数组，每条：{name(角色名，从上方 role.name 取；若缺失则用「学习者」), title(使命宣言的一句话：这个角色存在的意义，要具体、有个人色彩、能当锚点，20-40 字), why(一句话：为什么这个使命对用户重要), kr(一条可验证的季度关键结果，动词开头、有数字或可判定的标准)}。使命宣言要真诚有力，不要空洞口号。不要任何解说，只输出 JSON 数组。"},
    },
    "review": {
        "title": "每周复盘", "icon": "🧭", "max_tokens": 460,
        "domain": "这是「每周复盘·磨刀不误砍柴工」：停下来磨刀。看：本周完成量/习惯四维/角色平衡哪里失衡、\n"
                  "数据与预期的差距说明什么、下周只改一件事该改什么。",
        "suggest": "基于本周完成曲线、角色平衡与习惯数据，给 3 条复盘洞察：本周最值得肯定的一点、最大的失衡点、下周只改的一件。",
        "quick": ["这周我最失衡的在哪里？", "下周只改哪一件事？", "哪些复盘该写进角色档案？"],
        "fill": {"common": "\n\n（现在是「填充模式」：用户要的是把这一周的复盘直接写成文，不是给建议。）",
                 "prompt": "基于上方本周数据（完成/习惯/平衡），直接生成 {count} 条「复盘洞察与下周行动」。输出严格 JSON 对象，外层用 {\"items\": [...]} 包裹数组，每条：{title(复盘要点的一句话标题，如「本周最大失衡」), why(洞察正文，2-3 句，引用具体数据), action(下周一个具体可执行动作), role(可空字符串，若数据里有角色就挑最贴的)}。不要泛泛而谈，要引用数据。不要任何解说，只输出 JSON 数组。"},
    },
    "health": {
        "title": "健康看板", "icon": "❤️", "max_tokens": 460,
        "domain": "这是「健康看板·不断更新」：身体是那只下金蛋的鹅（P/PC 的产能侧）。看：睡眠时长与就寝规律、\n"
                  "HRV/静息心率相对个人基线的偏离（恢复信号）、呼吸率（>14次/分=浅快呼吸，副交感不足；共振呼吸目标约6次/分）、\n"
                  "活动/锻炼/正念是否在养鹅、骑行负荷。\n"
                  "引用数据时解读含义（如「HRV 连续低于基线=自主神经没缓过来」「静息心率高于基线=身体还在踩油门」），不要只复述数字。\n"
                  "若数据含 derived 派生指标：sleep_debt=近7晚睡眠债(小时)、sleep_eff=睡眠效率(总睡眠/在床，%)、\n"
                  "avg_bedtime/avg_wake=平均就寝/起床相位、rec_bedtime=CBT-I 滴定出的今晚熄灯时刻、ideal_bedtime=目标相位\n"
                  "（按每晚7.5h+85%效率反推，从 rec_bedtime 每周前移15-20分钟逼近它）、resp=呼吸率。\n"
                  "睡眠调节方法论（CBT-I，建议时按此给）：①固定起床时间是相位锚点，比「早点睡」有效；②睡眠效率<85%说明床too much——\n"
                  "按 rec_bedtime 滴定收紧就寝窗口，困了才上床（刺激控制）；③效率≥90%才逐步放宽15分钟；④晚间光暴露推迟褪黑素，\n"
                  "睡前1小时调暗+暖屏；⑤4-7-8呼吸（吸4屏7呼8）呼气长于吸气可直接激活迷走神经，适合熄灯前做。\n"
                  "数据为 Apple Watch 实测（近 7 天）；若用户写了睡眠主观备注，必须把主客观对齐着看\n"
                  "（如「你说睡得差但深睡其实够，问题可能在中途醒来」，或相反——数字差、体感还行，可能是咖啡因代偿）。",
        "suggest": "基于下方健康数据给 2-3 条恢复与养鹅建议：弱项是什么、今天强度该怎么调（轻量化/正常/可上强度）、\n"
                  "就寝规律给一个今晚的具体熄灯时间（优先用 rec_bedtime，并说明它是怎么从你的数据滴定出来的）。",
        "quick": ["我今天适合上强度还是轻量？", "我的恢复卡在哪一环？", "就寝时间怎么修？", "呼吸练习怎么做？"],
        "fill": {"common": "\n\n（现在是「填充模式」：用户要的是把今晚该执行的健康动作直接列出来，不是给建议。）",
                 "prompt": "基于下方健康数据（恢复度、睡眠、HRV、静息心率、呼吸率、derived 派生指标等），直接生成 {count} 条「今晚/明早就该执行的健康微行动」。输出严格 JSON 对象，外层用 {\"items\": [...]} 包裹数组，每条：{title(一个具体动作，含具体时间或数字，如「今晚 23:00 熄灯」，动词开头、12-20 字), why(一句话：它针对数据里的哪个弱项)}。动作要小到今晚就能做，针对数据里最弱的一两项。不要任何解说，只输出 JSON 数组。"},
    },
}

# ── 穷查理书房 · 随行顾问 ──
# 人格基座：芒格表达 DNA（短句/极端词/粗俗类比/数字锚定/证否自己），只辅助思考不替用户做决定。
MUNGER_COMMON = prompt_header("advisor") + (
    "你是「穷查理书房」的随行顾问——用查理·芒格的思维方法辅助用户思考与决策。通用规则：\n"
    "- 全部用中文。你是芒格的思维助手，不是芒格本人，但用他的方法思考。\n"
    "- 表达风格（芒格表达DNA）：短句、直接、犀利；用极端词（「蠢」「烂」「死」）；用粗俗类比把道理砸进脑子；\n"
    "  能用一个数字就不用一个词；不堆形容词，给可执行的动作。\n"
    "- 永远先问「怎么避免愚蠢」，再谈怎么聪明。\n"
    "- 只辅助思考，不给最终答案：用追问逼用户自己想清楚；结论永远落在追问或可执行动作，不替用户做决定。\n"
    "- 不要任何前言（如「好的，以下是…」）或结尾寒暄；输出 5-8 行以内、短句分行；可用「▪ 」引导要点，不用 markdown 代码块包裹。\n"
)

# 核心智慧：双轨分析/证否自己/能力圈/激励/避免愚蠢/耐心。只用于 advisor_stream（解读建议），
# 严禁拼进 fill——填充模式要求严格 JSON，「追问引导」会和「直接生成」冲突。
MUNGER_KERNEL = (
    "【芒格决策内核 · 每条建议都要体现】\n"
    "1 双轨分析：理性轨道（谁真正控制利益/激励）+ 潜意识轨道（哪些偏误在替你下结论）。\n"
    "2 证否自己：不持有无法证否的观点——「我从不允许自己对任何事持有观点，除非我能比反对者更好地论证他们的立场。」\n"
    "3 能力圈：不懂就 Too Hard，承认太难不是认输。\n"
    "4 激励机制：谁赚钱、谁担险、是否同一拨人。\n"
    "5 避免愚蠢 > 追求聪明：长期优势来自「持续地不犯蠢」。\n"
    "6 耐心：好机会稀少，下重注，然后坐在屁股上。\n"
    "用户卡在取舍/迷茫时，先用 1 个精准的芒格式提问引导（如「这件事的反面证据是什么？」「怎么保证它失败？」），\n"
    "随后仍要给出你基于数据的判断；提问至多 1 个，是引子不是替代。\n"
)

MUNGER_SCENES = {
    "decision": {
        "title": "芒格决策台", "icon": "🧭", "max_tokens": 460,
        "domain": "这是「芒格决策单」：用户正在用双轨分析做一次真实决策。\n"
                  "当前状态（见下方【本块当前数据】JSON 里的字段）：goal=决策事项、basket=Yes/No/Too Hard、incentive=谁赚钱/谁担险/对齐、biases=已勾偏误编号、counter=用户已写的反面证据。\n"
                  "你的任务：以芒格视角审视这份单子——① 三筐归类与激励判断是否自相矛盾；② 已勾偏误之外，还有哪些偏误在起作用（引用 25 偏误编号）；\n"
                  "③ 反面证据是否真的够强，还是用户在自我安慰；④ 若结论是 Yes，指出最可能让这件事失败的 3 个具体原因。\n"
                  "25 偏误速查：#1 奖励超级反应 #2 喜欢/厌恶 #3 避免怀疑 #4 避免不一致 #5 好奇 #6 康德式公平 #7 羡慕/嫉妒 #8 回馈 #9 受简单联想影响 #10 简单的避免痛苦的心理否认 #11 自视过高 #12 过度乐观 #13 被剥夺超级反应 #14 社会认同 #15 对比误反应 #16 压力影响 #17 易获得性误导 #18 不用就忘 #19 化学物质误导 #20 衰老误导 #21 权威误导 #22 废话 #23 重视理由 #24 Lollapalooza #25 极端化。",
        "suggest": "基于下方决策单状态，给 2-3 条芒格式审视：最危险的盲点、被忽略的偏误、反面证据是否够强。",
        "quick": ["这份单子最大的漏洞在哪？", "我可能还中了哪些偏误？", "反面证据够强吗？"],
        "fill": {"common": "\n\n（现在是「填充模式」：用户要的是你直接写出最强的反面证据，不是给建议。）",
                 "prompt": "你是芒格的反面证据机器。针对「{goal}」，写出 {count} 条最强、最具体、最让用户难受的反驳论据——不是客套的「可能不行」，而是能真正动摇用户立场的那几条。输出严格 JSON 对象，外层用 {\"items\": [...]} 包裹数组，每条：{text(一条犀利的具体反驳，20-60 字)}。不要任何解说，只输出该 JSON 对象。"},
    },
    "invert": {
        "title": "逆向工作台", "icon": "🔄", "max_tokens": 420,
        "domain": "这是「逆向工作台」：芒格的方法——「反过来想，总是反过来想」。\n"
                  "用户给出目标，你要把它翻成失败路线：不问「怎么成功」，问「怎么保证失败」，然后逐条避开。\n"
                  "经典配方：卡森三味（吸毒/嫉妒/怨恨）+ 芒格四味（反复无常/不从他人经验学习/意志力崩溃/不放弃第一印象）。\n"
                  "看用户的目标，指出最可能的 3 个死法，每个死法给一个具体的避坑动作。",
        "suggest": "基于下方目标，给 2-3 条逆向洞察：最可能的死法、对应的避坑动作、当前最危险的信号。",
        "quick": ["这个目标最可能的死法是什么？", "怎么保证它失败？", "我现在最危险的信号是什么？"],
        "fill": {"common": "\n\n（现在是「填充模式」：用户要的是你直接把失败路线图写出来，不是给建议。）",
                 "prompt": "针对「{goal}」，直接生成 {count} 条「必然失败路线」。输出严格 JSON 对象：{\"fill\":[\"…\",\"…\",…共 {count} 条字符串]}。每条字符串格式：「死法N：具体失败路径。避坑：对应的避坑动作。」40-80 字，要具体到能直接照着避开，不要空泛。不要任何解说，只输出该 JSON 对象。"},
    },
    "bias": {
        "title": "误判博物馆", "icon": "🎭", "max_tokens": 440,
        "domain": "这是「误判博物馆」：25 种人类误判心理学。用户给出当前决策目标（可能为空），你要识别他可能正中的偏误。\n"
                  "25 偏误速查：#1 奖励超级反应 #2 喜欢/厌恶 #3 避免怀疑 #4 避免不一致 #5 好奇 #6 康德式公平 #7 羡慕/嫉妒 #8 回馈 #9 受简单联想影响 #10 简单的避免痛苦的心理否认 #11 自视过高 #12 过度乐观 #13 被剥夺超级反应 #14 社会认同 #15 对比误反应 #16 压力影响 #17 易获得性误导 #18 不用就忘 #19 化学物质误导 #20 衰老误导 #21 权威误导 #22 废话 #23 重视理由 #24 Lollapalooza #25 极端化。\n"
                  "重点：指出哪些偏误在叠加成 Lollapalooza（≥3 项同时作用时，力量会非线性放大）。",
        "suggest": "基于下方决策目标，给 2-3 条偏误洞察：最可能中的偏误（带编号）、哪些在叠加、怎么对抗。",
        "quick": ["我可能正中了哪些偏误？", "哪些偏误在叠加成 Lollapalooza？", "怎么对抗我最可能中的那个？"],
        "fill": {"common": "\n\n（现在是「填充模式」：用户要的是你直接列出他可能中的偏误编号，不是给建议。）",
                 "prompt": "针对「{goal}」，识别用户最可能正中的 {count} 个偏误。输出严格 JSON 对象，外层用 {\"items\": [...]} 包裹数组，每条：{n(偏误编号，必须是 1-25 的整数), cn(偏误中文名), why(一句话：为什么用户可能中这个偏误，要结合目标具体说)}。不要任何解说，只输出该 JSON 对象。"},
    },
    "incentive": {
        "title": "激励透视镜", "icon": "💎", "max_tokens": 400,
        "domain": "这是「激励透视镜」：芒格说「永远不要低估激励的力量——你永远无法说服一个人，让他明白违背自身利益的事对他有好处。」\n"
                  "用户给出：谁在赚钱（谁从行动中获益）、谁在担风险（搞砸了谁付代价）。\n"
                  "你要判断激励是否对齐：赚钱的和担险的是同一拨人吗？不对齐的激励会扭曲一切行为。\n"
                  "看用户输入，指出激励结构里的危险点：谁在赚不该赚的钱、谁在担不该担的险、怎么重新设计激励。",
        "suggest": "基于下方激励输入，给 2-3 条洞察：激励是否对齐、谁在赚不该赚的钱、怎么重新设计。",
        "quick": ["激励对齐吗？", "谁在赚不该赚的钱？", "怎么重新设计激励？"],
        "fill": {"common": "\n\n（现在是「填充模式」：用户要的是你直接写出激励结构分析，不是给建议。）",
                 "prompt": "针对「{goal}」，分析激励结构。输出严格 JSON 对象，外层用 {\"items\": [...]} 包裹数组，每条：{who(谁在赚钱或担险), role(固定填「赚钱者」或「担险者」), text(一句话分析：他的真实动机/风险敞口，20-50 字)}。要具体犀利，不要空泛。不要任何解说，只输出该 JSON 对象。"},
    },
    "basket": {
        "title": "三筐分拣台", "icon": "🧺", "max_tokens": 380,
        "domain": "这是「三筐分拣台」：芒格把问题分成三筐——Yes（真正理解、在能力圈内）、No（看明白了、确信不做）、Too Hard（想不明白、放弃思考它）。\n"
                  "能力圈三问：① 我真正理解这件事吗？② 我能比反对者更好地论证反对意见吗？③ 我看得懂它的护城河与代价吗？\n"
                  "用户给出一个决策，你要判断它该进哪一筐，并用能力圈三问逼用户自检。",
        "suggest": "基于下方决策，给 2-3 条洞察：该进哪一筐、为什么、用能力圈三问逼用户自检。",
        "quick": ["这件事该进哪一筐？", "我真正理解它吗？", "这是不是 Too Hard？"],
    },
    "lattice": {
        "title": "格栅星空讲解", "icon": "🌌", "max_tokens": 470,
        "domain": "这是「格栅星空·模型讲解」：用户在八大学科格栅中选中了一个思维模型。下方【本块当前数据】JSON：\n"
                  "disc=所属学科名、en=学科英文名、desc=学科一句话、model=被选中的模型名、one=模型一句话说明。\n"
                  "你的任务：① 用最接地气的大白话把这个模型是什么重新讲一遍（比书面定义更贴近人的直觉）；\n"
                  "② 它什么时候最该被调出来用（给 1-2 个具体场景）；③ 它最常见的误用/陷阱是什么；\n"
                  "④ 用芒格的语言把要点砸进脑子——短句、犀利、可执行，最后落在「下一步你能用它做什么」；\n"
                  "⑤ 若 model 属于某个学科，顺带点一句它和该学科另一模型的协同（格栅思维：单学科必然盲区）。",
        "suggest": "针对下方选中的模型，给 2-3 条芒格式讲解：它的本质、最该用的场景、最常见的误用。短句、可落地。",
        "quick": ["这个模型什么时候最该用？", "它最常见的误用是什么？", "芒格通常怎么把它用在决策上？", "它和哪一根其他学科的模型能撞出火花？"],
    },
}

# ── 苏格拉底问题序列 ──
def _socratic_role_hint(self, role):
    """P2-V8: 使命五问的数据 hint——注入该角色现有使命/目标/KR，让用户有参照而非凭空想。"""
    r = _draft_role(self, role) or {}
    krs = []
    for k in (r.get("key_results") or []):
        krs.append(k.get("text") if isinstance(k, dict) else str(k or ""))
    kr_line = "；".join(x for x in krs if x) or "（尚未填写）"
    return (f"角色「{r.get('name') or role}」现有使命：{r.get('mission') or '（尚未填写）'}；"
            f"季度目标：{r.get('goal') or '（尚未填写）'}；关键结果：{kr_line}")

SOCRATIC_FLOWS = {
    "weekly_review": {
        "title": "AI 引导式复盘 · 4F",
        "questions": [
            ("事实：这一周你实际完成了什么？挑两三件真正做成的说。",
             lambda s, role: "本周完成清单：" + "、".join((s._get_week_report().get("titles") or [])[:8])),
            ("感受：这一周里，哪个瞬间让你有成就感？哪个时刻最疲惫？",
             lambda s, role: "本周完成 " + str(s._get_week_report().get("week_total", 0)) + " 项；习惯最长的连续 "
                       + str(max((h.get("streak", 0) for h in s._get_habits().get("habits", []) or [{}]), default=0)) + " 天"),
            ("发现：对照你周初的预期，数据和预期差在哪里？这说明什么？",
             lambda s, role: "链路健康：" + json.dumps(s._get_dashboard_data().get("chain_health", {}), ensure_ascii=False)[:200]),
            ("下一步：下周只改一件事，你会改什么？为什么是它？", lambda s, role: ""),
        ],
        "compile": AI_DRAFT_COMMON +
            "任务：把用户对 4F 复盘四问的回答，汇编成一份周复盘成稿。\n"
            "保留用户原意与语气，补充数据衔接，删除重复；四段小标题「事实：」「感受：」「发现：」「下一步：」。\n"
            "只输出成稿。\n\n【用户的回答】\n{answers}",
    },
    "mission": {
        "title": "使命五问 · AI 引导",
        "questions": [
            ("第一问：在这个角色上，谁是你心中的榜样？他/她身上最打动你的一点是什么？", lambda s, role: _socratic_role_hint(s, role)),
            ("第二问：如果这个角色做到了你理想的样子，那是一幅什么画面？用一两句描述。", lambda s, role: _socratic_role_hint(s, role)),
            ("第三问：十年后回头看，你希望这个角色留下了什么贡献？", lambda s, role: _socratic_role_hint(s, role)),
            ("第四问：在这个角色上，有什么原则是你绝不让步的？", lambda s, role: _socratic_role_hint(s, role)),
            ("第五问：用一句话说，这个角色对你意味着什么？", lambda s, role: _socratic_role_hint(s, role)),
        ],
        "compile": AI_DRAFT_COMMON +
            "任务：把用户「使命五问」的回答，凝炼成一段 80-150 字的第一人称使命宣言。\n"
            "保留用户自己的语言和意象，不添加用户没说过的价值观；结尾落在「因此我要…」式的行动指向。\n"
            "只输出宣言正文。\n\n【用户的回答】\n{answers}",
    },
}

if __name__ == "__main__":
    PORT = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    # HOST 可通过环境变量 DASH_HOST 覆盖（Docker 用 0.0.0.0，本地默认 127.0.0.1）
    HOST = os.environ.get("DASH_HOST", "127.0.0.1")
    # VPS 使用 Persistent systemd timer，避免服务停机或重启时错过周日五分钟窗口；
    # 本地开发仍保留轻量线程，亦可显式 DASH_INPROCESS_SCHEDULER=1 启用。
    if not ON_VPS or os.environ.get("DASH_INPROCESS_SCHEDULER") == "1":
        threading.Thread(target=_scheduler, daemon=True).start()
    threading.Thread(target=_sync_user_profile_with_vps, daemon=True).start()  # 按显式配置同步用户画像
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    # 浏览器首屏会并发大量请求（HTTP/1.0 每条一个新连接），
    # 默认 listen 队列仅 5，容易 ERR_CONNECTION_RESET。放大队列。
    try:
        server.request_queue_size = 256
        server.socket.listen(256)
    except Exception:
        pass
    print(f"☁️ {USER_NAME}驾驶舱 v3 → http://{HOST}:{PORT}")
    try:
        _created_domains = _ensure_recovery_domain_files()
        if _created_domains:
            print("[recovery] 已初始化空数据域: " + ", ".join(_created_domains), flush=True)
        _created_baselines = _ensure_recovery_baselines()
        if _created_baselines:
            print("[recovery] 已建立基线快照: " + ", ".join(_created_baselines), flush=True)
    except Exception as _e:
        print(f"[recovery] 基线快照异常: {_e}", flush=True)
    # 启动时校验镜像一致性（JSON↔MD 漂移检测，只报告不修复）
    try:
        _issues = verify_mirror()
        if _issues:
            for _i in _issues:
                print(f"[mirror-drift] {_i}", flush=True)
    except Exception as _e:
        print(f"[mirror] 启动校验异常: {_e}", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: print("\n已停止"); server.server_close()
