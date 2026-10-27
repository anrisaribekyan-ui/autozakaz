"""Разведка сайта Liberti под оптовым логином (только чтение): вход + одна страница товара.

Сохраняет HTML в data/liberti_disc/ и печатает, что нашёл: вошли ли, есть ли «Митино», оптовая цена, наличие.
Ничего не покупает и в корзину не кладёт.
Запуск: venv/bin/python discover_liberti.py [ссылка_на_товар]
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

BASE = "https://liberti.ru"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
OUT = Path("data/liberti_disc")
DEFAULT_URL = BASE + "/lcd_displey_dlya_samsung_galaxy_a14_sm_a145_v_ramke_chernyy_100_or.html"


def save(name: str, text: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(text, "utf-8")


def around(text: str, word: str, n: int = 3, width: int = 160) -> list[str]:
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"\s+", " ", plain)
    out = []
    for m in re.finditer(re.escape(word), plain, re.I):
        out.append(plain[max(0, m.start() - width): m.end() + width])
        if len(out) >= n:
            break
    return out


def main() -> None:
    load_dotenv(".env")
    login, pwd = os.getenv("LIBERTI_LOGIN", "").strip(), os.getenv("LIBERTI_PASSWORD", "").strip()
    if not login or not pwd:
        print("нет LIBERTI_LOGIN / LIBERTI_PASSWORD в .env")
        return
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    with httpx.Client(base_url=BASE, follow_redirects=True, timeout=30,
                      headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"}) as c:
        r = c.get("/")
        save("01_home.html", r.text)
        print("главная:", r.status_code, len(r.text))
        forms = re.findall(r"<form[^>]*>.*?</form>", r.text, re.S | re.I)
        auth = [f for f in forms if re.search(r"USER_LOGIN|USER_PASSWORD|password", f, re.I)]
        print("форм входа на главной:", len(auth))
        if auth:
            save("02_auth_form.html", auth[0])
        # стандартный вход Битрикса
        data = {"AUTH_FORM": "Y", "TYPE": "AUTH", "backurl": "/", "USER_LOGIN": login,
                "USER_PASSWORD": pwd, "USER_REMEMBER": "Y", "Login": "Войти"}
        for f in auth[:1]:
            for name, val in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', f, re.I):
                if name not in ("USER_LOGIN", "USER_PASSWORD"):
                    data.setdefault(name, val)
        action = "/?login=yes"
        if auth:
            m = re.search(r'action="([^"]+)"', auth[0], re.I)
            if m:
                action = m.group(1).replace("&amp;", "&")
        r = c.post(action, data=data)
        save("03_after_login.html", r.text)
        logged = bool(re.search(r"logout=yes|Выйти|Личный кабинет", r.text, re.I))
        print("вход:", r.status_code, "→ вошли" if logged else "→ НЕ видно, что вошли", "| action", action)
        for s in around(r.text, "неверн", 2):
            print("   !", s)
        r = c.get(url)
        save("04_product.html", r.text)
        print("товар:", r.status_code, url)
        for w in ("Митино", "Оптов", "наличи", "склад", "ajax"):
            hits = around(r.text, w, 3)
            print(f"\n[{w}] {len(hits)} совп.")
            for h in hits:
                print("   …", h)
        js = sorted(set(re.findall(r'["\'](/(?:local|ajax|bitrix)/[^"\']+?\.php[^"\']*)["\']', r.text)))
        print("\najax-адреса на странице:", js[:20])
    print("\nсохранено в", OUT)


if __name__ == "__main__":
    main()
