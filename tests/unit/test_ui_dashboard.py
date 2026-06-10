"""routes/ui_dashboard helpers unit tests — _poster_for_content_path 范围扫描."""

from __future__ import annotations

import sqlite3

import pytest

from routes.ui_dashboard import _poster_for_content_path


@pytest.fixture
def conn():
    """内存 SQLite + 最小 media_files 表（只含 helper 用到的列）."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        """
        CREATE TABLE media_files (
            id INTEGER PRIMARY KEY,
            path TEXT UNIQUE NOT NULL,
            poster_url TEXT
        )
        """
    )
    yield c
    c.close()


def _seed(conn, path: str, poster_url: str | None):
    conn.execute(
        "INSERT INTO media_files(path, poster_url) VALUES (?, ?)",
        (path, poster_url),
    )


def test_exact_match(conn):
    """content_path 精确等于 media_files.path 时命中."""
    _seed(conn, "/share/pt/Movie.2023.mkv", "https://img.example/p1.jpg")
    assert (
        _poster_for_content_path(conn, "/share/pt/Movie.2023.mkv") == "https://img.example/p1.jpg"
    )


def test_prefix_match(conn):
    """content_path 是目录时，命中目录下子文件的 poster_url."""
    _seed(conn, "/share/pt/Show.S01/Show.S01E01.mkv", "https://img.example/p2.jpg")
    assert _poster_for_content_path(conn, "/share/pt/Show.S01") == "https://img.example/p2.jpg"


def test_percent_in_path_no_wildcard_mismatch(conn):
    """content_path 含 % 不当通配符 → 不误匹配其他路径（旧 LIKE 实现会误匹配）."""
    # 如果 % 被当 LIKE 通配符，"/share/pt/[100%.complete]" 会匹配到这条无关路径
    _seed(conn, "/share/pt/[100AAA.complete]/Other.mkv", "https://img.example/wrong.jpg")
    assert _poster_for_content_path(conn, "/share/pt/[100%.complete]") is None
    # 而真正在该目录下的文件应命中
    _seed(conn, "/share/pt/[100%.complete]/Real.mkv", "https://img.example/right.jpg")
    assert (
        _poster_for_content_path(conn, "/share/pt/[100%.complete]")
        == "https://img.example/right.jpg"
    )


def test_no_result_returns_none(conn):
    """无匹配（含 content_path 为空）返 None."""
    _seed(conn, "/share/pt/Unrelated.mkv", "https://img.example/p3.jpg")
    assert _poster_for_content_path(conn, "/share/pt/Missing") is None
    assert _poster_for_content_path(conn, None) is None
    assert _poster_for_content_path(conn, "") is None
