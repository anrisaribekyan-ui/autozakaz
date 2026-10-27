# discover_suppliers.py
"""
Разведка poisk123.ru и master-mobile.ru: только чтение, ничего не заказывает.
Сохраняет HTML и скриншоты в debug/suppliers/ и пакует в debug/suppliers.zip.

Запуск: venv/bin/python discover_suppliers.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

OUT = Path("debug/suppliers")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PAGES = {
    "p123_camon12air": "https://poisk123.ru/s/moskva/?s=tecno+camon+12+air",
    "p123_camon12air_p2": "https://poisk123.ru/s/moskva/page-2.html?s=tecno+camon+12+air",
    "p123_a54": "https://poisk123.ru/s/moskva/?s=samsung+a54",
    "mm_a54_product": "https://master-mobile.ru/catalog/zapchasti_dlya_telefonov_1/displei/67454/",
    "mm_search_a54": "https://master-mobile.ru/search/?q=a54",
    "mm_catalog_displays": "https://master-mobile.ru/catalog/zapchasti_dlya_telefonov_1/displei/",
}


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"}) as c:
        for name, url in PAGES.items():
            try:
                r = await c.get(url)
                (OUT / f"{name}.http.html").write_text(r.text, "utf-8")
                report[name] = {"http": r.status_code, "final_url": str(r.url), "len": len(r.text)}
            except Exception as exc:  # noqa: BLE001
                report[name] = {"http_error": str(exc)}
            print(name, report[name])

    async with async_playwright() as pw:
        br = await pw.chromium.launch(headless=True)
        ctx = await br.new_context(user_agent=UA, locale="ru-RU", viewport={"width": 1400, "height": 1000})
        page = await ctx.new_page()
        reqs: list[dict] = []
        page.on("request", lambda rq: reqs.append({"m": rq.method, "u": rq.url,
                                                    "post": (rq.post_data or "")[:500]})
                if rq.resource_type in ("xhr", "fetch", "document") else None)
        for name, url in PAGES.items():
            try:
                await page.goto(url, wait_until="networkidle", timeout=45000)
                (OUT / f"{name}.pw.html").write_text(await page.content(), "utf-8")
                await page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
                report.setdefault(name, {})["pw"] = "ok"
            except Exception as exc:  # noqa: BLE001
                report.setdefault(name, {})["pw_error"] = str(exc)
        # poisk123: пробуем кликнуть фильтр «Митинский р-к»
        try:
            await page.goto(PAGES["p123_camon12air"], wait_until="networkidle", timeout=45000)
            el = page.get_by_text("Митинский", exact=False).first
            await el.click(timeout=10000)
            await page.wait_for_load_state("networkidle", timeout=30000)
            report["p123_mitino_click"] = {"url": page.url}
            (OUT / "p123_camon12air_mitino.pw.html").write_text(await page.content(), "utf-8")
            await page.screenshot(path=str(OUT / "p123_camon12air_mitino.png"), full_page=True)
        except Exception as exc:  # noqa: BLE001
            report["p123_mitino_click"] = {"error": str(exc)}
        (OUT / "requests.json").write_text(json.dumps(reqs, ensure_ascii=False, indent=1), "utf-8")
        await br.close()

    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    z = shutil.make_archive("debug/suppliers", "zip", root_dir="debug", base_dir="suppliers")
    print("ГОТОВО:", z)


if __name__ == "__main__":
    asyncio.run(main())
