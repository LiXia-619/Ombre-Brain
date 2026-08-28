"""trace 的删除审批路径失败时，也必须是 isError=True。

这个仓库已经把理由写在 test_mcp_tools_docker_integration.py 里了：

  「为什么不能用返回字符串表达这类失败：那在客户端是一次正常返回，调用方
   （通常是模型自己）会以为写成功了继续往下走，等下次去翻，那条记忆从来
   没存在过。」

test_trace_failure_is_error.py 为 trace 的常规路径立了这条规矩，但
`deletion_request_id` / `deletion_decision` 那条分支在工具最顶端就短路返回了，
既没走 `_with_notice`，也没被那份用例覆盖——于是同一个工具里，一半路径守规矩，
另一半不守。
"""

from __future__ import annotations

import pytest

# server 一律在函数内导入。模块顶层导入会在**收集阶段**就真实装配 tools/_runtime
# 的全局（embedding_engine 会变成 enabled=True 但查不出东西的真引擎），而 conftest
# 的还原 fixture 是按测试快照的，收集阶段发生的污染会被当成基线原样保留下去。
# 后果是 dream 的 feel 段整段静默消失——今天刚修过一次的那个 bug。
# 其他测试文件都遵守这条，我第一版没遵守，当场把它复现了出来。

失败用例 = [
    ("请求 id 不存在", {"deletion_request_id": "no-such-id", "deletion_decision": "approve"}),
    ("决定值非法", {"deletion_request_id": "any", "deletion_decision": "赞成"}),
    ("只给决定不给 id", {"deletion_decision": "approve"}),
]


@pytest.mark.parametrize("说明, 参数", 失败用例, ids=[c[0] for c in 失败用例])
@pytest.mark.asyncio
async def test_删除审批失败必须抛错(说明, 参数, monkeypatch):
    import server

    async def decide(*_args, **_kwargs):
        return {"ok": False, "error": "pending request not found"}

    monkeypatch.setattr(server.deletion_requests, "decide", decide)

    with pytest.raises(Exception) as excinfo:
        await server.trace(bucket_id="whatever", **参数)

    # 抛什么类型不重要，重要的是它不能作为「正常返回」交给模型。
    assert not isinstance(excinfo.value, AssertionError)


@pytest.mark.asyncio
async def test_删除审批成功照旧返回正文(monkeypatch):
    """反面。没有这一条，上面全部可以靠「这条分支一律抛错」作弊通过。"""
    import server

    async def decide(*_args, **_kwargs):
        return {"ok": True, "decision": "approved", "bucket_id": "bucket123"}

    monkeypatch.setattr(server.deletion_requests, "decide", decide)

    out = await server.trace(
        bucket_id="bucket123",
        deletion_request_id="req-1",
        deletion_decision="approve",
    )

    assert "approved" in out
    assert "bucket123" in out
    assert "req-1" in out
