"""bug #12: TMDB search() 区分"瞬时错误"和"genuine no-results"。

瞬时错误（429 / auth / 网络）必须 raise ProviderUnavailable，不能返空列表
（否则一次 429 把可识别文件永久写成 needs_review）。
真没结果（200 + results=[]）仍返 []。
"""

from __future__ import annotations

import pytest

from services.metadata.base import ProviderUnavailable
from services.metadata.tmdb import TMDBProvider


def test_search_raises_provider_unavailable_on_transient_error():
    """movie+tv 两侧都 transient 失败且无候选 → raise ProviderUnavailable（不返 []）。"""
    p = TMDBProvider("fake-key")

    def boom(*a, **k):
        raise RuntimeError("TMDB rate limit exceeded (40 req / 10s)")

    p._get = boom
    with pytest.raises(ProviderUnavailable):
        p.search("Inception", media_type=None)


def test_search_genuine_no_results_returns_empty():
    """200 + results=[] 是真没结果 → 返 []，不 raise。"""
    p = TMDBProvider("fake-key")
    p._get = lambda *a, **k: {"results": []}
    assert p.search("Nonexistent Title Xyz", media_type="movie") == []


def test_search_partial_success_returns_candidates_despite_one_side_error():
    """tv 侧失败但 movie 侧有结果 → 仍返候选（部分成功不算不可用）。"""
    p = TMDBProvider("fake-key")
    calls = {"n": 0}

    def mixed(path, **k):
        calls["n"] += 1
        if path == "/search/movie":
            return {"results": [{"id": 27205, "title": "Inception", "release_date": "2010-07-16"}]}
        raise RuntimeError("tv side 500")

    p._get = mixed
    cands = p.search("Inception", media_type=None)
    assert len(cands) == 1
    assert cands[0].external_ids["tmdb_id"] == "27205"
