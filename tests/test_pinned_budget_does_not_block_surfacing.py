from __future__ import annotations

import pytest

from tools.breath._verbatim import render_stored_bucket


def _core(bucket_id, content):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {"type": "permanent", "importance": 10, "pinned": True, "domain": []},
    }


def _ordinary(bucket_id, content, importance=10):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "type": "dynamic",
            "importance": importance,
            "activation_count": 1,
            "domain": [],
        },
    }


def _cost(bucket, header):
    return render_stored_bucket(bucket, header, "👣 Footprint：暂时无法读取")[1]


@pytest.mark.asyncio
async def test_ordinary_memories_still_surface_when_a_core_rule_is_too_big(monkeypatch):
    from tests.test_breath_verbatim_patch import OrderedBucketManager, _install_runtime
    from tools.breath.surface import surface_default

    fits = _core("fits", "Core rule that fits.")
    huge = _core("huge", "Oversized core rule " * 400)
    ordinary = _ordinary("ordinary", "An ordinary memory that fits the leftovers.")

    _install_runtime(OrderedBucketManager([fits, huge, ordinary]))
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 1.0)

    budget = _cost(fits, "📌 [核心准则] [bucket_id:fits]") + _cost(
        ordinary, "[权重:10.00] [bucket_id:ordinary]"
    )
    output = await surface_default(max_results=1, max_tokens=budget, tag_filter=[])

    assert "[bucket_id:fits]" in output
    assert "[bucket_id:ordinary]" in output
    assert "[bucket_id:huge]" not in output
    assert "token 预算不足" in output


@pytest.mark.asyncio
async def test_the_omitted_core_rule_is_still_reported(monkeypatch):
    from tests.test_breath_verbatim_patch import OrderedBucketManager, _install_runtime
    from tools.breath.surface import surface_default

    fits = _core("fits", "Core rule that fits.")
    huge = _core("huge", "Oversized core rule " * 400)
    ordinary = _ordinary("ordinary", "An ordinary memory that fits the leftovers.")

    _install_runtime(OrderedBucketManager([fits, huge, ordinary]))
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 1.0)

    budget = _cost(fits, "📌 [核心准则] [bucket_id:fits]") + _cost(
        ordinary, "[权重:10.00] [bucket_id:ordinary]"
    )
    output = await surface_default(max_results=1, max_tokens=budget, tag_filter=[])

    assert "omitted=1" in output
    assert "surfacing.breath_max_tokens" in output
    assert "普通浮现已跳过" not in output


@pytest.mark.asyncio
async def test_core_rules_still_get_the_budget_first(monkeypatch):
    from tests.test_breath_verbatim_patch import OrderedBucketManager, _install_runtime
    from tools.breath.surface import surface_default

    core = _core("core", "Core rule wins the budget.")
    ordinary = _ordinary("ordinary", "Ordinary memory must not crowd it out.")

    _install_runtime(OrderedBucketManager([core, ordinary]))
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 1.0)

    only_one_fits = _cost(core, "📌 [核心准则] [bucket_id:core]")
    output = await surface_default(max_results=5, max_tokens=only_one_fits, tag_filter=[])

    assert "[bucket_id:core]" in output
    assert "[bucket_id:ordinary]" not in output


# ---- Dashboard 诊断 ----

class _FakeManager:
    def __init__(self, buckets):
        self._buckets = buckets

    async def list_all(self, include_archive=False):
        return list(self._buckets)


async def _report(monkeypatch, buckets, limit):
    import web._shared as sh
    import web.system as system

    monkeypatch.setattr(sh, "bucket_mgr", _FakeManager(buckets), raising=False)
    monkeypatch.setattr(sh, "config", {"surfacing": {"breath_max_tokens": limit}}, raising=False)
    return await system._pinned_budget_report()


@pytest.mark.asyncio
async def test_report_counts_only_pinned_buckets(monkeypatch):
    report = await _report(
        monkeypatch,
        [_core("a", "rule"), _core("b", "rule"), _ordinary("c", "not pinned")],
        10000,
    )
    assert report["pinned_count"] == 2
    assert report["limit_tokens"] == 10000
    assert report["required_tokens"] > 0


@pytest.mark.asyncio
async def test_report_is_zero_without_pinned_buckets(monkeypatch):
    report = await _report(monkeypatch, [_ordinary("c", "not pinned")], 10000)
    assert report["pinned_count"] == 0
    assert report["required_tokens"] == 0


@pytest.mark.asyncio
async def test_report_notices_when_pins_exceed_the_budget(monkeypatch):
    report = await _report(monkeypatch, [_core("a", "rule " * 5000)], 1000)
    assert report["required_tokens"] > report["limit_tokens"]
    assert report["largest_entry_tokens"] > report["limit_tokens"]
