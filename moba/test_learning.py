import asyncio, json, types
from learning import Learned, norm_label
from matcher import find_candidates


def row(label, decision, wbq="copy", query=None, models=("redmi 9a",), ts="2026-10-15T10:00:00"):
    return {"wb_quality": wbq, "moba_quality_label": label, "decision": decision, "query": query,
            "wb_brand": "xiaomi", "wb_models": json.dumps(list(models)), "ts": ts}


def test_norm_label():
    assert norm_label("Optima") == "optima"
    assert norm_label("(Оптима)") == "optima"
    assert norm_label("OR (SP)") == "or (sp)"
    assert norm_label("(OLED) (Full Size) (Full HD, 500 Кд/м²)") == "oled"


def test_blocked_and_unblocked():
    rows = [row("(OLED)", "rejected")] * 3
    assert Learned(rows).is_blocked("copy", "OLED")
    assert not Learned(rows + [row("OLED", "confirmed")]).is_blocked("copy", "OLED")


def test_scores_order():
    L = Learned([row("Optima", "chosen"), row("Optima", "confirmed"), row("Incell", "passed")])
    assert L.label_score("copy", "Optima") > L.label_score("copy", "Incell")


def test_model_queries():
    L = Learned([row("Optima", "chosen", query="дисплей для redmi 9a optima"),
                 row("Incell", "rejected", query="плохой запрос")])
    assert L.model_queries("xiaomi", "redmi 9a") == ["дисплей для redmi 9a optima"]


CAT = ["Дисплей для Xiaomi Redmi 9A/9C/10A в сборе с тачскрином Черный - Optima",
       "Дисплей для Xiaomi Redmi 9A/9C/10A в сборе с тачскрином Черный - (OLED)",
       "Дисплей для Xiaomi Redmi 9A/9C/10A в сборе с тачскрином Черный - Incell"]


class FakeMoba:
    async def search(self, q, n):
        return [{"url": f"/u{i}", "name": c} for i, c in enumerate(CAT)]
    async def get_product(self, url):
        i = int(url[2:]); return types.SimpleNamespace(price=[900, 700, 1100][i], article="a", product_id=i, name=CAT[i])
    async def mitino_status(self, url):
        return "yes"


def test_learning_never_allows_oled_for_copy_and_ranks():
    L = Learned([row("(OLED)", "confirmed")] * 5 + [row("Incell", "confirmed")] * 2)
    res = asyncio.run(find_candidates(FakeMoba(), "Дисплей для Xiaomi Redmi 9A/9C/10A Копия", learned=L))
    names = [it["name"] for it in res["items"]]
    assert not any("OLED" in n for n in names)
    # правило копии: самая дешёвая (Optima 900 < Incell 1100), OLED не показывается
    assert names[0].endswith("Optima") and res["items"][0]["recommended"] is True
