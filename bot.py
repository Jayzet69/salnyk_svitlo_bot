"""Бот: сповіщає в Telegram, коли за графіком VOE вимикають/вмикають світло."""
import os, re, json, hashlib, datetime as dt
from zoneinfo import ZoneInfo
from curl_cffi import requests

TZ = ZoneInfo("Europe/Kyiv")
BASE = "https://disconnection.voe.com.ua"
STATE_FILE = "state.json"
TOKEN = os.environ["TG_TOKEN"]
# Адреса береться з секрету VOE_ADDRESS (JSON), щоб не світити її в публічному репозиторії
ADDR = json.loads(os.environ["VOE_ADDRESS"])


def tg(method, **params):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=30)
    return r.json()


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def chat_id(st):
    cid = os.environ.get("TG_CHAT_ID") or st.get("chat_id")
    if not cid:  # беремо з останнього повідомлення, яке ви написали боту
        upd = tg("getUpdates").get("result", [])
        if not upd:
            raise SystemExit("Напишіть боту будь-яке повідомлення в Telegram і запустіть ще раз")
        cid = (upd[-1].get("message") or upd[-1].get("my_chat_member"))["chat"]["id"]
        st["chat_id"] = cid
    return cid


def fetch():
    s = requests.Session(impersonate="chrome")
    html = s.get(BASE + "/", timeout=40).text
    m = re.search(r'csrf[^a-f0-9]{0,60}([a-f0-9]{32,128})', html, re.I)
    if not m:
        raise RuntimeError("CSRF не знайдено. Початок сторінки: " + html[:300])
    data = {"csrf": m.group(1), "selected_eic": "", "website": "", "type": "address",
            "value": ADDR["house_id"], **ADDR}
    r = s.post(BASE + "/api/search.php", data=data, timeout=40, headers={
        "x-requested-with": "XMLHttpRequest", "origin": BASE, "referer": BASE + "/"})
    j = r.json()
    if not j.get("ok") or not j.get("tables"):
        raise RuntimeError("Дивна відповідь: " + r.text[:300])
    return j["tables"][0]


def parse(table):
    """Повертає список інтервалів (start, end, kind) відключень."""
    iv = []
    for row in table.get("rows", []):
        d = dt.date.fromisoformat(row["date"])
        for c in row["cells"]:
            h = int(c["hour"][:2])
            for sg in c.get("segments", []):
                st = dt.datetime.combine(d, dt.time(h), TZ) + dt.timedelta(minutes=0.6 * sg["start"])
                iv.append([st, st + dt.timedelta(minutes=0.6 * sg["size"]), "графік"])
    msg = table.get("message") or {}
    if msg.get("from") and msg.get("to"):
        f = lambda s: dt.datetime.strptime(s, "%H:%M %Y.%m.%d").replace(tzinfo=TZ)
        iv.append([f(msg["from"]), f(msg["to"]), (msg.get("title") or "аварійне").lower()])
    iv.sort()
    merged = []
    for a, b, k in iv:  # зливаємо суміжні/перекриті
        if merged and a <= merged[-1][1] + dt.timedelta(minutes=1):
            merged[-1][1] = max(merged[-1][1], b)
            if k != "графік":
                merged[-1][2] = k
        else:
            merged.append([a, b, k])
    return merged


def fmt(t, now):
    day = "" if t.date() == now.date() else (" завтра" if t.date() == now.date() + dt.timedelta(1) else t.strftime(" %d.%m"))
    return t.strftime("%H:%M") + day


def schedule_text(iv, now, table):
    lines = [f"📅 Графік змінився (черга {table.get('listnum', '?')}):"]
    future = [x for x in iv if x[1] > now]
    lines += [f"• {fmt(a, now)} – {fmt(b, now)} ({k})" for a, b, k in future] or ["• відключень не заплановано ✅"]
    return "\n".join(lines)


def main():
    st = load_state()
    cid = chat_id(st)
    now = dt.datetime.now(TZ)
    try:
        table = fetch()
    except Exception as e:
        st["fails"] = st.get("fails", 0) + 1
        print("Помилка:", e)
        if st["fails"] == 6:  # ~30 хв підряд не вдається
            tg("sendMessage", chat_id=cid, text=f"⚠️ Не можу отримати графік з сайту VOE.\n{str(e)[:300]}")
        save_state(st)
        return
    if st.get("fails", 0) >= 6:
        tg("sendMessage", chat_id=cid, text="✅ Сайт VOE знову доступний")
    st["fails"] = 0

    iv = parse(table)
    cur = next((x for x in iv if x[0] <= now < x[1]), None)
    status = "off" if cur else "on"
    sched_hash = hashlib.sha1(json.dumps([[str(a), str(b), k] for a, b, k in iv if b > now]).encode()).hexdigest()

    if "status" not in st:
        tg("sendMessage", chat_id=cid, text="🤖 Бот запущено. Стежу за адресою: "
           f"{ADDR.get('city_label', '')}, {ADDR.get('street_label', '')}, {ADDR.get('house_label', '')}")
    if status != st.get("status"):
        if status == "off":
            text = f"🔴 Світло вимкнули ({cur[2]})\nОрієнтовно до {fmt(cur[1], now)}"
        else:
            nxt = next((x for x in iv if x[0] > now), None)
            text = "🟢 Світло увімкнули" + (f"\nНаступне вимкнення: {fmt(nxt[0], now)} – {fmt(nxt[1], now)}" if nxt else "")
        tg("sendMessage", chat_id=cid, text=text)
    if sched_hash != st.get("sched"):
        tg("sendMessage", chat_id=cid, text=schedule_text(iv, now, table))

    st.update(status=status, sched=sched_hash)
    save_state(st)
    print("OK", status)


if __name__ == "__main__":
    main()
