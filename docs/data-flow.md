# 数据流图

> 描述当前代码与运行时的数据事实。生产版本以 `/api/ops/status` 的 `release` 为准；
> Service Worker 版本由 `sw.js` 的自动指纹/缓存策略管理。

## 运行链路

```text
浏览器（SPA / PWA）
  → [可选] 反向代理（TLS + 认证，公网部署必须）
  → dashboard-server :8787（默认仅回环监听）
  → GET 聚合 /api/boot、/api/dashboard-data、/api/health
  → TickTick MCP / 本地 Markdown / 本地 JSON / 健康数据接收器
```

写入链路统一遵循：**请求校验 → 幂等键（clientMutationId）→ 权威源写入 → 权威回读（或明确
unverified）→ 缓存失效 → 脱敏回执 → 可追踪的 mutation receipt**。失败不被前端乐观状态掩盖；
传输失败进入 outbox，仅允许人工重试。

健康链路独立于任务链路：

```text
Apple 健康/手表类 App（支持 Health Auto Export 协议）
  → HTTPS /health-api/push（自设 token）
  → health_ingest :8786
  → data/health-data/raw/YYYY-MM-DD/*.json（只追加）
  → dashboard /api/health?days=N（只读聚合）
```

健康 raw 文件是外部设备事实源，驾驶舱不修改；缺失指标区分「未推送、窗口外、解析缺口、陈旧」，
不用 0 伪造。

## 数据源与权威

| 源 | 读 | 写 | 事实源 |
|---|---|---|---|
| TickTick（任务/习惯） | `call_ticktick_mcp(...)` | 同左 | TickTick 云端，本地零缓存落盘 |
| 角色档案 | `parse_role_file()` 读 `data/角色/*.md` | 原子改写同一 md | Markdown 文件本身 |
| 情感账户 | `data/情感账户/relations.json` | 写后重渲染 md 镜像 | **JSON**（md 是只读镜像） |
| 倾听笔记 | `data/倾听笔记/listening.json` | 同上 | **JSON** |
| 影响圈 | `data/影响圈/proactive.json` | 同上 | **JSON** |
| 习惯维度标签 | `data/habit_dims.json` | `set_habit_dimension` | JSON（TickTick 无自定义字段，本地补充） |
| 健康数据 | `data/health-data/raw/*` | 只读（外部设备写入） | raw 文件 |
| 管家记忆/画像/规则 | `data/*.json` | 对应 `_save_*`/`_merge_*` | 各自 JSON |
| 键盘诊断探针 | 无读端点 | `POST /api/kb-diag` → `data/kb_diag.jsonl` | jsonl 日志，非业务数据 |

所有路径均落在 `DASH_DATA_DIR`（默认 `data/`），可用环境变量整体迁移；指向 Obsidian vault
子目录即可用 Obsidian 浏览角色/镜像文件。

## JSON↔MD 双写机制

- `_write_json()` 写主数据 → 成功后 `_md_sync()` **全量重渲染** md（非增量 append，避免「删了 md 还在」）。
- `_md_sync()` 写前比对内容，未变只对齐 mtime 不落盘。
- `_mirror_stale()`：GET 时若 json mtime 新于 md，惰性自愈重渲染。
- `verify_mirror()`：条目数比对，drift 报告进启动日志（`[mirror-drift]`）。

## 缓存分层

- 唯一缓存层：模块级 `_cache`，30s TTL；所有写操作成功路径 `bust_cache()` 全清。
- `/api/invalidate`：前端写操作后主动失效，避免读到 30s 旧数据。

## 未捕获异常防护

`do_GET`/`do_POST` 的 `/api/*` 分发链有外层 try/except 兜底，统一返回既有
`{success:false,error}` / `{error}` 契约——任一 handler 内部异常不再表现为「网络失败」。

## 已知的权威边界设计

- `call_ticktick_mcp` 在源头统一识别 JSON-RPC `result.isError`，业务失败不被当成功。
- 日期窗口计算用 `date/timedelta`（跨月安全），不做 YYYYMMDD 整数加减。
