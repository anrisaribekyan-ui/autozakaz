"""Разовая проверка связок: удаляет те, где качество moba не совпадает с качеством WB
(например, на WB «OR», а связан «Стандарт»). Запуск: venv/bin/python fix_links.py [--apply]"""
import sqlite3, sys
from matcher import parse_wb_title, classify_moba, allowed, model_matches, detect_color, wb_wants_frame, is_or100

apply = "--apply" in sys.argv
c = sqlite3.connect("data/autozakaz.db")
rows = c.execute("""SELECT l.shop_key, l.nm_id, t.title, l.supplier_title FROM links l
                    LEFT JOIN wb_titles t ON t.shop_key=l.shop_key AND t.nm_id=l.nm_id""").fetchall()
bad = []
for shop, nm, wb_title, moba in rows:
    if not wb_title or not moba:
        continue
    wb = parse_wb_title(wb_title)
    wq = wb["quality"]
    mq = classify_moba(moba)["quality"]
    why = []
    if not allowed(wq, mq):
        why.append(f"качество {wq}→{mq}")
    if wb["models"] and not model_matches(moba, wb):
        why.append("модель/бренд")
    if wb_wants_frame(wb_title) and classify_moba(moba)["frame"] is not True:
        why.append("на WB с рамкой, на moba без")
    wc, mc = detect_color(wb_title), detect_color(moba)
    if wc and mc and wc != mc:
        why.append(f"цвет {wc}→{mc}")
    if why:
        bad.append((shop, nm, wb_title, moba, ", ".join(why), ""))
for b in bad:
    print(f"{b[0]} nmId {b[1]} [{b[4]}]\n   WB:   {b[2][:90]}\n   moba: {b[3][:110]}")
print(f"Несовпадений: {len(bad)} из {len(rows)}")
if apply and bad:
    c.executemany("DELETE FROM links WHERE shop_key=? AND nm_id=?", [(b[0], b[1]) for b in bad])
    c.commit()
    print("Удалено. Эти товары снова появятся в тренировке.")
elif bad:
    print("Чтобы удалить: venv/bin/python fix_links.py --apply")
