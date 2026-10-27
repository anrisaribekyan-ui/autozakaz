"""Отчёт о закупке: короткое сообщение в Telegram (HTML) + подробный отчёт-документ (HTML-файл).

Сообщение — «что сделать»: что забрать, что купить руками, что проверить, итог по деньгам.
Документ — всё по каждому заказу: что куплено, где, по какой цене, прибыль, куда перенесён, что не так.
"""
from __future__ import annotations

import html
from datetime import datetime
from typing import Any, Optional

from unit_econ import profit

E = html.escape


def _rub(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return ("−" if v < 0 else "") + f"{abs(float(v)):,.0f}".replace(",", " ") + " ₽"


def _price(x: dict) -> Optional[float]:
    return x.get("price_club") or x.get("price")


def _short(s: str, n: int = 60) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------------- #
# Разбор строк пачки по группам
# ---------------------------------------------------------------------- #
def groups(lines: list[dict], mm_res: Any) -> dict:
    mm_ok = mm_res is not None and getattr(mm_res, "status", "") == "placed"
    g = {"moba": [], "mm": [], "mm_manual": [], "manual": [], "none": []}
    for ln in lines:
        if ln.get("bought"):
            g["moba"].append(ln)
        elif ln.get("mm_pick"):
            (g["mm"] if mm_ok or ln.get("bought_mm") else g["mm_manual"]).append(ln)
        else:
            ext = ln.get("elsewhere") or {}
            (g["manual"] if ext.get("mitino") else g["none"]).append(ln)
    return g


def _cost(ln: dict) -> Optional[float]:
    if ln.get("bought"):
        return (ln.get("moba") or {}).get("price")
    if ln.get("mm_pick"):
        return _price(ln["mm_pick"])
    first = ((ln.get("elsewhere") or {}).get("mitino") or [None])[0]
    return (first.get("opt") or _price(first)) if first else None


def totals(lines: list[dict]) -> tuple[float, float]:
    sale = prof = 0.0
    for ln in lines:
        p = profit(ln.get("sale"), _cost(ln), ln["shop"]["key"])
        if p.get("profit") is not None:
            sale += ln["sale"]
            prof += p["profit"]
    return sale, prof


# ---------------------------------------------------------------------- #
# Telegram: короткое сообщение (parse_mode=HTML)
# ---------------------------------------------------------------------- #
def tg_summary(owner_title: str, lines: list[dict], mres: dict, mm_res: Any, live: bool,
               wb_errors: list[str], when: datetime) -> str:
    g = groups(lines, mm_res)
    out = [f"🛒 <b>Закупка · {E(owner_title)} · {when:%d.%m %H:%M}</b>" + ("" if live else " <i>(пробно)</i>")]
    if not lines:
        out.append("Новых заказов нет.")
        if wb_errors:
            out.append("⚠️ WB: " + E("; ".join(wb_errors)))
        return "\n".join(out)

    n_ok = len(g["moba"]) + len(g["mm"])
    n_hand = len(g["mm_manual"]) + len(g["manual"])
    out.append(f"Заказов: <b>{len(lines)}</b> · куплено {n_ok} · руками {n_hand} · нет нигде {len(g['none'])}")
    bought = g["moba"] + g["mm"]
    sale, prof = totals(bought)
    if sale:
        out.append(f"💰 Куплено на продажу {_rub(sale)} → прибыль ≈ <b>{_rub(prof)}</b> ({round(prof / sale * 100)}%)")

    # Забрать
    pick = []
    st = mres.get("status")
    if st == "placed":
        pick.append(f"• moba №{E(str(mres.get('order_number')))} — {len(g['moba'])} шт · {_rub(mres.get('total'))}")
    elif st == "dry_run_ok":
        pick.append(f"• moba (пробно) — {len(g['moba'])} шт · {_rub(mres.get('total'))}")
    if g["mm"] and mm_res is not None:
        num = getattr(mm_res, "order_number", None) or "—"
        pick.append(f"• Мастер Мобайл, пав. 535 №{E(str(num))} — {len(g['mm'])} шт · {_rub(getattr(mm_res, 'total', None))}")
    if pick:
        out.append("\n📦 <b>Забрать в Митино</b>")
        out += pick

    # Сделать руками
    todo = []
    for ln in g["mm_manual"]:
        x = ln["mm_pick"]
        todo.append(f"Мастер Мобайл: {E(_short(x.get('name', ''), 70))} — <b>{_rub(_price(x))}</b>"
                    f" <i>({E(ln['shop']['title'])} №{ln['wb_order_id']})</i>")
    for ln in g["manual"]:
        first = ln["elsewhere"]["mitino"][0]
        where = first.get("shop") or first.get("supplier") or "?"
        pr = first.get("opt") or _price(first)
        todo.append(f"{E(where)}: {E(_short(first.get('name', ''), 70))} — <b>{_rub(pr)}</b>"
                    f" <i>({E(ln['shop']['title'])} №{ln['wb_order_id']})</i>")
    if todo:
        out.append("\n👉 <b>Купить руками</b>")
        out += [f"{i}. {t}" for i, t in enumerate(todo, 1)]

    # Проверить / нет нигде
    warn = []
    if st == "unknown":
        warn.append("moba: заказ мог уйти — проверь на сайте")
    elif st not in ("placed", "dry_run_ok", "empty", None) and g["moba"] == [] and mres.get("problems"):
        warn.append("moba не оформлено: " + E("; ".join(mres["problems"])[:200]))
    if mm_res is not None and getattr(mm_res, "status", "") == "unknown":
        warn.append("Мастер Мобайл: заказ мог уйти — проверь в кабинете")
    elif g["mm_manual"] and mm_res is not None and getattr(mm_res, "problems", None):
        warn.append("Мастер Мобайл не оформился сам: " + E("; ".join(mm_res.problems)[:200]))
    for ln in g["none"]:
        warn.append(f"Нет нигде в Митино: {E(_short(ln['title'], 60))} <i>({E(ln['shop']['title'])} №{ln['wb_order_id']})</i>")
    for ln in lines:
        for p in ln.get("problems") or []:
            if "не перенёсся в сборку" in p:
                warn.append(f"WB №{ln['wb_order_id']}: {E(p[:150])}")
    if wb_errors:
        warn.append("WB: " + E("; ".join(wb_errors)[:200]))
    if warn:
        out.append("\n⚠️ <b>Проверить</b>")
        out += [f"• {w}" for w in warn]
    out.append("\n📄 Подробно по каждому заказу — в файле ниже.")
    return "\n".join(out)


# ---------------------------------------------------------------------- #
# Документ: HTML-отчёт (открывается в Telegram на телефоне)
# ---------------------------------------------------------------------- #
_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2129;--muted:#6b7280;--line:#e5e7eb;--ok:#15803d;--okbg:#e8f5ec;
--warn:#b45309;--warnbg:#fdf3e1;--bad:#b91c1c;--badbg:#fdecec;--blue:#1d4ed8;--bluebg:#e8eefc}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--card:#1b1e23;--text:#e7e9ee;--muted:#9aa1ad;--line:#2b2f36;
--ok:#4ade80;--okbg:#14301f;--warn:#fbbf24;--warnbg:#33270f;--bad:#f87171;--badbg:#3a1717;--blue:#93b4ff;--bluebg:#18233d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:16px}
.wrap{max-width:760px;margin:0 auto}h1{font-size:22px;margin:0 0 4px}.sub{color:var(--muted);margin-bottom:16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:20px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px}
.kpi b{display:block;font-size:20px;font-variant-numeric:tabular-nums}.kpi span{color:var(--muted);font-size:13px}
h2{font-size:17px;margin:24px 0 8px;display:flex;gap:8px;align-items:center}
.tag{font-size:12px;font-weight:600;padding:2px 8px;border-radius:99px}
.ok{background:var(--okbg);color:var(--ok)}.warn{background:var(--warnbg);color:var(--warn)}
.bad{background:var(--badbg);color:var(--bad)}.blue{background:var(--bluebg);color:var(--blue)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:8px}
.top{display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.wb{font-weight:600}.meta{color:var(--muted);font-size:13px}.buy{margin-top:6px}
.buy a{color:var(--blue);text-decoration:none}.price{font-weight:700;white-space:nowrap;font-variant-numeric:tabular-nums}
.econ{margin-top:6px;font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
.econ .p{font-weight:700;color:var(--ok)}.econ .m{font-weight:700;color:var(--bad)}
.note{margin-top:6px;font-size:13px;padding:6px 8px;border-radius:8px;background:var(--warnbg);color:var(--warn)}
ul.alt{margin:6px 0 0;padding-left:18px;font-size:13px}ul.alt a{color:var(--blue);text-decoration:none}.empty{color:var(--muted)}
"""


def _econ(ln: dict, cost: Optional[float]) -> str:
    p = profit(ln.get("sale"), cost, ln["shop"]["key"])
    if p.get("profit") is None:
        return f'<div class="econ">Продали {_rub(ln.get("sale"))}</div>' if ln.get("sale") else ""
    parts = " − ".join(f"{E(n)} {_rub(-v)}" for n, v in p["parts"])
    cls = "p" if p["profit"] >= 0 else "m"
    src = f" ({E(ln.get('sale_src', ''))})" if ln.get("sale_src") else ""
    return (f'<div class="econ">Продали {_rub(ln["sale"])}{src} − {parts} = '
            f'<span class="{cls}">{_rub(p["profit"])} ({p["margin_pct"]}%)</span></div>')


def _card(ln: dict, buy_html: str, price: Optional[float], extra: str = "") -> str:
    supply = f' · в поставке «{E(ln["supply"])}»' if ln.get("supply") else ""
    return (f'<div class="card"><div class="top"><div class="wb">{E(ln["title"])}</div>'
            f'<div class="price">{_rub(price)}</div></div>'
            f'<div class="meta">{E(ln["shop"]["title"])} · WB №{ln["wb_order_id"]}{supply}</div>'
            f'<div class="buy">{buy_html}</div>{_econ(ln, price)}{extra}</div>')


def _link(name: str, url: Optional[str]) -> str:
    return f'<a href="{E(url)}">{E(name)}</a>' if url else E(name)


def html_report(owner_title: str, lines: list[dict], mres: dict, mm_res: Any, live: bool,
                wb_errors: list[str], when: datetime) -> str:
    g = groups(lines, mm_res)
    bought = g["moba"] + g["mm"]
    sale, prof = totals(bought)
    spent = sum((_cost(ln) or 0) for ln in bought)
    h = [f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
         f'<meta name="viewport" content="width=device-width,initial-scale=1">'
         f'<title>Закупка {E(owner_title)} {when:%d.%m %H:%M}</title><style>{_CSS}</style></head><body><div class="wrap">',
         f'<h1>Закупка · {E(owner_title)}</h1><div class="sub">{when:%d.%m.%Y %H:%M}'
         + ("" if live else " · пробный запуск, ничего не оформлено") + '</div>',
         '<div class="kpis">',
         f'<div class="kpi"><b>{len(lines)}</b><span>новых заказов</span></div>',
         f'<div class="kpi"><b>{len(bought)}</b><span>куплено</span></div>',
         f'<div class="kpi"><b>{len(g["mm_manual"]) + len(g["manual"])}</b><span>купить руками</span></div>',
         f'<div class="kpi"><b>{len(g["none"])}</b><span>нет нигде</span></div>',
         f'<div class="kpi"><b>{_rub(spent)}</b><span>потрачено</span></div>',
         f'<div class="kpi"><b>{_rub(prof) if sale else "—"}</b><span>прибыль ≈'
         + (f" {round(prof / sale * 100)}%" if sale else "") + '</span></div></div>']

    if g["moba"]:
        st = mres.get("status")
        head = (f'moba №{E(str(mres.get("order_number")))} · {_rub(mres.get("total"))}' if st == "placed"
                else f'moba · {_rub(mres.get("total"))}')
        h.append(f'<h2>✅ Куплено на moba <span class="tag ok">{head}</span></h2>'
                 '<div class="meta" style="margin:-4px 0 8px">Забрать в Митино</div>')
        for ln in g["moba"]:
            m = ln["moba"]
            h.append(_card(ln, "moba: " + _link(m.get("name") or "", m.get("url")), m.get("price")))
    if g["mm"]:
        num = getattr(mm_res, "order_number", None)
        h.append(f'<h2>✅ Куплено в Мастер Мобайле <span class="tag ok">№{E(str(num or "—"))} · '
                 f'{_rub(getattr(mm_res, "total", None))}</span></h2>'
                 '<div class="meta" style="margin:-4px 0 8px">Забрать в Митино, пав. 535, оплата при получении</div>')
        for ln in g["mm"]:
            x = ln["mm_pick"]
            h.append(_card(ln, "Мастер Мобайл: " + _link(x.get("name", ""), x.get("url")), _price(x)))
    if g["mm_manual"] or g["manual"]:
        why = ""
        if g["mm_manual"] and mm_res is not None and getattr(mm_res, "problems", None):
            why = f'<div class="note">Мастер Мобайл не оформился сам: {E("; ".join(mm_res.problems))}</div>'
        h.append(f'<h2>👉 Купить руками <span class="tag blue">{len(g["mm_manual"]) + len(g["manual"])}</span></h2>{why}')
        for ln in g["mm_manual"]:
            x = ln["mm_pick"]
            h.append(_card(ln, "Мастер Мобайл, пав. 535: " + _link(x.get("name", ""), x.get("url")), _price(x)))
        for ln in g["manual"]:
            ext = ln["elsewhere"]
            first = ext["mitino"][0]
            alts = "".join(
                f'<li>{E(a.get("shop") or a.get("supplier") or "")}: {_link(a.get("name", ""), a.get("url"))} — '
                f'{_rub(a.get("opt") or _price(a))}</li>' for a in ext["mitino"][1:4])
            extra = (f'<ul class="alt">{alts}</ul>' if alts else "")
            note = ln["problems"][-1] if ln.get("problems") else ""
            if note:
                extra = f'<div class="note">{E(note)}</div>' + extra
            where = first.get("shop") or first.get("supplier") or ""
            h.append(_card(ln, f"{E(where)}: " + _link(first.get("name", ""), first.get("url")),
                           first.get("opt") or _price(first), extra))
    if g["none"]:
        h.append(f'<h2>🚫 Нет нигде в Митино <span class="tag bad">{len(g["none"])}</span></h2>')
        for ln in g["none"]:
            ext = ln.get("elsewhere") or {}
            bits = []
            for x in (ext.get("mitino_other_quality") or [])[:3]:
                bits.append(f'<li>другое качество: {E(x.get("shop") or x.get("supplier") or "")} — '
                            f'{_link(x.get("name", ""), x.get("url"))} — {_rub(x.get("opt") or _price(x))}</li>')
            for x in (ext.get("mm_not_mitino") or [])[:2]:
                bits.append(f'<li>Мастер Мобайл не в Митино: {_link(x.get("name", ""), x.get("url"))} — {_rub(_price(x))}</li>')
            note = ln["problems"][-1] if ln.get("problems") else ""
            extra = (f'<div class="note">{E(note)}</div>' if note else "") + (f'<ul class="alt">{"".join(bits)}</ul>' if bits else "")
            h.append(_card(ln, '<span class="empty">не найдено</span>', None, extra))
    issues = []
    if mres.get("status") == "unknown":
        issues.append("moba: заказ мог уйти — проверь на сайте. " + "; ".join(mres.get("problems") or []))
    if mm_res is not None and getattr(mm_res, "status", "") == "unknown":
        issues.append("Мастер Мобайл: заказ мог уйти — проверь в кабинете. " + "; ".join(mm_res.problems))
    issues += [f"WB: {e}" for e in wb_errors]
    if issues:
        h.append('<h2>⚠️ Проверить</h2>' + "".join(f'<div class="card note">{E(i)}</div>' for i in issues))
    if not lines:
        h.append('<p class="empty">Новых заказов нет.</p>')
    h.append("</div></body></html>")
    return "".join(h)
