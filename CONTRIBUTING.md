# 贡献指南

感谢关注这个项目！它首先是一个真实使用的个人系统，其次才是开源项目——所以贡献前请先理解两个原则：

## 心智模型

1. **本地优先**：任何功能不应强制要求外部服务；无配置必须能跑（演示模式）。
2. **写操作不装成功**：涉及数据写入的改动必须走幂等 + 权威回读模式，参考 `dashboard-server.py` 中 `create_task`/`update_task` 的实现与 `docs/data-consistency.md` 的契约表。

## 开发环境

```bash
git clone <fork> && cd dashboard-oss
python3 dashboard-server.py 8787        # 演示模式直接跑
python3 -m unittest discover -s tests -p "test_*.py"   # Python 全量（应全绿）
bash scripts/verify-release.sh          # 完整门禁（含语法/结构/浏览器冒烟）
```

浏览器测试需要本地 Chromium（`PLAYWRIGHT_CHROMIUM` 可指定可执行文件路径）。

## 提交前检查单

- [ ] `python3 -m unittest discover -s tests` 全绿（或说明跳过原因）
- [ ] `node --check` 通过（若改了前端 JS）
- [ ] 新增写端点：幂等键 + 回读验证 + 失败明确报错
- [ ] 新增 AI 能力：动作走白名单，UI 走确认卡
- [ ] 不引入新的运行时依赖（当前零 pip 是特性）
- [ ] 不提交任何真实数据/密钥/个人路径

## 分支与 PR

- 功能分支 `feat/<name>`，修复分支 `fix/<name>`
- PR 描述包含：动机、改动摘要、测试证据（命令+退出码）
- 涉及隐私边界（AI 外发数据字段）的改动必须同步更新 `PRIVACY.md`

## 行为准则

对事不对人；中文英文均可；提问前先读 `docs/`。
