"""NAS Vault gunicorn config — 强制单 worker + 不 preload。

用法:
    gunicorn -c gunicorn.conf.py app:app

为什么这些约束:
    NAS Vault 的 organize_runner + scanner 用 module-level lock + abort flag
    (per-process state)。多 worker 会让 batch organize / 全库扫描互相打架；
    preload 模式会让所有 worker 共享 import-time state (fcntl lock 也被 fork
    继承绕过)。所以两条都是 hard requirement。

    Phase 4C 计划升级到 SQLite lease + 跨进程 abort 信号，届时此约束可放开。
"""

bind = "127.0.0.1:8080"
workers = 1
preload_app = False
# 不要改 workers 数；app.py 启动期检测会拒启动。
# 改 timeout 是 OK 的（NAS Vault organize 后台 worker 不阻塞 HTTP request）
timeout = 60
keepalive = 5


def on_starting(server):
    """gunicorn r6 BLOCKER fix: hook 内读 server.cfg ground truth，拦下任何形式的
    multi-worker / preload。即使 user 改 config file / env / 命令行覆盖了上面
    workers/preload_app，hook 在 master 启动期跑，拒绝继续。
    """
    cfg = server.cfg
    if cfg.workers != 1:
        server.log.error(
            f"NAS Vault requires workers=1, got {cfg.workers}; see config_module-level constraint."
        )
        raise SystemExit(1)
    if cfg.preload_app:
        server.log.error(
            "NAS Vault does not support preload_app=True (organize_runner / "
            "scanner module-level state would be shared across workers)."
        )
        raise SystemExit(1)
