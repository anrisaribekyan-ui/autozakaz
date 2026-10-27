"""Разведка Liberti, шаг 2 (только чтение, ничего не кладёт в корзину и не оформляет):
скрипты сайта (как работает «В корзину»), страница поиска, корзина и страница оформления заказа.
Запуск: venv/bin/python discover_liberti2.py  → data/liberti_disc/
"""
from __future__ import annotations

import os
import re

import httpx
from dotenv import load_dotenv

from discover_liberti import BASE, DEFAULT_URL, OUT, UA, save


def main() -> None:
    load_dotenv(".env")
    login, pwd = os.getenv("LIBERTI_LOGIN", "").strip(), os.getenv("LIBERTI_PASSWORD", "").strip()
    with httpx.Client(base_url=BASE, follow_redirects=True, timeout=30,
                      headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"}) as c:
        c.get("/")
        r = c.post("/?login=yes", data={"AUTH_FORM": "Y", "TYPE": "AUTH", "backurl": "/", "USER_LOGIN": login,
                                        "USER_PASSWORD": pwd, "USER_REMEMBER": "Y", "Login": "Войти"})
        print("вход:", "ок" if re.search(r"logout=yes|Выйти", r.text) else "НЕ ВОШЛИ")
        p = c.get(DEFAULT_URL).text
        for src in re.findall(r'<script[^>]*src="(/local/templates/liberti2018/(?:build|js)/[^"]+)"', p):
            name = "js_" + src.split("/")[-1].split("?")[0]
            t = c.get(src).text
            save(name, t)
            print("скрипт", name, len(t), "| basket:", len(re.findall(r"basket", t, re.I)))
        for name, url in (("05_search.html", "/search/?q=BM52"), ("06_cart.html", "/personal/cart/"),
                          ("07_order.html", "/personal/order/make/"), ("08_cart2.html", "/basket/"),
                          ("09_order2.html", "/order/")):
            r = c.get(url)
            save(name, r.text)
            print(name, r.status_code, str(r.url), len(r.text))
    print("сохранено в", OUT)


if __name__ == "__main__":
    main()
