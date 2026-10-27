"""Разведка Liberti, шаг 4 (только чтение): каталог запчастей для телефонов страницами.
Цель — брать «В наличии N шт.» (для выбранного склада Митино) пачкой. Сверяет 3 товара с карточкой."""
import os
import re

import httpx
from dotenv import load_dotenv

from discover_liberti import BASE, UA, save

load_dotenv(".env")


def items(t):
    out = []
    for blk in re.split(r'class="product-list-item[^"]*"\s+data-id="', t)[1:]:
        pid = re.match(r"(\d+)", blk)
        art = re.search(r"Артикул:\s*([0-9A-Za-z-]+)", blk)
        q = re.search(r"В наличии\s*(\d+)\s*шт|(Нет в наличии|Под заказ|Много)", blk)
        href = re.search(r'href="(/[^"]+\.html)"', blk)
        out.append((pid and pid.group(1), art and art.group(1), q and (q.group(1) or q.group(2)), href and href.group(1)))
    return out


with httpx.Client(base_url=BASE, follow_redirects=True, timeout=60,
                  headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"}) as c:
    c.get("/")
    c.post("/?login=yes", data={"AUTH_FORM": "Y", "TYPE": "AUTH", "backurl": "/", "USER_LOGIN": os.getenv("LIBERTI_LOGIN"),
                                "USER_PASSWORD": os.getenv("LIBERTI_PASSWORD"), "USER_REMEMBER": "Y", "Login": "Войти"})
    base = "/catalog/zapchasti/zapchasti-dlya-telefonov/"
    p = c.get(base)
    save("12_phones.html", p.text)
    subs = sorted(set(re.findall(r'href="(' + re.escape(base) + r'[^"#?]+/)"', p.text)))
    print("подразделов:", len(subs), subs[:8])
    tgt = next((u for u in subs if "akkum" in u or "akb" in u), subs[0] if subs else base)
    for q in ("", "?PAGEN_1=2", "?count=100", "?view=table", "?display=table&count=200"):
        r = c.get(tgt + q)
        it = items(r.text)
        pag = sorted(set(re.findall(r'PAGEN_\d+=(\d+)', r.text)), key=int)[-3:]
        print(f"{tgt}{q}: {r.status_code} товаров {len(it)} | страниц до {pag} | пример {it[:3]}")
        save("13_" + re.sub(r"\W", "_", q or "p1") + ".html", r.text)
    # сверка с карточкой
    it = [x for x in items(c.get(tgt).text) if x[3]][:3]
    for pid, art, q, href in it:
        t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c.get(href).text))
        m = re.search(r'Наличие на складе "?Либерти\s*-\s*Митино"?\s*:\s*(\S+ ?\S*)', t)
        print(f"  {art}: в списке «{q}» · на карточке Митино: {m.group(1) if m else 'не нашёл'}")
