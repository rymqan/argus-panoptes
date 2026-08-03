#!/usr/bin/env python3
"""Find central free seat pairs for a movie in Astana Kinopark halls, notify via Telegram with a seat map."""
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta

MOVIE = os.environ.get("MOVIE", "Человек-паук")
CITY = "000000000000000000010000"
CINEMAS = {
    "Kinopark 8 Saryarka": "000000000000000000000013",
    "Kinopark 7 Keruen": "000000000000000000000014",
    "Kinopark 6 Keruencity": "000000000000000000000012",
}
DAYS = int(os.environ.get("DAYS", "7"))
SEATS_WANTED = 2
STATE_FILE = "seats_state.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def get(url, token=None):
    # Their WAF answers 425 unless the request looks like it came from the site itself.
    headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
               "Origin": "https://www.kinopark.kz", "Referer": "https://www.kinopark.kz/"}
    if token:
        headers["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def tokens():
    """Kinopark ships its API tokens in the page's public runtime config; read them fresh so rotation can't break us."""
    html = get("https://www.kinopark.kz/ru/movies/soon")
    out = {}
    for name in ("SERVER_TOKEN_AFISHA", "SERVER_TOKEN_BOOKING_TESSERA"):
        m = re.search(r'"%s"\s*:\s*"([^"]+)"' % name, html)
        if not m:
            sys.exit(f"cannot read {name} from kinopark.kz")
        out[name] = m.group(1)
    return out


def seances(tok):
    """All seances of the target movie across the configured cinemas and date range."""
    out = []
    for cname, oid in CINEMAS.items():
        for i in range(DAYS):
            d = (date.today() + timedelta(days=i)).isoformat()
            url = f"https://afisha.api.kinopark.kz/api/schedule/hall_format?city={CITY}&object={oid}&start={d}T07:00:00"
            data = json.loads(get(url, tok)).get("data") or []
            for movie in data:
                if MOVIE.lower() not in movie.get("name", "").lower():
                    continue
                for obj in movie.get("objects") or []:
                    for hall in (obj.get("halls") or {}).values():
                        for s in hall.get("seances") or []:
                            start = s["timeframe"]["start"]
                            out.append({
                                "cinema": cname, "date": start[:10], "time": start[11:16],
                                "lang": s.get("language", ""), "uuid": s["id"],
                            })
    return out


def wanted(s):
    """Weekday: evening only. Weekend: anything but morning. Nothing that starts too late."""
    if s["lang"] != "rus":
        return False
    weekend = datetime.strptime(s["date"], "%Y-%m-%d").weekday() >= 5
    return ("12:00" if weekend else "18:00") <= s["time"] <= "22:30"


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def hall_plan(uuid, tok):
    j = json.loads(get(f"https://booking.api.kinopark.kz/v2/api/seance/{uuid}/info", tok))
    seats = [
        {"id": st["id"], "row": int(st["properties"]["row"]),
         "col": int(st["properties"]["col"]), "x": st["properties"]["grid"]["x"]}
        for z in j["plan"].get("zones") or [] for st in z.get("seats") or []
    ]
    price = min((d["value"] for z in j["plan"].get("zones") or []
                 for d in z.get("discounts") or [] if "Взрослый" in d.get("name", "")), default=None)
    rows = sorted({s["row"] for s in seats})
    # Rows are not all centred on the same axis (side blocks skew the extremes) — the median row centre is the screen axis.
    axis = median([(min(s["x"] for s in seats if s["row"] == r) +
                    max(s["x"] for s in seats if s["row"] == r)) / 2 for r in rows])
    half = median([(max(s["x"] for s in seats if s["row"] == r) -
                    min(s["x"] for s in seats if s["row"] == r)) / 2 for r in rows]) or 1
    return {"seats": seats, "rows": rows, "axis": axis, "half": half,
            "hall": j["hall"]["name"], "count": j["plan"]["count"], "price": price}


def best_pair(plan, taken):
    """Closest adjacent free pair to the hall centre, skipping edge rows and edge seats."""
    rows, n = plan["rows"], len(plan["rows"])
    skip = 4 if n >= 12 else 3
    core = rows[skip:n - skip]
    mid_row = (rows[0] + rows[-1]) / 2
    best = None
    for r in core:
        row_seats = sorted([s for s in plan["seats"] if s["row"] == r], key=lambda s: s["x"])
        edge = 4 if len(row_seats) >= 14 else 3
        inner = row_seats[edge:len(row_seats) - edge]
        for a, b in zip(inner, inner[1:]):
            if b["x"] - a["x"] != 1 or a["id"] in taken or b["id"] in taken:
                continue
            lat = abs((a["x"] + b["x"]) / 2 - plan["axis"]) / plan["half"]
            dep = abs(r - mid_row) / (n / 2)
            score = round(100 * (1 - (0.5 * lat ** 2 + 0.5 * dep ** 2) ** 0.5))
            if not best or score > best["score"]:
                best = {"row": r, "cols": sorted([a["col"], b["col"]]), "score": score,
                        "off_axis": (a["x"] + b["x"]) / 2 - plan["axis"], "off_mid": r - mid_row}
    return best


def render(plan, taken, pick):
    """Compact seat map: ▫ free, ▪ taken, ▣ suggested. Stays under ~30 chars so it fits a phone."""
    xs = [s["x"] for s in plan["seats"]]
    lo, hi = min(xs), max(xs)
    lines = ["      ЭКРАН"]
    for r in plan["rows"]:
        by_x = {s["x"]: s for s in plan["seats"] if s["row"] == r}
        line = ""
        for x in range(lo, hi + 1):
            s = by_x.get(x)
            if not s:
                line += " "
            elif pick and r == pick["row"] and s["col"] in pick["cols"]:
                line += "▣"
            else:
                line += "▪" if s["id"] in taken else "▫"
        lines.append(f"{r:>2} {line}")
    return "\n".join(lines)


FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",          # GitHub runners
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",             # macOS
    "/Library/Fonts/Arial.ttf",
]


def _font(size, bold=False):
    from PIL import ImageFont
    for p in FONTS:
        if bold and "Bold" not in p:
            continue
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    for p in FONTS:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def render_png(plan, taken, pick, title, subtitle):
    """Seat map as a picture: light seats free, dark taken, accent = the suggested pair."""
    from PIL import Image, ImageDraw

    CELL, GAP, PAD = 26, 5, 28
    PITCH = CELL + GAP
    xs = [s["x"] for s in plan["seats"]]
    lo, hi = min(xs), max(xs)
    cols, nrows = hi - lo + 1, len(plan["rows"])

    grid_w = cols * PITCH - GAP
    top = 132                                   # header + screen arc
    W = grid_w + 2 * PAD + 64                   # room for row numbers on both sides
    H = top + nrows * PITCH + 66

    img = Image.new("RGB", (W, H), "#FFFFFF")
    d = ImageDraw.Draw(img)
    x0 = PAD + 32

    d.text((PAD, 20), title, font=_font(19, True), fill="#12141A")
    d.text((PAD, 48), subtitle, font=_font(14), fill="#6B7280")

    # screen: shallow arc bulging toward the audience, label tucked underneath
    inset = grid_w * 0.07
    d.arc([x0 + inset, 72, x0 + grid_w - inset, 116], start=202, end=338, fill="#8E96A1", width=5)
    sw = d.textlength("Э К Р А Н", font=_font(11))
    d.text((x0 + (grid_w - sw) / 2, 104), "Э К Р А Н", font=_font(11), fill="#8E96A1")

    fs = _font(11)
    for ri, r in enumerate(plan["rows"]):
        y = top + ri * PITCH
        by_x = {s["x"]: s for s in plan["seats"] if s["row"] == r}
        d.text((PAD, y + 7), f"{r}", font=fs, fill="#9AA1AB")
        d.text((x0 + grid_w + 12, y + 7), f"{r}", font=fs, fill="#9AA1AB")
        for c in range(cols):
            s = by_x.get(lo + c)
            if not s:
                continue
            x = x0 + c * PITCH
            chosen = pick and r == pick["row"] and s["col"] in pick["cols"]
            if chosen:
                bg, fg = "#7C4DFF", "#FFFFFF"
            elif s["id"] in taken:
                bg, fg = "#5A626D", "#A9B0BA"
            else:
                bg, fg = "#E6E9ED", "#79818C"
            d.rounded_rectangle([x, y, x + CELL, y + CELL], radius=7, fill=bg)
            label = str(s["col"])
            d.text((x + (CELL - d.textlength(label, font=fs)) / 2, y + 7), label, font=fs, fill=fg)

    ly = top + nrows * PITCH + 20
    for i, (color, text) in enumerate(
            [("#E6E9ED", "свободно"), ("#5A626D", "занято"), ("#7C4DFF", "вам")]):
        lx = PAD + i * 132
        d.rounded_rectangle([lx, ly, lx + 16, ly + 16], radius=5, fill=color)
        d.text((lx + 24, ly + 2), text, font=fs, fill="#6B7280")

    import io
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def send_photo(png, caption):
    boundary = "----seatmap7f3a"
    parts = []
    for key, val in (("chat_id", os.environ["TELEGRAM_CHAT_ID"]),
                     ("caption", caption), ("parse_mode", "HTML")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{val}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="seats.png"\r\n'
        f'Content-Type: image/png\r\n\r\n'.encode() + png + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendPhoto",
        data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    resp = json.load(urllib.request.urlopen(req, timeout=60))
    if not resp.get("ok"):
        sys.exit(f"Telegram API error: {resp}")
    print(f"photo sent, message_id={resp['result']['message_id']}")


def send(text):
    payload = json.dumps({
        "chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"})
    resp = json.load(urllib.request.urlopen(req, timeout=30))
    if not resp.get("ok"):
        sys.exit(f"Telegram API error: {resp}")
    print(f"sent, message_id={resp['result']['message_id']}")


def main():
    tok = tokens()
    found = [s for s in seances(tok["SERVER_TOKEN_AFISHA"]) if wanted(s)]
    print(f"{len(found)} seances match the time rules")

    results, skipped = [], 0
    for s in found:
        try:
            plan = hall_plan(s["uuid"], tok["SERVER_TOKEN_BOOKING_TESSERA"])
            status = json.loads(get(f"https://booking.api.kinopark.kz/v2/api/seance/{s['uuid']}/plan/status",
                                    tok["SERVER_TOKEN_BOOKING_TESSERA"])) or []
        except Exception as e:  # a seance can be listed before it opens for sale
            skipped += 1
            continue
        taken = {x["seat_id"] for x in status}
        pick = best_pair(plan, taken)
        if pick:
            results.append({**s, "plan": plan, "taken": taken, "pick": pick,
                            "occ": round(100 * len(taken) / plan["count"])})
    print(f"{len(results)} with a central pair, {skipped} unavailable")

    if not results:
        print("no central pairs available")
        return
    results.sort(key=lambda r: (-r["pick"]["score"], r["occ"]))
    top = results[0]
    key = f"{top['date']}|{top['time']}|{top['cinema']}|{top['pick']['row']}|{top['pick']['cols']}"

    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {"sent": []}
    if key in state["sent"] and os.environ.get("FORCE") != "true":
        print("already notified about this pick")
        return

    p, pick = top["plan"], top["pick"]
    dow = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][datetime.strptime(top["date"], "%Y-%m-%d").weekday()]
    when = f"{dow} {top['date'][8:]}.{top['date'][5:7]}, {top['time']}"
    caption = (f"🎬 <b>{MOVIE}</b> — свободна пара по центру\n\n"
               f"{when} · {top['cinema']}, {p['hall']}\n"
               f"<b>ряд {pick['row']} из {len(p['rows'])}, места {pick['cols'][0]}+{pick['cols'][1]}</b>\n"
               f"от оси экрана {pick['off_axis']:+g}, от середины зала {pick['off_mid']:+g} ряда\n"
               f"занято {top['occ']}% · {p['price']} ₸ за билет\n\n"
               f"https://www.kinopark.kz/ru/booking/{top['uuid']}")
    others = "\n".join(
        f"· {r['date'][5:]} {r['time']} {r['cinema']} — ряд {r['pick']['row']}, "
        f"места {r['pick']['cols'][0]}+{r['pick']['cols'][1]}"
        for r in results[1:4])
    if others:
        caption += "\n\nЕщё варианты:\n" + others

    png = render_png(p, top["taken"], pick, MOVIE, f"{when} · {top['cinema']}, {p['hall']}")
    try:
        send_photo(png, caption)
    except Exception as e:  # picture is nice-to-have, the pick is the point
        print(f"photo failed ({e}); falling back to text")
        send(caption + "\n\n<pre>" + render(p, top["taken"], pick) + "</pre>")

    state["sent"].append(key)
    json.dump(state, open(STATE_FILE, "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
