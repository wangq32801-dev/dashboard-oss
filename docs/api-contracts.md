# 驾驶舱 API 契约总表

版本：2026-08-31
事实来源：`dashboard-server.py` 的 `do_GET` / `do_POST` 路由。本文只记录契约
边界，不记录 token、正文、健康原始值或 AI 对话全文。

## 通用契约

- 业务请求必须经过可信来源检查；公网未鉴权请求不得获得业务数据。
- JSON 成功响应通常含 `success: true`；写入成功还应含 `clientMutationId`、
  `verified`/`unverified` 和脱敏的 mutation receipt。
- 失败响应为 `{success:false,error}`（SSE 在事件流中发送 error 事件）；HTTP
  状态码反映请求错误、冲突、过大或服务器异常。
- POST 可携带 `clientMutationId`。相同路由+相同键重复提交必须幂等，执行中的
  重复请求返回 `409`/`uncertain`，不得创建第二条实体。
- 可缓存 GET 只读；写操作成功后由服务端/客户端失效聚合缓存。健康页手动刷新
  使用 `fresh=1` 绕过聚合缓存，后台指纹探针不能重绘当前视图。
- `/api/ops/status.data` 只返回权威域数量、revision 策略计数、冲突域名摘要和
  SQLite `read-model` 标记；不下发文件路径、正文、健康原始值或凭据。
- `/api/health?days=N` 返回窗口内的只读聚合；核心数量带 `units`/`source`，workout
  带 `distance_source`、`speed_source`、`energy_source`、心率/室内外字段和
  `workout_quality` 摘要。
  `reported` 是 HAE 原值，`gps`/`derived` 是驾驶舱推算，`missing` 绝不显示为 0。

## GET 路由

### 启动、状态与一致性

`/api/boot` · `/api/healthz` · `/api/vps-status` · `/api/ops/status` ·
`/api/recovery/status` · `/api/shadow/status` · `/api/sync/conflicts` ·
`/api/mutations/recent` · `/api/advisor/outcomes` · `/api/chain-health`

### 业务聚合

`/api/dashboard-data` · `/api/habits` · `/api/stats` · `/api/stats/week` ·
`/api/review/draft` · `/api/local-state` · `/api/relations` · `/api/listening` ·
`/api/week-plan` · `/api/proactive` · `/api/coach/cards`

### 健康与 AI 只读

`/api/health` · `/api/health/notes` · `/api/health/changes` ·
`/api/health/ingest` · `/api/health/briefing` · `/api/health/behavior` ·
`/api/correlation` · `/api/ai/oracle` · `/api/ai/usage` · `/api/ai/contracts` · `/api/butler/status` ·
`/api/butler/history` · `/api/butler/actions` · `/api/butler/prefs` ·
`/api/butler/gaps` · `/api/butler/profile` · `/api/review/draft`

## POST 路由矩阵

服务端 `POST_ROUTE_CLASSES` 对每条 POST 明确标注 `mutation`、`read-only`、
`read-model`、`diagnostic` 或 `operator`；`tests/test_post_matrix.py` 会比较
源代码路由集合与分类集合，新增 POST 若未选择副作用策略会直接阻断回归。

| 路由 | 写入/副作用 | 事实源或执行器 | 幂等/回读要求 |
|---|---|---|---|
| `/api/tasks/create` | 创建任务 | TickTick | 幂等；按原 ID 回读 |
| `/api/tasks/update` | 更新任务 | TickTick | 幂等；字段存在性语义；回读 |
| `/api/tasks/archive` / `/api/tasks/unarchive` | 标签归档/恢复 | TickTick | 原 ID；回读标签 |
| `/api/tasks/delete` | 兼容删除端点 | TickTick | 仅明确删除语义；不得被归档 UI 调用 |
| `/api/tasks/complete` | 完成任务 | TickTick `update_task(status=2)` | 禁止依赖有缺陷的 `complete_task`；回读 |
| `/api/roles/create` / `/api/roles/sync` / `/api/roles/delete` | 角色文件写入 | Obsidian Markdown | 原子写；回读文件 |
| `/api/habits/create` / `/api/habits/update` / `/api/habits/checkin` | 习惯/打卡 | TickTick | 幂等；回读；维度映射另存 JSON |
| `/api/habits/dimension` | 维度映射 | `~/.dash_habit_dims.json` | revision；不覆盖 TickTick |
| `/api/review/save` | 复盘保存 | Obsidian Markdown | 原子写；回读 |
| `/api/relations` / `/api/relations/delete` / `/api/relations/restore` / `/api/relations/update` | 情感账户 | relations.json + MD 镜像 | revision；镜像回读 |
| `/api/listening` / `/api/listening/delete` / `/api/listening/restore` / `/api/listening/update` | 倾听笔记 | listening.json + MD 镜像 | revision；镜像回读 |
| `/api/proactive` / `/api/proactive/delete` / `/api/proactive/restore` / `/api/proactive/update` | 影响圈 | proactive.json + MD 镜像 | revision；镜像回读 |
| `/api/health/notes` / `/api/health/notes/delete` | 健康备注 | 健康 notes JSON | revision；不写 raw |
| `/api/week-plan` | 周计划 | 本地/VPS JSON | revision；回读 |
| `/api/local-state` | 本地状态 | 本地/VPS JSON | revision；合并不覆盖未知更新 |
| `/api/ai/oracle/refresh` | 生成神谕 | AI + oracle JSON | 冷却；成功必须显式 `success` |
| `/api/review/draft/refresh` | 生成周复盘草稿 | AI + draft JSON | 失败不破坏旧草稿 |
| `/api/butler/profile` / `/api/butler/prefs/save` | 管家配置 | JSON | revision；回读 |
| `/api/butler/act` / `/api/butler/undo` | AI 建议动作/撤销 | write coordinator | 用户确认；建议 ID；回读 |
| `/api/butler/history/clear` | 清空对话历史 | history JSON | 明确确认；回读 |
| `/api/advisor/fill` / `/api/advisor/outcomes` | 顾问填充/反馈 | AI + outcomes JSON | 不直接写业务；结果可追踪 |
| `/api/ai/usage` | AI 调用遥测 | 脱敏模型/耗时/成功率元数据 | 不返回提示词、用户数据或生成正文 |
| `/api/ai/contracts` | AI 契约版本 | 场景输入/输出 schema 与证据边界 | 只读静态元数据，不含用户数据 |
| `/api/munger/fill` | 场景填充 | AI | 不直接写业务 |
| `/api/shadow/reconcile` | 只读影子对账 | SQLite shadow | 不改事实源；仅更新读模型报告 |
| `/api/sync/conflicts/ack` | 确认冲突 | sync metadata | 只确认，不覆盖数据 |
| `/api/recovery/restore` | 恢复快照 | 受限 JSON 恢复域 | 明确键；恢复后校验 |
| `/api/sync-vps` | 触发发布 | `sync-dashboard.sh` | 不绕过脚本；回读发布状态 |
| `/api/invalidate` | 清理缓存 | 内存缓存 | 无业务写入 |
| `/api/kb-diag` | 几何诊断日志 | `kb_diag.jsonl` | 不含业务数据；失败不阻断 |
| `/api/butler/vision` | 图片理解 | AI | 仅允许 PNG/JPEG/WebP/GIF；base64 严格校验且限制体积；不落盘原图；每客户端每分钟最多 6 次，超限返回 429 + `Retry-After` |
| `/api/butler/chat` / `/api/butler/stream` | 管家对话 | AI | 失败降级；不写业务 |
| `/api/ai/draft` / `/api/advisor/stream` | AI 流式生成 | AI | SSE error 事件；不写业务 |
| `/api/munger/advisor` | 场景顾问流 | AI | SSE error 事件；不写业务 |
| `/api/ai/socratic` / `/api/ai/socratic/compile` | 苏格拉底流程 | AI | 结构校验；不直接写业务 |

## 契约维护规则

新增或删除路由时必须同时更新本表和 `tests/test_api_contracts.py` 的自动扫描；
测试会从 `do_GET`/`do_POST` 提取 `/api/*` 路由并确认每一条都在本文出现，避免
“代码已变、文档仍旧”的接手风险。
