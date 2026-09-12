# 驾驶舱全系统数据流与权威关系（2026-09-04 审计批校准版）

> 本文是 `docs/data-flow.md` 的全系统视图补充：覆盖 TickTick、Obsidian、本地 JSON、HAE 健康、AI、前端状态六大数据域的权威来源、同步方向、冲突与失败处理。实现细节（镜像机制/缓存分层/勘误记录）仍以 `docs/data-flow.md` 为准；两者冲突时以代码与本文为准。

## 一、权威总表

| 数据域 | 唯一权威源 | 谁可写 | 同步方向 | 冲突处理 | 失败处理 |
|---|---|---|---|---|---|
| 任务/完成/日期/标签/象限 | TickTick 云端 | 驾驶舱经 `call_ticktick_mcp`（写后按 ID 回读） | 单向：驾驶舱 → TickTick → 前端读取 | 无本地副本，不产生合并冲突 | MCP `isError` 上抛（{success:false}）；前端乐观态 + outbox 人工重试，失败不落账 |
| 习惯与打卡 | TickTick 云端 | 同上（`update_task(status=2)` 语义用于任务；打卡走 habits/checkin 回读） | 单向 | 无 | 同上；查询窗口用 `timedelta`（防月末整数日期戳） |
| 习惯维度映射 | `DASH_DATA_DIR/habit_dims.json` | 仅驾驶舱 | 本地 JSON，revision 乐观并发 | 旧 revision 拒绝（conflict） | 原子写 + 损坏保护（corrupt 备份） |
| 角色/使命/KR/复盘正文 | `DASH_DATA_DIR/角色/*.md` | 仅驾驶舱（原子写 `create/sync/delete`、`review/save`） | 文件 → 前端；可将数据目录放入自己的笔记库 | 软删进回收站；改名防覆盖 | `_write_text_atomic`（temp+rename+600）；写后回读文件 |
| 情感账户/倾听/影响圈 | `DASH_DATA_DIR` 下的对应 JSON | 仅驾驶舱（先写 JSON 再 `_sync_mirror` 全量重渲染 MD） | JSON 主 → MD 只读镜像 | 条目级 revision + ts 刷新；`/api/sync/conflicts` 脱敏报告 + 人工 ack | 镜像写失败不阻断主数据；`_mirror_stale` 惰性自愈 |
| 健康 raw | `DASH_DATA_DIR/health-data/raw/<date>/`（HAE 推送，只追加） | 仅 HAE（驾驶舱**永不改写**） | HAE → health-ingest :8786 → raw → dashboard 只读聚合 | 无（外部设备唯一写者） | 缺失区分：未推送/窗口外/解析缺口/陈旧；绝不用 0 伪造；质量摘要 `workout_quality` |
| 管家记忆/偏好/规则/画像 | `DASH_DATA_DIR` 下对应 JSON | 驾驶舱 + 用户确认的 AI 动作（write coordinator） | 本地文件，可由用户自行备份 | revision 合并，不做 mtime 最后写入 | 损坏自动备份重建；操作前检查相关数据域 |
| AI 生成物（神谕/周复盘草稿/健康简报） | 各自 JSON 缓存（oracle/draft/briefing） | 仅 AI 管道（LLM 成功后落盘） | 单向 | 失败保留旧草稿/旧神谕（不破坏） | LLM 失败不缓存不假成功；前端明确错误态 + 重试按钮 |
| 前端状态 `S`/`LO` | 派生只读缓存（TickTick/JSON/boot 聚合的内存投影） | 仅前端渲染管道 | 单向：权威源 → 前端 | 无（boot 单请求聚合 + SWR 快照先显后校准） | 快照兜底（`dash_health_snap` 10 分钟窗）；boot 单域失败不白屏 |
| 本地 UI 状态 | `dash_last_view` / `dash_last_global_view` / 草稿 keys | 仅浏览器 | localStorage，不跨设备 | 上次视图恢复用 `dash_last_view`；全局分区记忆用白名单校验的独立键 | head 首帧 CSS 预置防闪跳；键缺失回落默认 |

## 二、关键链路图

```text
【任务写闭环】UI(button) → write-coordinator(outbox+幂等 clientMutationId)
  → POST /api/tasks/* → TickTick MCP(update_task 等) → 按 ID 权威回读
  → bust_cache(30s TTL) → 脱敏 receipt(success:true/verified) → 前端 keyed 行级同步
  失败：不落账/明确 unverified/outbox 人工重试；重复提交 duplicate:true

【健康读管道】Apple Watch → HAE HTTPS push(token) → health-ingest :8786
  → raw/<date>/*.json 只追加 → dashboard /api/health?days=N(1-60) 只读聚合
  → 前端今日状态带(同 readiness 口径) / 健康页(7/14/30 默认 7)
  后台指纹探针(120s, 仅健康页可见时) → 只置「有新数据」提示，不重绘

【AI 边界】所有场景 prompt 走 ai_prompts.py::PROMPT_CATALOG(版本化+事实边界注入)
  → ai_runtime.build_evidence 只给元数据 → SSE 流式(textContent 渲染)
  → 写业务必须经 butler/act 确认卡 + write coordinator；文本本身不产生写入
  → 遥测仅 metadata(record_call)

【发布链】工作副本 → 本地全量测试 → verify-release.sh
  → 用户选择的部署方式（本地、Docker 或 systemd）
  → 健康检查与 /api/ops/status 回读
```

## 三、同步与失败不变量（审计确认）

1. **写操作四件套**：幂等键（同路由+同键 duplicate:true）、权威回读（或明确 `unverified`）、`bust_cache`、脱敏回执——239 项契约测试锁定。
2. **条目级 ts 铁律**：影响「条目是否应存在/显示」的写（软删/恢复/改字段）必须刷新 `ts`，否则跨端 merge 取旧 ts、操作不传播。
3. **MD 永远是镜像**：relations/listening/proactive 手改 MD 会被下次 JSON 写入覆盖（设计如此，非缺陷）。
4. **健康 raw 不可逆保护**：驾驶舱无任何写 raw 的代码路径；旧批次只移入可恢复目录。
5. **前端不造第二事实源**：`S`/`LO` 仅为投影；`LO=window.LO` 暴露给模块只读使用，双份实现风险由契约测试（内联兜底必须存在且语义一致）约束。
6. **错误一律脱敏外发**：`_safe_err` 收口（2026-09-04 审计批），`send_json` 自动 400/404 升级，无「200 携带错误」。
