"""测试文件不许在模块顶层 import server。

`import server` 会真实装配 `tools/_runtime` 的全局——其中 `embedding_engine`
变成一个 enabled=True、但（测试环境是 dummy key）什么都查不出来的真引擎。

为什么这会静默炸掉别的测试：dream 的 feel 段按融合分挑选，向量可用时是
`0.7*向量 + 0.3*关键词`，不可用时关键词独自承担。继承到这么一个引擎，向量那路
恒为 0，门槛就变成事实上的 1.67 倍，**整段 feel 无声消失**——测试不报错，
只是断言的东西不见了。

而且 conftest 的 `_restore_tool_runtime` 挡不住这一种：它按测试快照再还原，
而模块顶层的 import 发生在**收集阶段**，早于任何 fixture，于是污染被当成基线
保留了下去。

所有既有测试文件都遵守「函数内导入 server」这条约定。这条用例把约定变成规矩，
因为违反它的代价是别处两条断言静默失效，而不是这里报错。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
PATTERN = re.compile(r"^(?:import\s+server\b|from\s+server\s+import\b)", re.M)


def _offenders() -> list[str]:
    bad = []
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        # 只看行首（顶层）；缩进过的是函数内导入，那正是我们要的写法
        if PATTERN.search(text):
            bad.append(path.name)
    return bad


def test_no_test_module_imports_server_at_top_level():
    offenders = _offenders()
    assert not offenders, (
        "这些测试文件在模块顶层 import server，会在收集阶段污染 tools/_runtime："
        f"{'、'.join(offenders)}。改成在测试函数内部导入。"
    )


@pytest.mark.parametrize("snippet", ["import server", "from server import trace"])
def test_the_guard_actually_catches_it(tmp_path, monkeypatch, snippet):
    """反面：确认这条守卫不是摆设。"""
    fake = tmp_path / "test_fake_offender.py"
    fake.write_text(f"{snippet}\n", encoding="utf-8")
    monkeypatch.setattr(
        "tests.test_no_module_level_server_import.TESTS", tmp_path, raising=False
    )
    import tests.test_no_module_level_server_import as guard

    monkeypatch.setattr(guard, "TESTS", tmp_path)
    assert guard._offenders() == ["test_fake_offender.py"]
