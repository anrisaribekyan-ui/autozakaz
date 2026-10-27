"""Разведка Liberti, шаг 3 (только чтение): можно ли брать наличие Митино оптом.
Сохраняет страницу «Прайс-лист», страницу каталога АКБ и пробует размер страницы/выгрузки.
Запуск: venv/bin/python discover_liberti3.py → data/liberti_disc/
"""
import os
import re

import httpx
from dotenv import load_dotenv

from discover_liberti import BASE, UA, save

load_dotenv(".env")
with httpx.Client(base_url=BASE, follow_redirects=True, timeout=60,
                  headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"}) as c:
    c.get("/")
    r = c.post("/?login=yes", data={"AUTH_FORM": "Y", "TYPE": "AUTH", "backurl": "/",
                                    "USER_LOGIN": os.getenv("LIBERTI_LOGIN"), "USER_PASSWORD": os.getenv("LIBERTI_PASSWORD"),
                                    "USER_REMEMBER": "Y", "Login": "Войти"})
    print("вход:", "ок" if "logout=yes" in r.text or "Выйти" in r.text else "НЕТ")
    home = r.text
    links = sorted(set(re.findall(r'href="(/[^"#?]*(?:price|prais|прайс)[^"]*)"', home, re.I)))
    print("ссылки на прайс:", links[:10])
    for i, u in enumerate(links[:3]):
        p = c.get(u)
        save(f"10_price_{i}.html", p.text)
        print(u, p.status_code, len(p.text), "| xls/csv ссылки:",
              sorted(set(re.findall(r'href="([^"]+\.(?:xlsx?|csv|zip|xml)[^"]*)"', p.text, re.I)))[:10])
    cats = re.findall(r'href="(/catalog/[^"#?]+/)"', home)
    akb = next((u for u in cats if "akb" in u.lower() or "akkum" in u.lower()), cats[0] if cats else None)
    print("каталог:", akb)
    if akb:
        for q in ("", "?PAGEN_1=1&count=200", "?SHOWALL_1=1"):
            p = c.get(akb + q)
            n = len(re.findall(r'product-list-item product-list-item--table"\s+data-id=', p.text))
            avail = re.findall(r'В наличии\s*(\d+)\s*шт', p.text)
            save(f"11_catalog{q.replace('?', '_').replace('&', '_').replace('=', '')}.html", p.text)
            print(f"  {akb}{q}: {p.status_code}, товаров на странице {n}, «В наличии N шт.»: {avail[:8]}")
