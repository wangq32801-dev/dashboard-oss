# 数据一致性契约（按数据域）

> 口径：权威来源=唯一可写事实源；本地缓存=可丢弃投影；回读=写后以权威源验证
> （verified/unverified 二态，禁止假成功）。

| 数据域 | 权威来源 | 本地缓存 | 写入接口 | 回读 | 冲突处理 | 失败恢复 | 快照/回滚 |
|---|---|---|---|---|---|---|---|
| 任务 | TickTick 云端 | S.tasks（内存）+boot 快照 | /api/tasks/{create,update,complete,archive,unarchive}（clientMutationId 幂等） | 全部写后 0.25+0.75s 双重读，失败显式报错 | 单人单写者；跨端以 TickTick 为准 | outbox 人工重试；失败不落账 | TickTick 自身版本 |
| 习惯 | TickTick（打卡/连击）+ data/habit_dims.json（维度映射） | S.habits + 打卡本地增量 | /api/habits/{create,checkin,update,dimension} | checkin 按返回值更新本地 | 维度映射 revision 单调 | 打卡失败 toast+不更新 | dims 原子写+损坏保护 |
| 角色/使命 | data/角色/*.md | S.roles/roleMissions | /api/roles/{create,sync,delete} | sync 后 parse_role_file 回读（含改名分支） | 单人；外部编辑以文件为准 | 删除进 .回收站 可恢复 | md 原子写+回收站 |
| 复盘 | 角色 md 正文 | rv-text 草稿（dash_drafts） | /api/review/save | 原子写本地 md 即权威 | 同角色域 | 草稿保护（未保存提示） | md 原子写 |
| 关系/倾听/影响圈 | 对应 JSON + MD 只读镜像 | LO.bank/listen/pro | /api/{relations,listening,proactive}[/delete/restore/update] | 写后 _sync_mirror 全量重渲染+镜像回读 | 软删墓碑+ts 取新 | 软删可 restore | 原子写+recovery 域 |
| 健康 | data/health-data/raw（设备只追加） | 无（聚合缓存 30s） | 无（只读域） | /api/health?days=N 聚合（units/source 标注；missing≠0） | 单写者（设备） | 聚合失败回退快照 | raw 只追加 |
| 周计划/大石头 | week-plan JSON | LO.rocks | /api/week-plan（revision+回读+幂等） | 回读 | revision 乐观并发 | 失败回滚 | recovery 域 |
| 管家记忆/画像 | data/*.json | 内存注入 | 对话自动沉淀+确认动作 | 原子写回读 | 单进程内锁 | 双锁防并发丢行 | recovery 域 |
| SW/前端资源 | 仓库 | SW precache（构建号版本） | 仅发布通道 | navigate/JS 网络优先 | Last-Modified 版本链 | SW 缓存回退 | 发布快照 |

## 已知残余

- 打卡/周界/复盘周标题依赖服务端本地时区：Docker 部署建议显式设 TZ。
- complete 之外的撤销（undo）语义为「归档」，不做物理删除。
