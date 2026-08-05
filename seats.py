#!/usr/bin/env python3
"""Find central free seat pairs for a movie in left-bank Astana cinemas, notify via Telegram with a seat map.

Two ticketing backends are supported. Kinopark exposes a booking API whose tokens ship in the
site's public runtime config; Chaplin exposes a plain schedule/hall API. Both are normalised into
the same seat shape, so the scoring and rendering below are chain-agnostic.
"""
import io
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta

MOVIE = os.environ.get("MOVIE", "Человек-паук")
DAYS = int(os.environ.get("DAYS", "7"))
STATE_FILE = "seats_state.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

KP_CITY = "000000000000000000010000"          # Astana
CINEMAS = [
    {"chain": "kinopark", "name": "Kinopark 7 Keruen",     "id": "000000000000000000000014"},
    {"chain": "kinopark", "name": "Kinopark 8 Saryarka",   "id": "000000000000000000000013"},
    {"chain": "chaplin",  "name": "Chaplin MEGA Silk Way", "id": 5},
    {"chain": "chaplin",  "name": "Chaplin Khan Shatyr",   "id": 6},
]

# Halls worth preferring even when a rival seat scores a little better.
# "rows" pins the wanted row band for that hall, overriding the generic edge-row rule.
PRIORITY = {
    ("Chaplin MEGA Silk Way", "зал 6"): {
        "note": "самый большой экран Астаны, 22×12 м, + Dolby Atmos",
        "rows": (7, 9),
    },
}
# ...but not at any cost: a badly placed pair in the flagship still loses to a good one elsewhere.
PRIORITY_MIN_SCORE = 55


def priority_of(cinema, hall):
    return PRIORITY.get((cinema, (hall or "").strip().lower()))


def get(url, token=None, referer="https://www.kinopark.kz/"):
    # Kinopark's WAF answers 425 unless the request looks like it came from the site itself.
    headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
               "Origin": referer.rstrip("/"), "Referer": referer}
    if token:
        headers["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def dates():
    return [(date.today() + timedelta(days=i)).isoformat() for i in range(DAYS)]


def wanted(d, t):
    """Weekday: evening only. Weekend: anything but morning. Nothing that starts too late."""
    weekend = datetime.strptime(d, "%Y-%m-%d").weekday() >= 5
    return ("12:00" if weekend else "18:00") <= t <= "22:30"


# --------------------------------------------------------------------------- Kinopark

def kp_tokens():
    html = get("https://www.kinopark.kz/ru/movies/soon")
    out = {}
    for name in ("SERVER_TOKEN_AFISHA", "SERVER_TOKEN_BOOKING_TESSERA"):
        m = re.search(r'"%s"\s*:\s*"([^"]+)"' % name, html)
        if not m:
            sys.exit(f"cannot read {name} from kinopark.kz")
        out[name] = m.group(1)
    return out


def kp_seances(cinema, tok):
    out = []
    for d in dates():
        url = (f"https://afisha.api.kinopark.kz/api/schedule/hall_format"
               f"?city={KP_CITY}&object={cinema['id']}&start={d}T07:00:00")
        for movie in json.loads(get(url, tok)).get("data") or []:
            if MOVIE.lower() not in movie.get("name", "").lower():
                continue
            for obj in movie.get("objects") or []:
                for fmt, hall in (obj.get("halls") or {}).items():
                    for s in hall.get("seances") or []:
                        start = s["timeframe"]["start"]
                        if s.get("language") != "rus" or not wanted(start[:10], start[11:16]):
                            continue
                        out.append({"cinema": cinema["name"], "date": start[:10], "time": start[11:16],
                                    "fmt": fmt, "ref": s["id"], "chain": "kinopark"})
    return out


def kp_plan(seance, tok):
    uuid = seance["ref"]
    j = json.loads(get(f"https://booking.api.kinopark.kz/v2/api/seance/{uuid}/info", tok))
    status = json.loads(get(f"https://booking.api.kinopark.kz/v2/api/seance/{uuid}/plan/status", tok)) or []
    taken = {x["seat_id"] for x in status}
    seats = [{"row": int(st["properties"]["row"]), "num": int(st["properties"]["col"]),
              "x": float(st["properties"]["grid"]["x"]), "free": st["id"] not in taken}
             for z in j["plan"].get("zones") or [] for st in z.get("seats") or []]
    price = min((d["value"] for z in j["plan"].get("zones") or []
                 for d in z.get("discounts") or [] if "Взрослый" in d.get("name", "")), default=None)
    return {"hall": j["hall"]["name"], "seats": seats, "price": price,
            "url": f"https://www.kinopark.kz/ru/booking/{uuid}"}


# --------------------------------------------------------------------------- Chaplin

def ch_seances(cinema, _tok=None):
    out = []
    for d in dates():
        url = f"https://chaplin.kz/api/get-cinema-schedule/{cinema['id']}/{d}"
        for movie in json.loads(get(url, referer="https://chaplin.kz/")) or []:
            if MOVIE.lower() not in (movie.get("title") or "").lower():
                continue
            for s in movie.get("schedules") or []:
                if not wanted(s["date"], s["time"]):
                    continue
                out.append({"cinema": cinema["name"], "date": s["date"], "time": s["time"],
                            "fmt": s.get("format") or "2D", "ref": s["id"],
                            "hall_hint": s.get("hall_title"), "chain": "chaplin"})
    return out


def ch_plan(seance, _tok=None):
    j = json.loads(get(f"https://chaplin.kz/api/get-hall/{seance['ref']}",
                       referer="https://chaplin.kz/"))
    seats, prices = [], []
    for row in j.get("places") or []:
        for s in row:
            seats.append({"row": int(s["row"]), "num": int(s["place"]), "x": float(s["x"]),
                          "free": bool(s.get("is_free")) and bool(s.get("is_available"))})
            prices.append(s.get("price"))
    # Chaplin's x grows right-to-left on their canvas; flip so every chain shares one orientation.
    if seats:
        mx = max(s["x"] for s in seats)
        for s in seats:
            s["x"] = mx - s["x"]
    return {"hall": f"зал {seance.get('hall_hint')}", "seats": seats,
            "price": min(prices) if prices else None,
            "url": f"https://chaplin.kz/astana/seats/{seance['ref']}"}


CHAINS = {"kinopark": (kp_seances, kp_plan), "chaplin": (ch_seances, ch_plan)}


# --------------------------------------------------------------------------- scoring

def median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def geometry(plan):
    """Normalise seats onto a uniform column grid and locate the screen axis."""
    seats = plan["seats"]
    rows = sorted({s["row"] for s in seats})
    gaps = []
    for r in rows:
        xs = sorted(s["x"] for s in seats if s["row"] == r)
        gaps += [b - a for a, b in zip(xs, xs[1:]) if b > a]
    pitch = median(gaps) if gaps else 1
    lo = min(s["x"] for s in seats)
    for s in seats:
        s["col_pos"] = round((s["x"] - lo) / pitch)
    # Rows are not all centred on the same axis (side blocks skew the extremes) — median row centre wins.
    centres, halves = [], []
    for r in rows:
        cs = [s["col_pos"] for s in seats if s["row"] == r]
        centres.append((min(cs) + max(cs)) / 2)
        halves.append((max(cs) - min(cs)) / 2)
    return {"rows": rows, "axis": median(centres), "half": median(halves) or 1}


def best_pair(plan, want_rows=None):
    """Closest adjacent free pair to the wanted spot, skipping edge rows and edge seats.

    want_rows pins an explicit (first, last) row band for halls we know well; otherwise the
    generic rule applies — drop the first and last few rows and aim at the middle of the hall.
    """
    g = geometry(plan)
    rows, n = g["rows"], len(g["rows"])
    if want_rows:
        core = [r for r in rows if want_rows[0] <= r <= want_rows[1]]
        mid_row = sum(want_rows) / 2
    else:
        skip = 4 if n >= 12 else 3
        core = rows[skip:n - skip]
        mid_row = (rows[0] + rows[-1]) / 2
    if not core:
        return None
    best = None
    for r in core:
        row_seats = sorted([s for s in plan["seats"] if s["row"] == r], key=lambda s: s["col_pos"])
        edge = 4 if len(row_seats) >= 14 else 3
        inner = row_seats[edge:len(row_seats) - edge]
        for a, b in zip(inner, inner[1:]):
            if b["col_pos"] - a["col_pos"] != 1 or not a["free"] or not b["free"]:
                continue
            lat = abs((a["col_pos"] + b["col_pos"]) / 2 - g["axis"]) / g["half"]
            dep = abs(r - mid_row) / (n / 2)
            score = round(100 * (1 - (0.5 * lat ** 2 + 0.5 * dep ** 2) ** 0.5))
            if not best or score > best["score"]:
                best = {"row": r, "nums": sorted([a["num"], b["num"]]), "score": score,
                        "off_axis": (a["col_pos"] + b["col_pos"]) / 2 - g["axis"],
                        "off_mid": r - mid_row, "rows": n}
    return best


# --------------------------------------------------------------------------- rendering

FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
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


def render_png(plan, pick, title, subtitle, badge=None):
    from PIL import Image, ImageDraw

    geometry(plan)
    seats = plan["seats"]
    rows = sorted({s["row"] for s in seats})
    cols_lo = min(s["col_pos"] for s in seats)
    cols_hi = max(s["col_pos"] for s in seats)
    cols, nrows = cols_hi - cols_lo + 1, len(rows)

    CELL = 26 if cols <= 26 else max(12, int(700 / cols))
    GAP = 5 if CELL >= 20 else 2
    PITCH = CELL + GAP
    grid_w = cols * PITCH - GAP
    band = 34 if badge else 0            # flagship ribbon above the title
    top, PAD = 132 + band, 28
    W, H = grid_w + 2 * PAD + 64, top + nrows * PITCH + 66

    img = Image.new("RGB", (W, H), "#FFFFFF")
    d = ImageDraw.Draw(img)
    x0 = PAD + 32
    fs = _font(min(11, max(7, CELL - 15)))

    if badge:
        bf = _font(12, True)
        # plain text only: the star glyph is missing from some system fonts and renders as tofu
        text = "ФЛАГМАНСКИЙ ЗАЛ  ·  " + badge.upper()
        d.rounded_rectangle([PAD, 16, min(W - PAD, PAD + d.textlength(text, font=bf) + 28), 16 + 26],
                            radius=13, fill="#7C4DFF")
        d.text((PAD + 14, 21), text, font=bf, fill="#FFFFFF")

    d.text((PAD, 20 + band), title, font=_font(19, True), fill="#12141A")
    d.text((PAD, 48 + band), subtitle, font=_font(14), fill="#6B7280")

    inset = grid_w * 0.07
    d.arc([x0 + inset, 72 + band, x0 + grid_w - inset, 116 + band],
          start=202, end=338, fill="#8E96A1", width=5)
    sw = d.textlength("Э К Р А Н", font=_font(11))
    d.text((x0 + (grid_w - sw) / 2, 104 + band), "Э К Р А Н", font=_font(11), fill="#8E96A1")

    lbl = _font(11)
    for ri, r in enumerate(rows):
        y = top + ri * PITCH
        d.text((PAD, y + 7), f"{r}", font=lbl, fill="#9AA1AB")
        d.text((x0 + grid_w + 12, y + 7), f"{r}", font=lbl, fill="#9AA1AB")
        for s in [s for s in seats if s["row"] == r]:
            x = x0 + (s["col_pos"] - cols_lo) * PITCH
            chosen = pick and r == pick["row"] and s["num"] in pick["nums"]
            if chosen:
                bg, fg = "#7C4DFF", "#FFFFFF"
            elif s["free"]:
                bg, fg = "#E6E9ED", "#79818C"
            else:
                bg, fg = "#5A626D", "#A9B0BA"
            d.rounded_rectangle([x, y, x + CELL, y + CELL], radius=max(3, CELL // 4), fill=bg)
            if CELL >= 18:
                t = str(s["num"])
                d.text((x + (CELL - d.textlength(t, font=fs)) / 2, y + 7), t, font=fs, fill=fg)

    ly = top + nrows * PITCH + 20
    for i, (color, text) in enumerate([("#E6E9ED", "свободно"), ("#5A626D", "занято"), ("#7C4DFF", "вам")]):
        lx = PAD + i * 132
        d.rounded_rectangle([lx, ly, lx + 16, ly + 16], radius=5, fill=color)
        d.text((lx + 24, ly + 2), text, font=lbl, fill="#6B7280")

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- telegram

def send_photo(png, caption):
    boundary = "----seatmap7f3a"
    parts = []
    for key, val in (("chat_id", os.environ["TELEGRAM_CHAT_ID"]),
                     ("caption", caption), ("parse_mode", "HTML")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{val}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="seats.png"\r\n'
                 f'Content-Type: image/png\r\n\r\n'.encode() + png + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendPhoto",
        data=b"".join(parts), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    resp = json.load(urllib.request.urlopen(req, timeout=60))
    if not resp.get("ok"):
        sys.exit(f"Telegram API error: {resp}")
    print(f"photo sent, message_id={resp['result']['message_id']}")


def send(text):
    payload = json.dumps({"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text,
                          "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"})
    resp = json.load(urllib.request.urlopen(req, timeout=30))
    if not resp.get("ok"):
        sys.exit(f"Telegram API error: {resp}")
    print(f"sent, message_id={resp['result']['message_id']}")


# --------------------------------------------------------------------------- main

def main():
    tok = kp_tokens()
    found = []
    for c in CINEMAS:
        lister, _ = CHAINS[c["chain"]]
        t = tok["SERVER_TOKEN_AFISHA"] if c["chain"] == "kinopark" else None
        try:
            got = lister(c, t)
        except Exception as e:
            print(f"{c['name']}: schedule failed ({e})")
            continue
        print(f"{c['name']}: {len(got)} сеансов подходят по времени")
        found += got

    results, skipped = [], 0
    for s in found:
        _, planner = CHAINS[s["chain"]]
        t = tok["SERVER_TOKEN_BOOKING_TESSERA"] if s["chain"] == "kinopark" else None
        try:
            plan = planner(s, t)
        except Exception:      # a seance can be listed before it opens for sale
            skipped += 1
            continue
        if not plan["seats"]:
            skipped += 1
            continue
        prio = priority_of(s["cinema"], plan["hall"])
        pick = best_pair(plan, prio.get("rows") if prio else None)
        if pick:
            total = len(plan["seats"])
            free = sum(1 for x in plan["seats"] if x["free"])
            results.append({**s, "plan": plan, "pick": pick, "total": total,
                            "occ": round(100 * (total - free) / total), "prio": prio})
    print(f"{len(results)} сеансов с центральной парой, {skipped} недоступны")
    if not results:
        print("нет подходящих мест")
        return

    # priority halls come first once their seat is decent; then best centring, bigger hall, emptier session
    results.sort(key=lambda r: (0 if (r["prio"] and r["pick"]["score"] >= PRIORITY_MIN_SCORE) else 1,
                                -r["pick"]["score"], -r["total"], r["occ"]))
    prio_hits = sum(1 for r in results if r["prio"])
    print(f"из них в приоритетных залах: {prio_hits}")
    top = results[0]
    pick, plan = top["pick"], top["plan"]
    key = f"{top['date']}|{top['time']}|{top['cinema']}|{plan['hall']}|{pick['nums']}"

    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {"sent": []}
    if key in state["sent"] and os.environ.get("FORCE") != "true":
        print("об этом варианте уже сообщали")
        return

    dow = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][datetime.strptime(top["date"], "%Y-%m-%d").weekday()]
    when = f"{dow} {top['date'][8:]}.{top['date'][5:7]}, {top['time']}"
    where = f"{top['cinema']}, {plan['hall']}"
    head = "⭐️ <b>ФЛАГМАНСКИЙ ЗАЛ</b>\n\n" if top["prio"] else ""
    caption = (f"{head}🎬 <b>{MOVIE}</b> — свободна пара по центру\n\n"
               f"{when} · {where} · {top['fmt']}\n"
               f"<b>ряд {pick['row']} из {pick['rows']}, места {pick['nums'][0]}+{pick['nums'][1]}</b>\n"
               f"от оси экрана {pick['off_axis']:+g}, от середины зала {pick['off_mid']:+g} ряда\n"
               f"зал на {top['total']} мест, занято {top['occ']}% · {plan['price']} ₸\n")
    if top["prio"]:
        caption += f"⭐️ {top['prio']['note']}\n"
    caption += f"\n{plan['url']}"
    others = "\n".join(
        f"{'⭐️ ' if r['prio'] else '· '}{r['date'][5:]} {r['time']} {r['cinema']} {r['plan']['hall']} — "
        f"ряд {r['pick']['row']}, места {r['pick']['nums'][0]}+{r['pick']['nums'][1]}"
        for r in results[1:5])
    if others:
        caption += "\n\nЕщё варианты:\n" + others

    try:
        send_photo(render_png(plan, pick, MOVIE, f"{when} · {where}",
                              top["prio"]["note"] if top["prio"] else None), caption)
    except Exception as e:     # picture is nice-to-have, the pick is the point
        print(f"photo failed ({e}); falling back to text")
        send(caption)

    state["sent"].append(key)
    json.dump(state, open(STATE_FILE, "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
