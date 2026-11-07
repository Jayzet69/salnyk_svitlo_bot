"""Бот: сповіщає в Telegram, коли за графіком VOE вимикають/вмикають світло."""
import os, re, json, time, hashlib, datetime as dt
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


def dur(a, b):
    m = round((b - a).total_seconds() / 60)
    h, m = divmod(m, 60)
    return " ".join(x for x in [f"{h} год" if h else "", f"{m} хв" if m else ""] if x) or "0 хв"


def period(a, b, now):
    return f"{fmt(a, now)} – {fmt(b, now)} ({dur(a, b)})"


def schedule_text(iv, now, table):
    lines = [f"📅 Графік змінився (черга {table.get('listnum', '?')}):"]
    future = [x for x in iv if x[1] > now]
    lines += [f"• {period(max(a, now) if a < now else a, b, now)} — {k}" for a, b, k in future] or ["• відключень не заплановано ✅"]
    return "\n".join(lines)


def check(st, cid, iv, now):
    """Перевіряє статус і нагадування. Кожне нагадування надсилається лише один раз."""
    sent = st.setdefault("sent", [])

    def once(key, text):
        if key not in sent:
            tg("sendMessage", chat_id=cid, text=text)
            sent.append(key)
            del sent[:-100]

    cur = next((x for x in iv if x[0] <= now < x[1]), None)
    nxt = next((x for x in iv if x[0] > now), None)
    status = "off" if cur else "on"

    if status != st.get("status"):
        if status == "off":
            tg("sendMessage", chat_id=cid, text=f"🔴 Світло вимкнули ({cur[2]})\nБез світла: {period(cur[0], cur[1], now)}")
        else:
            prev = max((x[1] for x in iv if x[1] <= now), default=None)
            a = prev if prev and now - prev < dt.timedelta(minutes=30) else now
            text = f"🟢 Світло увімкнули\nЗі світлом: {period(a, nxt[0], now)}" if nxt else \
                "🟢 Світло увімкнули\nНаступних відключень поки не заплановано"
            tg("sendMessage", chat_id=cid, text=text)
        st["status"] = status

    if nxt:  # нагадування перед вимкненням
        left = (nxt[0] - now).total_seconds() / 60
        k = nxt[0].isoformat()
        if 6 < left <= 30:
            once("w30:" + k, f"⏰ Через {round(left)} хв вимкнення світла\nБез світла: {period(nxt[0], nxt[1], now)}")
        elif 0 < left <= 6:
            once("w5:" + k, f"⚠️ Через {max(1, round(left))} хв вимкнення світла!\nБез світла: {period(nxt[0], nxt[1], now)}")
    if cur:  # нагадування перед увімкненням
        left = (cur[1] - now).total_seconds() / 60
        if 0 < left <= 10:
            once("on10:" + cur[1].isoformat(), f"💡 Через {max(1, round(left))} хв має з'явитися світло (о {fmt(cur[1], now)})")


def main():
    st = load_state()
    cid = chat_id(st)
    run_until = time.time() + 60 * float(os.environ.get("RUN_MINUTES", "0"))
    iv, table, last_fetch = None, None, 0
    first = True
    while first or time.time() < run_until:
        first = False
        now = dt.datetime.now(TZ)
        if time.time() - last_fetch >= 180 or iv is None:  # сайт опитуємо раз на 3 хв
            last_fetch = time.time()
            try:
                table = fetch()
                if st.get("fails", 0) >= 6:
                    tg("sendMessage", chat_id=cid, text="✅ Сайт VOE знову доступний")
                st["fails"] = 0
                iv = parse(table)
                h = hashlib.sha1(json.dumps([[str(a), str(b), k] for a, b, k in iv if b > now]).encode()).hexdigest()
                if "status" not in st:
                    tg("sendMessage", chat_id=cid, text="🤖 Бот запущено. Стежу за адресою: "
                       f"{ADDR.get('city_label', '')}, {ADDR.get('street_label', '')}, {ADDR.get('house_label', '')}")
                if h != st.get("sched"):
                    tg("sendMessage", chat_id=cid, text=schedule_text(iv, now, table))
                    st["sched"] = h
            except Exception as e:
                st["fails"] = st.get("fails", 0) + 1
                print("Помилка:", e)
                if st["fails"] == 6:
                    tg("sendMessage", chat_id=cid, text=f"⚠️ Не можу отримати графік з сайту VOE.\n{str(e)[:300]}")
        if iv is not None:
            check(st, cid, iv, now)
        save_state(st)
        if time.time() < run_until:
            time.sleep(20)
    print("OK", st.get("status"))


if __name__ == "__main__":
    main()
