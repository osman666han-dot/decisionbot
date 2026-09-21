"""Юнит-тесты ядра без сети: python -m tests.test_core"""
import asyncio
import collections
import os
import tempfile

import config
from db import DB
from entropy import Engine
from entropy.mixer import build
from entropy.picker import pick
from entropy.sources import Source
from parsing import parse


def test_picker():
    base = build("1:1", 123, "wiki", ["a", "b", "c"])
    a = pick(base, 3)
    assert a == pick(base, 3), "детерминированность"
    cnt = collections.Counter(pick(build(f"u:{i}", i, "wiki", ["x"]), 3)[0] for i in range(9000))
    assert all(2700 < cnt[k] < 3300 for k in range(3)), cnt
    # ветка rejection sampling: n > 2**255, отбрасывается ~половина значений
    big = (1 << 255) + 12345
    for i in range(50):
        idx, _ = pick(f"t{i}", big)
        assert 0 <= idx < big
    for n in range(1, 11):
        assert 0 <= pick("z", n)[0] < n
    print("picker ok", dict(cnt))


def test_parse():
    p = parse("Что выпить?\nЧай\nКофе\nГазировка")
    assert p.ok and p.question == "Что выпить?" and p.options == ["Чай", "Кофе", "Газировка"]
    p = parse("Что выпить?\n1. Чай\n2) Кофе\n- Газировка\n• Сок")
    assert p.ok and p.options == ["Чай", "Кофе", "Газировка", "Сок"], p
    assert parse("Что выпить?\nЧай\nКофе").error == "few"
    assert parse("только вопрос").error == "few"
    assert parse("").error == "few"
    assert parse("Q\n" + "\n".join(str(i) + "x" for i in range(11))).error == "many"
    assert parse("Q\n" + "\n".join(["a" * 101, "b", "c"])).error == "long"
    assert parse("Q\n\n\nA\n\nB\n\nC\n").ok
    print("parse ok")


class Fake(Source):
    def __init__(self, name, delay, gap=0.01):
        super().__init__()
        self.name, self.delay, self.gap = name, delay, gap

    async def stream(self, session):
        await asyncio.sleep(self.delay)
        i = 0
        while True:
            self.publish(f"{self.name}:{i}")
            i += 1
            await asyncio.sleep(self.gap)


async def test_engine():
    fast, slow, dead = Fake("fast", 0.05), Fake("slow", 0.4), Fake("dead", 999)
    eng = Engine([slow, fast, dead])
    await eng.start()
    r = await eng.throw("1:1", 3, timeout=3, extra=2, extra_timeout=2)
    assert r and r.source == "fast" and len(r.events) == 3, r
    assert r.events == ["fast:0", "fast:1", "fast:2"] or r.events[0].startswith("fast:"), r.events
    assert 0 <= r.index < 3
    # после броска подписчиков не осталось
    assert all(len(s._subs) == 0 for s in eng.sources)
    # события только после запроса: у следующего броска счётчик продолжается, а не с нуля
    r2 = await eng.throw("1:2", 3, timeout=3, extra=2, extra_timeout=2)
    n0 = int(r2.events[0].split(":")[1])
    assert n0 > 2, r2.events
    await eng.stop()

    # все молчат -> None
    eng2 = Engine([Fake("a", 999), Fake("b", 999)])
    await eng2.start()
    assert await eng2.throw("2:1", 3, timeout=0.3) is None
    assert all(len(s._subs) == 0 for s in eng2.sources)
    await eng2.stop()

    # источник заговорил, но дал меньше событий, чем нужно: берём что есть
    once = Fake("once", 0.01, gap=999)
    eng3 = Engine([once])
    await eng3.start()
    r3 = await eng3.throw("3:1", 3, timeout=2, extra=2, extra_timeout=0.3)
    assert r3 and len(r3.events) == 1
    await eng3.stop()
    print("engine ok")


async def test_db():
    with tempfile.TemporaryDirectory() as d:
        db = DB(os.path.join(d, "sub", "t.db"))
        await db.init()
        await db.touch_user(1, "a")
        await db.touch_user(2, "b")
        await db.touch_user(1, "a2")
        await db.log_event(1, "start")
        await db.log_event(1, "sub_check_failed")
        await db.mark_converted(2)          # у 2 отказа не было, ничего не пишет
        await db.mark_converted(1)
        await db.mark_converted(1)          # второй раз не дублирует
        s = await db.stats()
        assert s["users_total"] == 2 and s["users_today"] == 2
        assert s["fail_users"] == 1 and s["pass_users"] == 1, s
        from db import today
        await db.save_throw(1, "1:1", "Q", ["a", "b", "c"], "ok", 1, "wiki", ["e"], 5, "h")
        await db.save_throw(1, "1:2", "Q", ["a", "b", "c"], "silent")
        assert await db.count_ok_throws(1, today()) == 1
        assert await db.next_throw_seq(1) == 3
        await db.kv_set("k", "v1"); await db.kv_set("k", "v2")
        assert await db.kv_get("k") == "v2" and await db.kv_get("zz") is None
        rows = await db.export_throws()
        assert len(rows) == 2 and rows[0]["chosen_text"] == "b"
        await db.close()
    print("db ok")


if __name__ == "__main__":
    test_picker()
    test_parse()
    asyncio.run(test_engine())
    asyncio.run(test_db())
    print("ALL CORE OK")
