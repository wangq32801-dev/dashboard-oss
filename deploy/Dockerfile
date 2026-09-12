# 个人驾驶舱 · Docker 镜像
# 纯 Python 标准库，零外部依赖
FROM python:3.13-slim

WORKDIR /app
COPY LICENSE THIRD-PARTY-NOTICES.md ./

# 复制后端 + 前端静态文件 + 入口脚本 + 苹果风背景图 + 装饰图片 + 神谕名言库
COPY dashboard-server.py hermes-dashboard.html chart.umd.min.js quotes.js ./
COPY ai_prompts.py ai_runtime.py health_ingest.py dashboard_entities.py dashboard_sqlite.py demo_data.py ./
COPY frontend-modules/ ./frontend-modules/
COPY sw.js manifest.webmanifest ./
COPY img/ ./img/
COPY icon-1024.png icon-192.png icon-512.png icon-512-maskable.png moon-crescent.png ./
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 环境变量（与 config 文件互补：文件优先；DASH_DEMO=1 时全部忽略）
ENV PORT=8787
ENV DASH_HOST=0.0.0.0
ENV DASH_DATA_DIR=/data
ENV TICKTICK_TOKEN=""
ENV LLM_API_KEY=""
ENV LLM_API_BASE=""
ENV LLM_MODEL=""
ENV VPS_AUTH_USER=""
ENV VPS_AUTH_PASS=""

EXPOSE 8787

ENTRYPOINT ["/entrypoint.sh"]
CMD ["8787"]
