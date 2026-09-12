# 部署指南（开源版）

个人驾驶舱是一个**本地优先**的应用：单进程 Python 后端 + 单页前端，零第三方 pip 依赖。

## 本地运行（推荐起步）

```bash
python3 dashboard-server.py 8787
# 浏览器打开 http://127.0.0.1:8787/
```

首次启动即进入**演示模式**：内置虚构数据（3 角色 / 10 任务 / 5 习惯 / 关系记录 / 健康趋势），
无需任何配置。之后可在设置里连接你自己的数据源（见下）。

## 数据目录

所有用户数据默认写入**运行目录下的 `data/`**（可被 `DASH_DATA_DIR` 环境变量覆盖），
与代码目录分离，删除即重置。

## Docker（可选）

```bash
cd deploy
cp .env.example .env   # 按需填写
docker compose up -d
```

- 镜像基于 `python:3.13-slim`，零 pip 运行时依赖；实际镜像大小需在目标机器构建后确认
- `deploy/deploy.sh`：交互式初始化（数据目录、配置模板）
- `deploy/nginx.conf`：反向代理示例（含安全头）

## systemd（可选，自托管服务器）

```bash
sudo cp deploy/app.service /etc/systemd/system/
# 编辑 WorkingDirectory 指向你的安装目录后：
sudo systemctl daemon-reload && sudo systemctl enable --now app
```

`dashboard-weekly-draft.timer`：每周日 20:00 自动生成复盘草稿的可选定时器。

## ⚠️ 公网部署警告

后端默认只监听 `127.0.0.1`。**绝不要在无认证的情况下把服务直接暴露到公网**——
它读写你的任务、笔记和个人数据。公网访问必须自行加：TLS 终端 + 反向代理鉴权
（Basic Auth / Access 前置）+ 仅回环监听。参见 SECURITY.md。
