"""services.path_resolver — QNAP symlink alias 翻译测试."""

from __future__ import annotations

import pytest

from services import path_resolver


@pytest.fixture(autouse=True)
def _clear_cache():
    """每个测试前后清 alias cache, 避免 cross-test 污染."""
    path_resolver.invalidate()
    yield
    path_resolver.invalidate()


# Real QNAP `ls -la /share` 输出样本 (basis for parse).
_QNAP_LS_SAMPLE = """total 0
drwxrwxrwt 33 admin administrators  980 Apr 30 23:15 .
drwxr-xr-x 22 admin administrators  600 May 17 01:28 ..
drwxrwxrwx 18 admin administrators 4096 May 21 12:35 CACHEDEV1_DATA
drwxrwxrwx 48 admin administrators 4096 Apr 16 17:19 CACHEDEV2_DATA
lrwxrwxrwx  1 admin administrators   24 Apr 16 17:14 downloads -> CACHEDEV2_DATA/downloads
lrwxrwxrwx  1 admin administrators   16 Apr 16 17:14 pt -> CACHEDEV2_DATA/pt
lrwxrwxrwx  1 admin administrators   20 Apr 16 17:14 Moives -> CACHEDEV2_DATA/Moives
lrwxrwxrwx  1 admin administrators   22 Apr 16 17:14 TV show -> CACHEDEV2_DATA/TV show
lrwxrwxrwx  1 admin administrators   12 Apr 16 17:14 Public -> Public
lrwxrwxrwx  1 admin administrators   30 May  5 10:00 abs_link -> /share/CACHEDEV2_DATA/abs_target
drwxrwxrwx  2 admin administrators   40 Sep 19  2005 HDA_DATA
"""


def test_parse_ls_symlinks_basic():
    aliases = path_resolver.parse_ls_symlinks(_QNAP_LS_SAMPLE, "/share")
    assert aliases["/share/downloads"] == "/share/CACHEDEV2_DATA/downloads"
    assert aliases["/share/pt"] == "/share/CACHEDEV2_DATA/pt"
    assert aliases["/share/Moives"] == "/share/CACHEDEV2_DATA/Moives"


def test_parse_ls_symlinks_filename_with_space():
    aliases = path_resolver.parse_ls_symlinks(_QNAP_LS_SAMPLE, "/share")
    # "TV show" filename 含空格 — target 也含空格
    assert aliases["/share/TV show"] == "/share/CACHEDEV2_DATA/TV show"


def test_parse_ls_symlinks_absolute_target():
    aliases = path_resolver.parse_ls_symlinks(_QNAP_LS_SAMPLE, "/share")
    # 绝对路径 target 不应再拼 parent
    assert aliases["/share/abs_link"] == "/share/CACHEDEV2_DATA/abs_target"


def test_parse_ls_symlinks_skips_dirs_and_regular_files():
    aliases = path_resolver.parse_ls_symlinks(_QNAP_LS_SAMPLE, "/share")
    # 目录不进 alias
    assert "/share/CACHEDEV2_DATA" not in aliases
    assert "/share/HDA_DATA" not in aliases
    assert "/share/." not in aliases
    assert "/share/.." not in aliases


def test_parse_ls_symlinks_empty_input():
    assert path_resolver.parse_ls_symlinks("", "/share") == {}


def test_parse_ls_symlinks_no_symlinks():
    only_dirs = "drwxrwxrwx 2 admin administrators 40 Sep 19 2005 HDA_DATA\n"
    assert path_resolver.parse_ls_symlinks(only_dirs, "/share") == {}


def _fake_ssh_ok(cmd, timeout=10):
    """模拟 ssh_exec 返 QNAP ls 输出."""
    return (0, _QNAP_LS_SAMPLE, "")


def _fake_ssh_fail(cmd, timeout=10):
    return (1, "", "permission denied")


def _fake_ssh_raises(cmd, timeout=10):
    raise OSError("ssh connection refused")


def test_get_aliases_caches_first_call():
    calls = [0]

    def ssh(cmd, timeout=10):
        calls[0] += 1
        return _fake_ssh_ok(cmd, timeout)

    a1 = path_resolver.get_aliases(ssh)
    a2 = path_resolver.get_aliases(ssh)
    assert a1 == a2
    assert calls[0] == 1  # 第二次走 cache, 不 SSH


def test_get_aliases_force_refresh():
    calls = [0]

    def ssh(cmd, timeout=10):
        calls[0] += 1
        return _fake_ssh_ok(cmd, timeout)

    path_resolver.get_aliases(ssh)
    path_resolver.get_aliases(ssh, force=True)
    assert calls[0] == 2


def test_get_aliases_ssh_fail_returns_empty():
    a = path_resolver.get_aliases(_fake_ssh_fail)
    assert a == {}


def test_get_aliases_ssh_raises_returns_empty():
    a = path_resolver.get_aliases(_fake_ssh_raises)
    assert a == {}


@pytest.mark.parametrize(
    "input_path,expected",
    [
        # 完整命中 symlink alias prefix
        ("/share/downloads/foo.mkv", "/share/CACHEDEV2_DATA/downloads/foo.mkv"),
        ("/share/pt/movie/bar.mkv", "/share/CACHEDEV2_DATA/pt/movie/bar.mkv"),
        # filename 含空格
        ("/share/TV show/S01E01.mkv", "/share/CACHEDEV2_DATA/TV show/S01E01.mkv"),
        # 输入恰好等于 alias root (无 trailing /)
        ("/share/downloads", "/share/CACHEDEV2_DATA/downloads"),
        # 已是 canonical → 原样返
        ("/share/CACHEDEV2_DATA/downloads/foo.mkv", "/share/CACHEDEV2_DATA/downloads/foo.mkv"),
        # 完全不相关 path → 原样返
        ("/etc/passwd", "/etc/passwd"),
        ("", ""),
        # prefix 看似匹配但是非 separator (downloads vs downloadsX) — 不应误替
        ("/share/downloadsX/file.mkv", "/share/downloadsX/file.mkv"),
    ],
)
def test_resolve_paths(input_path, expected):
    assert path_resolver.resolve(input_path, _fake_ssh_ok) == expected


def test_resolve_many_batches():
    paths = [
        "/share/downloads/a.mkv",
        "/share/pt/b.mkv",
        "/share/CACHEDEV2_DATA/already_canonical.mkv",
        "/etc/passwd",
    ]
    expected = [
        "/share/CACHEDEV2_DATA/downloads/a.mkv",
        "/share/CACHEDEV2_DATA/pt/b.mkv",
        "/share/CACHEDEV2_DATA/already_canonical.mkv",
        "/etc/passwd",
    ]
    assert path_resolver.resolve_many(paths, _fake_ssh_ok) == expected


def test_resolve_longest_prefix_wins():
    """如果有 /share + /share/downloads 同时存在, 应取后者 (longest prefix)."""
    custom_ls = (
        "lrwxrwxrwx 1 admin admin 5 May 1 12:00 . -> /short_root\n"
        "lrwxrwxrwx 1 admin admin 20 May 1 12:00 downloads -> CACHEDEV2_DATA/downloads\n"
    )

    def ssh(cmd, timeout=10):
        return (0, custom_ls, "")

    # ". -> /short_root" 这种 mock 实际会被 parse 接受为 /share/. -> /short_root, 防止 ambiguous, 我们假设它和 downloads 都存在.
    # 真正测的: /share/downloads/x 不会被 /share/. 错配.
    result = path_resolver.resolve("/share/downloads/x", ssh)
    assert result == "/share/CACHEDEV2_DATA/downloads/x"


def test_invalidate_clears_cache():
    path_resolver.get_aliases(_fake_ssh_ok)
    assert path_resolver.snapshot() is not None
    path_resolver.invalidate()
    assert path_resolver.snapshot() is None


def test_resolve_when_no_aliases_loaded():
    """SSH 返空 (无 symlink) → resolve 返原样."""

    def ssh(cmd, timeout=10):
        return (0, "drwxrwxrwx 2 admin admin 40 Sep 19 2005 HDA_DATA\n", "")

    assert path_resolver.resolve("/share/downloads/foo", ssh) == "/share/downloads/foo"
