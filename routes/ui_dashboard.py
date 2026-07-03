"""UI dashboard fragments — HTMX poll endpoints for dashboard cards."""

from __future__ import annotations

from functools import wraps

from flask import Blueprint, render_template

ui_dashboard_bp = Blueprint("ui_dashboard", __name__, url_prefix="/ui")


def _require_token(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        from app import require_token  # deferred (循环 import 避免)

        return require_token(view)(*args, **kwargs)

    return wrapped


@ui_dashboard_bp.route("/dashboard/system", methods=["GET"])
@_require_token
def system_card():
    """Provider 状态卡片 (复用 ui_status providers data)."""
    from app import get_cached_providers_status
    from routes.ui_status import _to_status_segments

    raw = get_cached_providers_status(force=False)
    providers = _to_status_segments(raw)
    return render_template("partials/dashboard/system_card.html", providers=providers)


@ui_dashboard_bp.route("/dashboard/workers", methods=["GET"])
@_require_token
def workers_card():
    """后台 worker 卡片 (复用 ui_status workers data)."""
    from routes.ui_status import _aggregate_running_workers

    workers = _aggregate_running_workers()
    return render_template("partials/dashboard/workers_card.html", workers=workers)


@ui_dashboard_bp.route("/dashboard/todo", methods=["GET"])
@_require_token
def todo_card():
    """待处理卡片 (复用 ui_status badges data)."""
    from routes.ui_status import _compute_sidebar_badges

    badges = _compute_sidebar_badges()
    return render_template("partials/dashboard/todo_card.html", badges=badges)


@ui_dashboard_bp.route("/dashboard/library-stats", methods=["GET"])
@_require_token
def library_stats_card():
    """媒体库统计卡片: total + 按类型 + 评分分布 + top genres."""
    from app import get_db
    from services import metadata_cache

    stats = metadata_cache.get_library_stats(get_db())
    return render_template("partials/dashboard/library_stats_card.html", stats=stats)


@ui_dashboard_bp.route("/dashboard/disk", methods=["GET"])
@_require_token
def disk_card():
    """磁盘空间卡片 (复用 SSH df 解析逻辑)."""
    from app import NAS_BASE_PATH, NAS_DISK_PATTERN, _validate_glob_pattern, ssh_exec

    pattern = NAS_DISK_PATTERN if NAS_DISK_PATTERN else NAS_BASE_PATH
    disks: list[dict] = []
    error: str | None = None
    try:
        _validate_glob_pattern(pattern)
        cmd = f"df -hP {pattern} 2>/dev/null"
        code, stdout, _stderr = ssh_exec(cmd)
        if code != 0:
            error = "SSH df 失败"
        else:
            for line in stdout.strip().split("\n")[1:]:
                parts = line.split()
                if len(parts) >= 6:
                    pct_str = parts[4].rstrip("%")
                    try:
                        pct = int(pct_str)
                    except ValueError:
                        pct = 0
                    disks.append(
                        {
                            "mount": parts[5],
                            "size": parts[1],
                            "used": parts[2],
                            "available": parts[3],
                            "use_percent": pct,
                        }
                    )
    except ValueError:
        error = "无效的 disk pattern"
    except Exception as e:
        error = f"读取失败: {e}"

    return render_template("partials/dashboard/disk_card.html", disks=disks, error=error)


def _poster_for_content_path(conn, content_path: str | None) -> str | None:
    """content_path（目录或单文件）→ media_files.poster_url（已有缓存列，无新 API）。"""
    if not content_path:
        return None
    row = conn.execute(
        "SELECT poster_url FROM media_files WHERE path = ? AND poster_url IS NOT NULL LIMIT 1",
        (content_path,),
    ).fetchone()
    if row is None:
        # 前缀匹配用范围扫描而非 LIKE：content_path 含 % / _ 时（torrent 目录名常见）
        # LIKE 会把它们当通配符 → 误匹配 + 全表扫描；范围扫描走 path UNIQUE 索引零转义。
        # '0' 是 '/' (0x2F) 的下一个码点 (0x30)，[prefix+'/', prefix+'0') 区间精确覆盖
        # "以 prefix/ 开头"的所有路径。不要用 '/~' 当上界——中文文件名 UTF-8 字节 > 0x7E 会漏。
        row = conn.execute(
            "SELECT poster_url FROM media_files "
            "WHERE path >= ? || '/' AND path < ? || '0' AND poster_url IS NOT NULL LIMIT 1",
            (content_path, content_path),
        ).fetchone()
    return row["poster_url"] if row else None


def _fmt_ts(ts: int | None) -> str:
    """Unix ts → 友好相对时间 ("刚刚" / "5 分钟前" / "MM-DD HH:MM")."""
    if not ts:
        return ""
    import time

    now = int(time.time())
    diff = now - int(ts)
    if diff < 60:
        return "刚刚"
    if diff < 3600:
        return f"{diff // 60} 分钟前"
    if diff < 86400:
        return f"{diff // 3600} 小时前"
    if diff < 7 * 86400:
        return f"{diff // 86400} 天前"
    return time.strftime("%m-%d %H:%M", time.localtime(int(ts)))


# organize 任务里需要人工处理的状态 → 首页活动卡可点开「处理抽屉」
_ACTIONABLE_ORGANIZE = {"skipped_needs_identify", "skipped_low_confidence", "failed"}


@ui_dashboard_bp.route("/dashboard/activity", methods=["GET"])
@_require_token
def activity_card():
    """最近活动卡片: organize / scan recent runs 时间线."""
    from app import get_db, ssh_exec
    from services import path_resolver

    conn = get_db()
    items: list[dict] = []

    # auto_organize_runs: 最近 10 条 (terminal + running)
    for row in conn.execute(
        """
        SELECT torrent_name, status, created_at, completed_at,
               files_succeeded, files_failed, last_error, content_path
          FROM auto_organize_runs
         ORDER BY COALESCE(completed_at, created_at) DESC
         LIMIT 10
        """
    ):
        ts = row["completed_at"] or row["created_at"]
        status = row["status"]
        # qBit 报的 content_path 是 QNAP /share/<alias> symlink；扫库/元数据端点都用
        # canonical (/share/CACHEDEV*_DATA/...)。在此边界统一解析成 canonical，poster 查询
        # 与下游 list-videos/cached/identify/organize 才对得上 namespace（resolve 有 5min
        # 缓存 + SSH 失败安全回退原路径）。见 alias-vs-canonical 陷阱。
        content_path = (
            path_resolver.resolve(row["content_path"], ssh_exec)
            if row["content_path"]
            else row["content_path"]
        )
        items.append(
            {
                "ts": ts,
                "ts_label": _fmt_ts(ts),
                "kind": "organize",
                "label": row["torrent_name"] or "(unknown)",
                "status": status,
                "ok": row["files_succeeded"] or 0,
                "fail": row["files_failed"] or 0,
                "error": row["last_error"],
                # 最多 10 条 × 2 查询，SQLite 本地 <20ms，30s 轮询可接受；量大时改批量 IN
                "poster_url": _poster_for_content_path(conn, content_path),
                # 人工处理入口：needs_identify / low_confidence / failed → 抽屉可操作
                "content_path": content_path,
                "actionable": status in _ACTIONABLE_ORGANIZE,
                "action_note": (
                    "该类型不支持自动整理到电影/剧集库"
                    if status == "skipped_unsupported"
                    else None
                ),
            }
        )

    # scan_runs: 最近 5 条
    for row in conn.execute(
        """
        SELECT id, base_path, status, started_at, completed_at,
               files_total, files_done, files_failed
          FROM scan_runs
         ORDER BY COALESCE(completed_at, started_at) DESC
         LIMIT 5
        """
    ):
        ts = row["completed_at"] or row["started_at"]
        items.append(
            {
                "ts": ts,
                "ts_label": _fmt_ts(ts),
                "kind": "scan",
                "label": f"全库扫描 ({row['base_path']})",
                "status": row["status"],
                "ok": row["files_done"] or 0,
                "fail": row["files_failed"] or 0,
                "error": None,
                "poster_url": None,
                "content_path": None,
                "actionable": False,
                "action_note": (
                    "全库扫描失败，可到扫描历史查看详情"
                    if row["status"] == "failed"
                    else None
                ),
            }
        )

    items.sort(key=lambda x: x["ts"] or 0, reverse=True)
    items = items[:10]

    return render_template("partials/dashboard/activity_card.html", items=items)
