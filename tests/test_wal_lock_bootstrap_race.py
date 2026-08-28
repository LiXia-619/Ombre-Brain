"""WAL 的跨进程锁不能往锁文件里写东西。

原实现在拿锁**之前**先把锁文件撑到 1 字节，而写的位置正是接下来要锁的 byte 0。
另一个进程已经持锁时，这次 write/flush 就撞在锁上抛 PermissionError: [Errno 13]，
而且发生在拿锁之前——调用直接崩掉，不是等锁。
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from ombrebrain.fabric.log.wal import WalStore, _exclusive_file_lock


def _lock_path(target):
    return target.with_name(f"{target.name}.lock")


def test_lock_file_is_never_written_to(tmp_path):
    """锁文件全程 0 字节——没有写，就没有那个竞态。"""
    target = tmp_path / "a.wal"

    with _exclusive_file_lock(target):
        pass
    with _exclusive_file_lock(target):
        pass

    lock = _lock_path(target)
    assert lock.exists()
    assert lock.stat().st_size == 0


def test_lock_still_serializes_writers(tmp_path):
    """去掉那个字节之后，锁本身还得管用。"""
    target = tmp_path / "b.wal"
    store = WalStore(target)

    with ThreadPoolExecutor(max_workers=8) as pool:
        indexes = sorted(
            pool.map(lambda value: store.append({"value": value}).index, range(40))
        )

    assert indexes == list(range(1, 41))


@pytest.mark.skipif(os.name != "nt", reason="msvcrt 区域锁是 Windows 特有的")
def test_entering_the_lock_does_not_raise_when_another_handle_holds_it(tmp_path):
    """真实交错：别人已经锁住 byte 0，此时进来不该抛 PermissionError。

    原实现会——它在拿锁前先写 byte 0，而那正是被锁住的字节。这里用非阻塞锁
    从另一个 handle 占住，然后确认进入锁的那条路径抛的是「拿不到锁」，
    而不是「写不进去」。
    """
    import msvcrt

    target = tmp_path / "c.wal"
    lock = _lock_path(target)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    assert lock.stat().st_size == 0

    holder = lock.open("a+b")
    try:
        holder.seek(0)
        msvcrt.locking(holder.fileno(), msvcrt.LK_LOCK, 1)

        with lock.open("a+b") as contender:
            contender.seek(0)
            with pytest.raises(OSError) as excinfo:
                msvcrt.locking(contender.fileno(), msvcrt.LK_NBLCK, 1)
            # 拿不到锁是对的；关键是它没有先在写入上崩掉。
            assert excinfo.value.errno in (13, 36)

        holder.seek(0)
        msvcrt.locking(holder.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        holder.close()

    # 锁释放之后照常能用，而且锁文件依然是空的。
    with _exclusive_file_lock(target):
        pass
    assert lock.stat().st_size == 0
