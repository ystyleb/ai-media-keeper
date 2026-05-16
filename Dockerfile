# NASVault Docker image — Flask app + gunicorn 单 worker。
#
# 用法（开发）:
#   docker build -t nasvault .
#   docker run --rm -p 8080:8080 \
#       -v $(pwd)/config:/app/config \
#       -v ~/.ssh:/root/.ssh:ro \
#       nasvault
#
# 生产推荐用 docker-compose.yml 起 — 持久化 config volume + 自动 restart。
#
# 注意：
# - SSH key 必须挂进容器（NAS 走 SSH ControlMaster），ro 挂载 + 只读
# - config/ 持久化到 host：API token / 配置都在里面
# - 默认 bind 127.0.0.1:8080，远程访问通过反代 / Tailscale，不直接公网暴露

FROM python:3.12-slim AS base

# 系统依赖：openssh-client（SSH 到 NAS）、tini（PID 1 信号转发）
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        openssh-client \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制 requirements 利用 Docker layer cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# 复制 app code
COPY app.py gunicorn.conf.py ./
COPY services/ services/
COPY db/ db/
COPY mcp_server/ mcp_server/
COPY templates/ templates/
COPY static/ static/

# 创建 config 目录（运行时挂 volume 覆盖）
RUN mkdir -p config && chmod 700 config

EXPOSE 8080

# tini 作为 PID 1 — 处理 SIGTERM 优雅停掉 worker
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
