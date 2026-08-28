"""模型能用的能力，必须在它看得见的地方写着。

工具的 docstring 就是模型唯一的说明书——MCP 把它作为 tool description 发过去。
一个参数存在于签名、却在 docstring 里一个字都没有，等于这个能力只有读源码的人
知道。审计时撞到的实例：`trace` 有一整套删除请求审批流程
（deletion_request_id / deletion_decision / deletion_ai_reason），
1897 字的 docstring 里没提，INTERNALS.md 和 README.md 里也没有。

这条测试守的是**能力可发现性**，不是文档格式。所以判定放得很宽：参数名在
docstring 里以任何形式出现过就算数。豁免名单只放那些「名字本身就是说明」或
「不给模型用」的，每个都要写明理由——豁免名单变长就是这条规矩在松动。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent / "src" / "server.py"

# 参数名 -> 为什么可以不在 docstring 里出现
EXEMPT = {
    # 单参数工具，docstring 通篇都在说这一个东西
    ("anchor", "bucket_id"): "工具只有这一个参数，正文即说明",
    ("release", "bucket_id"): "同上",
    ("pulse", "include_archive"): "正文有「include_archive=True 同时返回归档区」",
    # 主参数，工具名与正文已经说清
    ("plan", "content"): "正文通篇在讲写什么内容",
    ("letter_write", "content"): "同上",
    ("breath_search", "query"): "正文通篇在讲检索什么",
    # 不给模型用的内部开关
    ("hold", "test_data"): "测试数据标记，不是给模型的能力",
    ("grow", "test_data"): "同上",
}


def _tools() -> list[ast.AsyncFunctionDef]:
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    return [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any("mcp.tool" in ast.unparse(d) for d in node.decorator_list)
    ]


def test_registered_tool_count_is_stable():
    """工具数变了就该有人来改这个数字，顺便重读一遍上面的豁免名单。"""
    assert len(_tools()) == 16


@pytest.mark.parametrize("tool", _tools(), ids=lambda t: t.name)
def test_every_parameter_is_mentioned_in_the_docstring(tool):
    doc = ast.get_docstring(tool) or ""
    assert doc.strip(), f"{tool.name} 没有 docstring —— 模型收到的说明是空的"

    missing = [
        arg.arg
        for arg in tool.args.args
        if (tool.name, arg.arg) not in EXEMPT
        and not re.search(rf"\b{re.escape(arg.arg)}\b", doc)
    ]

    assert not missing, (
        f"{tool.name} 的这些参数在 docstring 里一个字都没提：{'、'.join(missing)}。"
        "模型能传，却无从知道什么时候该传。要么补进 docstring，"
        "要么在 EXEMPT 里写明为什么不需要。"
    )
