import asyncio, json, types
import pytest
import training
from training import TrainingSession, _SESSIONS, _do_pick, _do_skip, _apply_custom_query, _show_next, build_queue
from storage import Storage
from learning import Learned

CAT = ["Дисплей для Samsung Galaxy A40 (A405F) в сборе с тачскрином Черный - Optima",
       "Дисплей для Samsung Galaxy A40 (A405F) в сборе с тачскрином Черный - Incell"]


class FakeMoba:
    async def search(self, q, n):
        return [{"url": f"/u{i}", "name": c, "price": 1000 + i} for i, c in enumerate(CAT)]


class FakeBot:
    def __init__(self): self.edits = []
    async def edit_message_text(self, **kw): self.edits.append(kw)


def make(tmp_path):
    st = Storage(str(tmp_path / "t.db")); asyncio.run(st.init())
    svc = types.SimpleNamespace(owner="anri", tg_id=1, bot=FakeBot(), storage=st, moba_client=FakeMoba(), learned=Learned([]))
    async def fb(task, url, name, decision):
        await st.add_feedback(owner="anri", shop_key=task["shop_key"], wb_quality="copy",
                              moba_quality_label=name.rsplit(" - ", 1)[-1], decision=decision,
                              query=json.loads(task["offer_json"])["last_query"])
    async def rl(): svc.learned = Learned(await st.feedback_rows("anri"))
    svc._feedback = fb; svc._reload_learned = rl
    return svc, st


def cards(n):
    return [{"nm_id": i, "title": "Дисплей для Samsung Galaxy A40 Копия"} for i in range(1, n + 1)]


def test_queue_skips_linked_and_unclear(tmp_path):
    svc, st = make(tmp_path)
    asyncio.run(st.save_link("shop2", 1, "/x", None, "x", None))
    q, unclear = asyncio.run(build_queue(svc, "shop2", cards(3) + [{"nm_id": 9, "title": "Чехол прозрачный"}]))
    assert [c["nm_id"] for c in q] == [2, 3] and unclear == 1


def test_session_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(training, "is_confident", lambda svc, r: False)  # без автопилота — проверяем ручной поток
    svc, st = make(tmp_path)
    async def run():
        s = TrainingSession(owner="anri", shop_key="shop2", shop_title="Анри", svc=svc, queue=cards(23), total=23, message_id=5)
        _SESSIONS["anri"] = s
        await _show_next(s)
        await _do_pick(s, 1)            # промах
        assert list(s.last) == [False]
        await _do_skip(s)               # не считается
        assert list(s.last) == [False]
        await _apply_custom_query(s, "samsung a40 incell")
        await _do_pick(s, 0)            # после своего запроса — промах
        assert list(s.last) == [False, False]
        for _ in range(20):
            await _do_pick(s, 0)
        assert all(s.last) and s.trained_shown
        assert "обучен" in svc.bot.edits[-1]["text"]
        # ссылки сохранены
        assert await st.get_link("shop2", 1) is not None
        # обучение: после выбора Incell первым? счёт incell растёт
        assert svc.learned.label_score("copy", "Optima") > 0  # №1 теперь самая дешёвая — Optima
        for t in s.prefetch.values(): t.cancel()
    asyncio.run(run())
    _SESSIONS.clear()


def test_callback_data_len():
    s = TrainingSession(owner="a", shop_key="s", shop_title="t", svc=None, queue=[], total=1)
    items = [{"url": "u", "name": "n" * 300, "price": 99999, "quality_label": "x" * 40, "frame": True, "recommended": True}] * 8
    _, kb = training._build_message(s, {"card": {"nm_id": 1, "title": "t"}, "result": {"items": items, "hidden": 3}})
    assert all(len(b.callback_data.encode()) <= 64 for row in kb.inline_keyboard for b in row)


def test_card_category():
    from training import card_category
    assert card_category({"title": "Дисплей для Samsung Galaxy A40 Копия"}) == "disp"
    assert card_category({"title": "Задняя крышка для iPhone 11 Premium", "subject": "Корпуса для телефонов"}) == "back"
    assert card_category({"title": "Аккумулятор для Samsung A40"}) == "batt"
    assert card_category({"title": "Шлейф зарядки Redmi 9A"}) == "parts"
    assert card_category({"title": "Защитное стекло"}) is None


def test_streaming_start(tmp_path, monkeypatch):
    svc, st = make(tmp_path)
    monkeypatch.setattr(training, "CARDS_CACHE_DIR", tmp_path)
    monkeypatch.setenv("WB_SHOP2", "tok")
    async def fake_fetch(token, progress=None, text_search=None, on_page=None):
        for page in range(3):
            await asyncio.sleep(0.05)
            await on_page([{"nm_id": page * 10 + i, "title": "Дисплей для Samsung Galaxy A40 Копия"} for i in range(5)])
        return []
    monkeypatch.setattr(training, "fetch_shop_cards", fake_fetch)

    class Msg:
        message_id = 5
        async def edit_text(self, *a, **k): pass
    cq = types.SimpleNamespace(message=Msg())

    async def run():
        await training._start_session(svc, cq, "shop2", "disp")
        s = _SESSIONS["anri"]
        assert s.current is not None          # первая карточка показана, не дожидаясь всех страниц
        await asyncio.sleep(0.3)
        assert s.total == 15 and not s.loading
        assert (tmp_path / "cards_shop2_disp.json").exists()
        for t in s.prefetch.values(): t.cancel()
    asyncio.run(run())
    _SESSIONS.clear()



def test_films_not_displays():
    from training import card_category
    assert card_category({"title": "Гидрогелевая пленка на экран iPhone 12 mini"}) is None
    assert card_category({"title": "Защитное стекло для дисплея Samsung A40"}) is None
    assert card_category({"title": "LCD дисплей для iPhone 13 (черный) incell"}) == "disp"



def test_autopilot(tmp_path):
    svc, st = make(tmp_path)
    async def run():
        # учим: Incell для копии — сильно
        for _ in range(6):
            await st.add_feedback(owner="anri", shop_key="shop2", wb_quality="copy", moba_quality_label="Incell", decision="confirmed")
        await svc._reload_learned()
        s = TrainingSession(owner="anri", shop_key="shop2", shop_title="Анри", svc=svc, queue=cards(25), total=25, message_id=5)
        _SESSIONS["anri"] = s
        await _show_next(s)
        assert s.auto_done == 9 and s.current["control"] is True   # 9 сам, 10-я — на контроль
        await _do_pick(s, 0)
        assert s.control_total == 1 and s.control_hits == 1
        assert await st.get_link("shop2", 1) is not None
        rows = await st.feedback_rows("anri")
        assert sum(r["decision"] == "auto" for r in rows) >= 9
        for t in s.prefetch.values(): t.cancel()
    asyncio.run(run())
    _SESSIONS.clear()



def test_skips_persist(tmp_path):
    svc, st = make(tmp_path)
    class Empty:
        async def search(self, q, n): return []
    async def run():
        s = TrainingSession(owner="anri", shop_key="shop2", shop_title="Анри", svc=svc, queue=cards(3), total=3, message_id=5)
        _SESSIONS["anri"] = s
        svc.moba_client = Empty()
        await _show_next(s)          # все 3 не найдены — бот сам их пропустил
        assert s.not_found == 3
        q, _ = await build_queue(svc, "shop2", cards(4))
        assert [c["nm_id"] for c in q] == [4]   # пропущенные больше не в очереди
        svc.moba_client = FakeMoba()
        s2 = TrainingSession(owner="anri", shop_key="shop2", shop_title="Анри", svc=svc, queue=q, total=1, message_id=5)
        _SESSIONS["anri"] = s2
        await _show_next(s2)
        await _do_skip(s2)
        q, _ = await build_queue(svc, "shop2", cards(4))
        assert q == []
    asyncio.run(run())
    _SESSIONS.clear()
