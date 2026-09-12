# 部署说明

## 本地（推荐）

```bash
python3 dashboard-server.py 8787
# 打开 http://127.0.0.1:8787/
```

无配置自动进入演示模式。数据目录默认 `./data/`（`DASH_DATA_DIR` 可改）。

## 配置

查找顺序：`DASH_CONFIG` 环境变量指定的文件 → 项目目录 `./config`。不自动继承主目录配置；强制演示模式不读取配置文件。远端画像同步需要显式设置 `DASH_PROFILE_SYNC=1`。
格式为 `KEY=VALUE` 纯文本（`#` 注释）。可用键见 `deploy/.env.example`——
`TICKTICK_TOKEN`、`PROJECT_IDS`、`LLM_API_BASE/KEY/MODEL/FALLBACK`、
`WECHAT_DELIVER`、`USER_NAME`。图片理解使用同一组 `LLM_API_BASE` / `LLM_API_KEY`，未配置时不会上传图片。

> Docker 用户可直接用环境变量（见 `deploy/docker-compose.yml`）。

## Docker

```bash
cd deploy && cp .env.example .env && docker compose up -d
```

## systemd（自托管服务器）

先创建专用 dashboard 系统用户和组，将 data 目录所有权交给该用户；其余代码保持只读。服务不应以 root 运行。

```bash
sudo cp deploy/app.service /etc/systemd/system/
# 编辑 WorkingDirectory 后：
sudo systemctl daemon-reload && sudo systemctl enable --now app
```

可选：`deploy/dashboard-weekly-draft.timer` 每周自动生成复盘草稿。

## 健康数据接收（可选）

```bash
# health_ingest.py 仅提供诊断函数，不包含 HTTP 接收服务。
```

本版本未提供健康推送接收器。需要自行实现并验证认证接收端后，才能在健康 App 配置推送 URL 和 token。
设备推送的原始数据落在 `data/health-data/raw/`（只追加）。

## 公网部署警告

后端默认仅回环监听。公网访问必须自行加 TLS + 认证反向代理，
详见 SECURITY.md。**禁止无认证直接暴露服务。**
