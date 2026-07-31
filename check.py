#!/usr/bin/env python3
"""Watch kz.kinoafisha.info for «Одиссея» IMAX sessions on target dates, notify via Telegram."""
import json
import os
import re
import sys
import urllib.request

URL = "https://kz.kinoafisha.info/astana/movies/8379477/"
TARGET_DATES = {"2026-08-03": "пн", "2026-08-04": "вт"}
TARGET_CINEMAS = {"Kinopark 8 IMAX Saryarka", "Kinopark 7 IMAX Keruen"}
STATE_FILE = "state.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def fetch():
    req = urllib.request.Request(URL, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")


def parse(html):
    """Return {"date|cinema": [times]} for target dates/cinemas."""
    found = {}
    # page contains one hidden <article data-schedule-date="YYYY-MM-DD"> per date
    parts = re.split(r'<article class="showtimesListItem_item[^"]*" data-schedule-date="(\d{4}-\d{2}-\d{2})"', html)
    for i in range(1, len(parts) - 1, 2):
        date, body = parts[i], parts[i + 1]
        if date not in TARGET_DATES:
            continue
        # cinema blocks carry their clean name in data-Schedule-item="..."
        chunks = re.split(r'data-Schedule-item="([^"]+)"', body)
        for j in range(1, len(chunks) - 1, 2):
            cinema, cbody = chunks[j].strip(), chunks[j + 1]
            if cinema not in TARGET_CINEMAS:
                continue
            times = re.findall(r'class="session_time">([^<]+)<', cbody)
            if times:
                found[f"{date}|{cinema}"] = times
    return found


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    data = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    resp = json.load(urllib.request.urlopen(req, timeout=30))
    if not resp.get("ok"):
        sys.exit(f"Telegram API error: {resp}")
    print(f"Telegram message sent, message_id={resp['result']['message_id']}")


def main():
    if os.environ.get("FORCE_TEST") == "true":
        send_telegram("✅ Тест: воркфлоу «Одиссея» работает. Слежу за сеансами 3–4 августа в Kinopark 8 IMAX Saryarka и Kinopark 7 IMAX Keruen.")
        return

    html = open(sys.argv[1]).read() if len(sys.argv) > 1 else fetch()
    found = parse(html)
    print(f"Found target sessions: {json.dumps(found, ensure_ascii=False)}")

    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {"notified": []}
    new_keys = [k for k in found if k not in state["notified"]]
    if not new_keys:
        print("Nothing new.")
        return

    lines = ["🎬 «Одиссея» — появились сеансы!"]
    for k in sorted(new_keys):
        date, cinema = k.split("|")
        lines.append(f"{date} ({TARGET_DATES[date]}) — {cinema}: {', '.join(found[k])}")
    lines.append(URL)
    send_telegram("\n".join(lines))

    state["notified"] += new_keys
    json.dump(state, open(STATE_FILE, "w"), ensure_ascii=False, indent=1)
    print(f"State updated: {new_keys}")


if __name__ == "__main__":
    main()
