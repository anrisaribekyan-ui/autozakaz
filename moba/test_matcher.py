# test_matcher.py
"""
Тесты matcher.py (pytest, без сети).
"""

from __future__ import annotations

import pytest

from matcher import (
    allowed,
    build_queries,
    classify_moba,
    model_matches,
    parse_wb_title,
)


# ---------------------------------------------------------------------- #
# parse_wb_title
# ---------------------------------------------------------------------- #
def test_parse_samsung_copy():
    wb = parse_wb_title("Дисплей для Samsung Galaxy A40 Копия")
    assert wb["type"] == "дисплей"
    assert wb["brand"] == "samsung"
    assert wb["models"] == ["a40"]
    assert wb["quality"] == "copy"


def test_parse_xiaomi_multi():
    wb = parse_wb_title("Дисплей для Xiaomi Redmi 9A/9C/10A Копия")
    assert wb["brand"] == "xiaomi"
    assert wb["models"] == ["redmi 9a", "redmi 9c", "redmi 10a"]
    assert wb["quality"] == "copy"


def test_parse_oled():
    wb = parse_wb_title("Дисплей для Samsung Galaxy S21 OLED")
    assert wb["quality"] == "oled"
    assert wb["models"] == ["s21"]


def test_parse_original():
    wb = parse_wb_title("Дисплей для Samsung Galaxy A40 Оригинал")
    assert wb["quality"] == "original"


def test_parse_apple():
    wb = parse_wb_title("Дисплей для Apple iPhone 11 Копия")
    assert wb["brand"] == "apple"
    assert wb["models"] == ["11"]


# ---------------------------------------------------------------------- #
# classify_moba
# ---------------------------------------------------------------------- #
def test_classify_original_frame_true():
    cls = classify_moba(
        "Дисплей для Samsung Galaxy S26 (S942B) модуль с рамкой "
        "Черный - OR (SP)"
    )
    assert cls["quality"] == "original"
    assert cls["frame"] is True


def test_classify_oled_frame_false():
    cls = classify_moba(
        "Дисплей для Samsung Galaxy S26 (S942B) в сборе с тачскрином "
        "Черный - (OLED) (Full Size)"
    )
    assert cls["quality"] == "oled"
    assert cls["frame"] is False


def test_classify_copy_optima():
    cls = classify_moba(
        "Дисплей для Samsung Galaxy A40 (A405F) в сборе с тачскрином "
        "Черный - Optima"
    )
    assert cls["quality"] == "copy"


def test_classify_incell():
    cls = classify_moba(
        "Дисплей для Samsung Galaxy A40 (A405F) в сборе с тачскрином "
        "Черный - Incell"
    )
    assert cls["quality"] == "copy"


# ---------------------------------------------------------------------- #
# model_matches
# ---------------------------------------------------------------------- #
def test_model_matches_a40():
    wb = parse_wb_title("Дисплей для Samsung Galaxy A40 Копия")
    label = (
        "Дисплей для Samsung Galaxy A40 (A405F) в сборе с тачскрином "
        "Черный - Optima"
    )
    assert model_matches(label, wb) is True


def test_model_matches_a40s_no():
    wb = parse_wb_title("Дисплей для Samsung Galaxy A40 Копия")
    label = (
        "Дисплей для Samsung Galaxy A40s (A405F) в сборе с тачскрином "
        "Черный - Optima"
    )
    assert model_matches(label, wb) is False


def test_model_matches_a405f_no():
    wb = parse_wb_title("Дисплей для Samsung Galaxy A40 Копия")
    label = (
        "Дисплей для Samsung Galaxy A405F в сборе с тачскрином "
        "Черный - Optima"
    )
    assert model_matches(label, wb) is False


def test_model_matches_wrong_type():
    wb = parse_wb_title("Дисплей для Samsung Galaxy A40 Копия")
    label = (
        "Аккумулятор для Samsung Galaxy A40 (A405F) - Optima"
    )
    assert model_matches(label, wb) is False


def test_model_matches_xiaomi_redmi_9a():
    wb = parse_wb_title("Дисплей для Xiaomi Redmi 9A Копия")
    label = (
        "Дисплей для Xiaomi Redmi 9A (M2006C3) в сборе с тачскрином "
        "Черный - Optima"
    )
    assert model_matches(label, wb) is True


def test_model_matches_xiaomi_redmi_9at_no():
    wb = parse_wb_title("Дисплей для Xiaomi Redmi 9A Копия")
    label = (
        "Дисплей для Xiaomi Redmi 9AT (M2006C3) в сборе с тачскрином "
        "Черный - Optima"
    )
    assert model_matches(label, wb) is False


# ---------------------------------------------------------------------- #
# allowed
# ---------------------------------------------------------------------- #
def test_allowed_copy():
    assert allowed("copy", "copy") is True
    assert allowed("copy", "unknown") is True
    assert allowed("copy", "oled") is False
    assert allowed("copy", "original") is False


def test_allowed_oled():
    assert allowed("oled", "oled") is True
    assert allowed("oled", "unknown") is True
    assert allowed("oled", "copy") is False
    assert allowed("oled", "original") is False


def test_allowed_original():
    assert allowed("original", "original") is True
    assert allowed("original", "unknown") is True
    assert allowed("original", "copy") is False
    assert allowed("original", "oled") is False


# ---------------------------------------------------------------------- #
# build_queries
# ---------------------------------------------------------------------- #
def test_build_queries_copy_has_optima():
    wb = parse_wb_title("Дисплей для Samsung Galaxy A40 Копия")
    qs = build_queries(wb)
    assert any("optima" in q for q in qs)
    assert any("incell" in q for q in qs)
    assert any("standard" in q for q in qs)
    assert len(qs) <= 8
    assert len(qs) == len(set(qs))


def test_build_queries_xiaomi_no_brand_dup():
    wb = parse_wb_title("Дисплей для Xiaomi Redmi 9A Копия")
    qs = build_queries(wb)
    # бренд "xiaomi" не должен дублироваться перед подбрендом redmi
    assert not any("xiaomi redmi" in q for q in qs)
    assert any("redmi 9a" in q for q in qs)


def test_build_queries_oled_no_optima():
    wb = parse_wb_title("Дисплей для Samsung Galaxy S21 OLED")
    qs = build_queries(wb)
    assert not any("optima" in q for q in qs)
    assert not any("incell" in q for q in qs)
    assert not any("standard" in q for q in qs)


# ---- дополнительные проверки (ревью) ----
def test_suffix_models_do_not_match():
    wb = parse_wb_title("Дисплей для Samsung Galaxy S26 Копия")
    assert model_matches("Дисплей для Samsung Galaxy S26 Ultra (S948B) модуль с рамкой Черный - OR (SP)", wb) is False
    assert model_matches("Дисплей для Samsung Galaxy S26+ (S947B) модуль с рамкой Черный - OR (SP)", wb) is False
    assert model_matches("Дисплей для Samsung Galaxy S26 (S942B) модуль с рамкой Черный - OR (SP)", wb) is True


def test_iphone_pro_not_11():
    wb = parse_wb_title("Дисплей для Apple iPhone 11 Копия")
    assert model_matches("Дисплей для iPhone 11 Pro в сборе с тачскрином Черный - Incell", wb) is False
    assert model_matches("Дисплей для iPhone 11 в сборе с тачскрином Черный - Incell", wb) is True
    assert any(q.startswith("дисплей iphone 11") for q in build_queries(wb))


def test_redmi_multi_label():
    wb = parse_wb_title("Дисплей для Xiaomi Redmi 9A/9C/10A Копия")
    assert model_matches("Дисплей для Xiaomi Redmi 9A/9C/10A в сборе с тачскрином Черный - Optima", wb) is True
    assert model_matches("Дисплей для Xiaomi Redmi 9C NFC в сборе - Optima", wb) is True
    assert model_matches("Дисплей для Xiaomi Redmi 9 в сборе - Optima", wb) is False


def test_redmi_note_vs_note_pro():
    wb = parse_wb_title("Дисплей для Xiaomi Redmi Note 8 Копия")
    assert model_matches("Дисплей для Xiaomi Redmi Note 8 Pro в сборе - Optima", wb) is False
    assert model_matches("Дисплей для Xiaomi Redmi Note 8 (M1908C3JH) в сборе - Optima", wb) is True


def test_user_query_redmi():
    wb = parse_wb_title("дисплей для redmi 9a")
    assert wb["brand"] == "xiaomi" and wb["models"] == ["redmi 9a"]
    wb = parse_wb_title("дисплей для samsung a40")
    assert wb["models"] == ["a40"]


def test_battery_only_bc():
    import asyncio
    from matcher import find_candidates
    class M:
        async def search(self, q, n):
            return [{"url": "/a", "name": "Аккумулятор для Samsung Galaxy A40 (EB-BA405ABE) - Premium", "price": 500},
                    {"url": "/b", "name": "Аккумулятор для Samsung Galaxy A40 (EB-BA405ABE) - BC", "price": 700}]
    # правило 18.10: на WB «… BC» — на moba любой АКБ, самый дешёвый
    r = asyncio.run(find_candidates(M(), "АКБ Samsung Galaxy A40 BC", show_all=True, check_stock=False))
    assert [i["url"] for i in r["items"]] == ["/a", "/b"]
    from matcher import moba_skip_reason
    assert moba_skip_reason("АКБ Samsung Galaxy A40 BC") is None
    assert moba_skip_reason("АКБ Samsung EB-BA217ABY") is not None
    assert moba_skip_reason("Дисплей для Samsung A40 Копия") is None


def test_back_cover_rules():
    import asyncio
    from matcher import find_candidates
    names = {"/p": ("Задняя крышка для iPhone 13 Pro Max Золото - Премиум", 900),
             "/n": ("Задняя крышка для iPhone 13 Pro Max (узкое отверстие) Золото", 300),
             "/w": ("Задняя крышка для iPhone 13 Pro Max (с широким отверстием) Золото", 500),
             "/c": ("Задняя крышка для iPhone 13 Pro Max Синий", 200)}
    class M:
        async def search(self, q, n):
            return [{"url": u, "name": v[0], "price": v[1]} for u, v in names.items()]
    r = asyncio.run(find_candidates(M(), "Задняя крышка для iPhone 13 Pro Max Золото", check_stock=False))
    assert [i["url"] for i in r["items"]] == ["/w"]
    r = asyncio.run(find_candidates(M(), "Задняя крышка для iPhone 13 Pro Max узкое Золото", check_stock=False))
    assert [i["url"] for i in r["items"]] == ["/n"]
    r = asyncio.run(find_candidates(M(), "Задняя крышка для iPhone 13 Pro Max Премиум Золото", check_stock=False))
    assert [i["url"] for i in r["items"]] == ["/p"]


def test_lcd_prefix_type():
    wb = parse_wb_title("LCD дисплей для iPhone 13 (черный) incell")
    assert wb["type"] == "дисплей" and wb["models"] == ["13"] and wb["quality"] == "copy"
    assert model_matches("Дисплей для iPhone 13 в сборе с тачскрином Черный - (In-Cell)", wb)
    assert not model_matches("Дисплей для iPhone 13 Pro Max в сборе - (In-Cell)", wb)
    assert build_queries(wb)[0] == "дисплей iphone 13"


def test_slash_combo_models():
    wb = parse_wb_title("Дисплей Apple IPhone 8 с тачскрином (Модуль) белый")
    assert wb["models"] == ["8"]
    assert model_matches("Дисплей для iPhone 8/SE 2020 в сборе с тачскрином Белый - Premium", wb)
    assert not model_matches("Дисплей для iPhone 8 Plus в сборе с тачскрином Белый - Premium", wb)
    assert not model_matches("Дисплей для iPhone SE 2020 в сборе - Premium", wb)


def test_slash_expansion():
    wb = parse_wb_title("Дисплей для Xiaomi Redmi 9C Копия")
    assert model_matches("Дисплей для Xiaomi Redmi 9A/9C/10A в сборе - Optima", wb)
    assert not model_matches("Дисплей для Xiaomi Redmi 9A/9AT в сборе - Optima", wb)
    wb = parse_wb_title("Дисплей для Samsung Galaxy A31 Копия")
    assert model_matches("Дисплей для Samsung Galaxy A30s/A31 в сборе - Incell", wb)


def test_wb_or_in_brackets():
    assert parse_wb_title("Дисплей Honor 10 Lite/10i/20e (OR в рамке)")["quality"] == "original"
    assert parse_wb_title("Дисплей Huawei Honor 10i / Honor 10 Lite черный (OR)")["quality"] == "original"
    assert parse_wb_title("Дисплей для Samsung Galaxy A40 Копия")["quality"] == "copy"
    assert parse_wb_title("Дисплей для Motorola Moto G Копия")["quality"] == "copy"


def test_brand_must_match():
    assert not model_matches("Дисплей для Xiaomi Redmi Note 9S/9 Pro в сборе - OR", parse_wb_title("Дисплей Huawei Honor 9S/Y5p (DUA-LX9) Черный - OR"))
    assert not model_matches("Дисплей для Itel A60s/A60 в сборе - OR", parse_wb_title("Дисплей OPPO A60 (CPH2631) Original"))
    assert not model_matches("Дисплей для Tecno Spark 10 4G/10C в сборе - Оптима", parse_wb_title("Дисплей Realme 10 4G (RMX3630) с рамкой Черный (In-Cell)"))
    assert model_matches("Дисплей для Huawei Honor X9/X30 в сборе - OR", parse_wb_title("Дисплей Honor X9/X30 Original"))
    assert model_matches("Дисплей для iPhone 13 в сборе - Стандарт", parse_wb_title("Дисплей IPhone 13 (In-Cell)"))
    assert model_matches("Дисплей для Tecno Spark 30 Pro/Infinix Hot 50 Pro в сборе", parse_wb_title("Дисплей Infinix Hot 50 Pro (In-Cell)"))


def test_color():
    from matcher import detect_color
    assert detect_color("Дисплей Apple IPhone 7 белый") == "white"
    assert detect_color("Дисплей для iPhone 7 в сборе с тачскрином Черный - Оптима") == "black"
    from matcher import colors_compatible
    c = detect_color("модуль с рамкой Серебро - OR")
    assert colors_compatible(c, "silver") and colors_compatible(c, "white")
    assert not colors_compatible("white", detect_color("Серебристый"))
    assert detect_color("Дисплей для Samsung - Сервисный Оригинал") is None


def test_frame_required():
    import asyncio
    from matcher import find_candidates
    class M:
        async def search(self, q, n):
            return [{"url": "/a", "name": "Дисплей для Huawei Honor X6b (JDY-LX1) в сборе с тачскрином Черный - OR", "price": 900},
                    {"url": "/b", "name": "Дисплей для Huawei Honor X6b (JDY-LX1) модуль с рамкой Черный - OR", "price": 1500}]
    r = asyncio.run(find_candidates(M(), "Дисплей Huawei Honor X6b (JDY-LX1) с рамкой Черный - OR", check_stock=False))
    assert [i["url"] for i in r["items"]] == ["/b"]
    r = asyncio.run(find_candidates(M(), "Дисплей Huawei Honor X6b (JDY-LX1) Черный - OR", check_stock=False))
    assert r["items"][0]["url"] == "/a"


def test_hd_is_other_model():
    wb = parse_wb_title("Дисплей Infinix Smart 7 / Tecno Spark 10 4G черный (OR)")
    assert not model_matches("Дисплей для Infinix Smart 7 HD (X6516) в сборе с тачскрином Черный - OR", wb)
    assert model_matches("Дисплей для Infinix Smart 7 (X6515) модуль с рамкой Черный - OR", wb)
    wb = parse_wb_title("Дисплей Infinix Smart 9 (X6532) OR")
    assert model_matches("Дисплей для Infinix Smart 9/9 HD (X6532/X6532C) в сборе - OR", wb)


def test_or100_strict():
    import asyncio
    from matcher import find_candidates
    A = {"url": "/a", "name": "Дисплей для Huawei Nova 11i (MAO-LX9N) в сборе с тачскрином Черный - OR (Full HD)", "price": 900}
    B = {"url": "/b", "name": "Дисплей для Huawei Nova 11i (MAO-LX9N) в сборе с тачскрином Черный - OR100 (Full HD)", "price": 1300}
    class M:
        def __init__(self, items): self.items = items
        async def search(self, q, n): return self.items
    r = asyncio.run(find_candidates(M([A, B]), "Дисплей Huawei Nova 11i (MAO-LX9N) Черный - OR100", check_stock=False))
    assert [i["url"] for i in r["items"]] == ["/b"]
    r = asyncio.run(find_candidates(M([A]), "Дисплей Huawei Nova 11i (MAO-LX9N) Черный - OR100", check_stock=False))
    assert [i["url"] for i in r["items"]] == ["/a"]


def test_year_and_codes():
    from matcher import codes_conflict
    wb = parse_wb_title("Дисплей Realme C11 (RMX2185) с рамкой Черный (Original)")
    assert not model_matches("Дисплей для Realme C21/C20/C11 2021/Narzo 50i (RMX3201/RMX3231) модуль с рамкой Черный - OR", wb)
    assert model_matches("Дисплей для Realme C11 (RMX2185) модуль с рамкой Черный - OR", wb)
    assert codes_conflict("Дисплей Realme C11 (RMX2185)", "Дисплей для Realme C11 2021 (RMX3231)")
    assert not codes_conflict("Дисплей Infinix Hot 40 (X6836)", "Дисплей для Infinix Hot 40 (X6836) в сборе")
    assert model_matches("Дисплей для iPhone 8/SE (2020)/SE (2022) в сборе - Оптима", parse_wb_title("Дисплей Apple IPhone 8 белый"))


def test_copy_cheapest():
    import asyncio
    from matcher import find_candidates
    class M:
        async def search(self, q, n):
            return [{"url": "/full", "name": "Дисплей для iPhone 13 (A2635) в сборе с тачскрином Черный - (In-Cell, Full HD) (с верификацией)", "price": 4200},
                    {"url": "/hd", "name": "Дисплей для iPhone 13 (A2635) в сборе с тачскрином Черный - (In-Cell, HD COG, 350 Кд/м²) Стандарт", "price": 2600},
                    {"url": "/or", "name": "Дисплей для iPhone 13 (A2635) в сборе с тачскрином Черный - OR (SP)", "price": 9000}]
    r = asyncio.run(find_candidates(M(), "Дисплей IPhone 13 (In-Cell)", check_stock=False))
    assert [i["url"] for i in r["items"]] == ["/hd", "/full"]


def test_parts_rules():
    import asyncio
    from matcher import find_candidates
    names = {"/a": ("Шлейф для Samsung A50 (A505F) на кнопки громкости с компонентами", 300),
             "/b": ("Шлейф для Samsung A50 (A505F) на кнопки громкости", 120),
             "/c": ("Шлейф для Samsung A50 (A505F) на кнопки громкости - Оригинал", 450),
             "/d": ("Шлейф для Samsung A50 (A505F) межплатный", 90),
             "/p": ("Шлейф для Samsung A50 (A505F) плата системный разъем/микрофон", 150)}
    class M:
        async def search(self, q, n):
            return [{"url": u, "name": v[0], "price": v[1]} for u, v in names.items()]
    f = lambda t: [i["url"] for i in asyncio.run(find_candidates(M(), t, check_stock=False))["items"]]
    assert f("Шлейф для Samsung A50 на кнопки громкости") == ["/b"]
    assert f("Шлейф для Samsung A50 на кнопки громкости Премиум") == ["/a", "/c"]
    assert f("Шлейф межплатный Samsung A50") == ["/d"]
    assert f("Нижняя плата Samsung A50") == ["/p"]


def test_camera_speaker_rules():
    import asyncio
    from matcher import find_candidates
    names = {"/m": ("Камера для Samsung A50 (A505F) основная 25 MP", 400),
             "/f": ("Камера для Samsung A50 (A505F) фронтальная", 200),
             "/t": ("Динамик для Samsung A50 (A505F) слуховой (верхний)", 90),
             "/b": ("Звонок (buzzer) для Samsung A50 (A505F) в сборе", 120)}
    class M:
        async def search(self, q, n):
            return [{"url": u, "name": v[0], "price": v[1]} for u, v in names.items()]
    f = lambda t: [i["url"] for i in asyncio.run(find_candidates(M(), t, check_stock=False))["items"]]
    assert f("Камера основная для Samsung A50") == ["/m"]
    assert f("Фронтальная камера Samsung A50") == ["/f"]
    assert f("Спикер Samsung A50") == ["/t"]
    assert f("Бузер Samsung A50") == ["/b"]
