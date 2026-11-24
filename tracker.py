#!/usr/bin/env python3
"""Vienna -> Thailand, Vietnam, Malaysia and Indonesia flight-deal RSS monitor.

RSS finds published deals, not live inventory. Required env vars:
TELEGRAM_TOKEN and TELEGRAM_CHAT_ID. Dependencies: feedparser, requests.
"""
from __future__ import annotations
import hashlib, html, json, logging, os, re, sys, time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import feedparser
import requests

# Only these four countries are allowed. No airline whitelist: do not hide
# deals just because an unfamiliar airline operates them.
ORIGIN_ALIASES = ("vienna", "wien", "wiedeń", "viedeň", "vieden", "vídeň", "schwechat", "vie")
DESTINATIONS = {
    "Thajsko": {
        "Bangkok": ("bangkok", "bkk", "dmk"),
        "Phuket": ("phuket", "hkt"),
        "Koh Samui": ("koh samui", "ko samui", "usm"),
        "Krabi": ("krabi", "kbv"),
        "Chiang Mai": ("chiang mai", "cnx"),
    },
    "Vietnam": {
        "Hanoi": ("hanoi", "ha noi", "han"),
        "Ho Či Minovo Mesto": ("ho chi minh", "ho-chi-minh", "saigon", "sgn"),
        "Da Nang": ("da nang", "danang", "dad"),
        "Nha Trang": ("nha trang", "cam ranh", "cah"),
        "Phu Quoc": ("phu quoc", "pqc"),
    },
    "Malajzia": {
        "Kuala Lumpur": ("kuala lumpur", "kul"),
        "Langkawi": ("langkawi", "lgk"),
        "Penang": ("penang", "george town", "pen"),
        "Kota Kinabalu": ("kota kinabalu", "bki"),
    },
    "Indonézia": {
        "Bali": ("bali", "denpasar", "dps"),
        "Jakarta": ("jakarta", "cgk"),
        "Lombok": ("lombok", "lop"),
        "Yogyakarta": ("yogyakarta", "jog", "ykia"),
        "Surabaya": ("surabaya", "sub"),
        "Medan": ("medan", "kno"),
    },
}

# Fly4free's main feed is known to publish frequently. The regional and Polish
# feeds broaden coverage; duplicate posts are removed by canonical URL/hash.
FEEDS = (
    ("Fly4free", "https://www.fly4free.com/feed/"),
    ("Fly4free Europe", "https://www.fly4free.com/flights/flight-deals/europe/feed/"),
    ("Fly4free Poland", "https://www.fly4free.pl/feed/"),
    ("Travel-Dealz", "https://travel-dealz.com/feed/"),
)
AIRLINES = (
    "EVA Air", "Austrian Airlines", "Etihad Airways", "Qatar Airways", "Emirates",
    "Turkish Airlines", "Lufthansa", "Air China", "China Eastern", "China Southern",
    "Thai Airways", "Thai AirAsia", "AirAsia", "Malaysia Airlines", "Vietnam Airlines",
    "VietJet Air", "Singapore Airlines", "Scoot", "Cathay Pacific", "KLM", "Air France",
    "Finnair", "LOT Polish Airlines", "Saudia", "Oman Air", "Gulf Air", "IndiGo",
    "SriLankan Airlines", "Batik Air", "Garuda Indonesia", "Qantas", "British Airways",
    "Swiss", "Brussels Airlines",
)
FLIGHT_TERMS = (
    "flight", "flights", "airfare", "air fare", "plane ticket", "flight ticket",
    "roundtrip", "round trip", "return flight", "one-way", "one way", "non-stop",
    "nonstop", "direct flight", "letenk", "flug", "flüge", "volo", "voli",
    "billet d'avion", "flug", "flüge", "flugangebot", "flugangebote",
    "flugtickets", "loty", "lotów", "bilety lotnicze", "przelot",
    "przeloty", "wylot", "wyloty",
)
PRICE_RE = re.compile(
    r"(?:(€|EUR|US\$|USD|\$|£|GBP)\s*([0-9][0-9., ]{1,8})|"
    r"([0-9][0-9., ]{1,8})\s*(€|EUR|US\$|USD|\$|£|GBP))", re.I
)
SEEN_FILE = Path(__file__).with_name("seen.json")
MAX_SEEN = 5000
TIMEOUT = 25
USER_AGENT = "Zolo-Asia-Flight-Deal-Monitor/2.0"


def plain(value) -> str:
    text = str(value or "")
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def get_text(entry) -> str:
    bits = [entry.get("title", ""), entry.get("summary", "")]
    for item in entry.get("content", []) or []:
        if isinstance(item, dict): bits.append(item.get("value", ""))
    for tag in entry.get("tags", []) or []:
        if isinstance(tag, dict): bits.append(tag.get("term", ""))
    return plain(" ".join(map(str, bits))).lower()


def has_term(text: str, term: str) -> bool:
    term = term.lower().strip()
    if len(term) == 3 and term.isascii() and term.isalpha():
        # Three-letter IATA codes are common words (e.g. HAN, PEN, SUB).
        # Accept them only when they appear in an airport/route context.
        code = re.escape(term)
        patterns = (
            rf"\bfrom\s+{code}\b",
            rf"\bto\s+{code}\b",
            rf"\b{code}\s*(?:-|–|—|→|/|to|>)\s*[a-z]{{3}}\b",
            rf"\b[a-z]{{3}}\s*(?:-|–|—|→|/|to|>)\s*{code}\b",
            rf"\b{code}\s+(?:airport|intl|international)\b",
        )
        return any(re.search(pattern, text, re.I) for pattern in patterns)
    return term in text


def canonical_url(url: str) -> str:
    if not url: return ""
    try:
        p = urlsplit(url.strip())
        ignored = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "ref", "source"}
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in ignored]
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", urlencode(q, doseq=True), ""))
    except Exception:
        return url.strip()


def destinations_in(text: str):
    found = []
    for country, cities in DESTINATIONS.items():
        for city, aliases in cities.items():
            if any(has_term(text, a) for a in aliases): found.append((country, city))
    return list(dict.fromkeys(found))


def parse_price(text: str):
    m = PRICE_RE.search(text)
    if not m: return None
    if m.group(1): currency, amount = m.group(1), m.group(2)
    else: amount, currency = m.group(3), m.group(4)
    currency = currency.upper()
    currency = {"€": "EUR", "EUR": "EUR", "$": "USD", "US$": "USD", "USD": "USD", "£": "GBP", "GBP": "GBP"}.get(currency, currency)
    return f"{re.sub(r'\s+', '', amount)} {currency}"


def parse_published(entry):
    raw = entry.get("published") or entry.get("updated")
    if not raw: return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    except (TypeError, ValueError, OverflowError):
        return str(raw)[:80]


def classify(entry):
    title = plain(entry.get("title", "")) or "Akciová letenka"
    summary = plain(entry.get("summary", ""))
    text = get_text(entry)
    if not any(has_term(text, a) for a in ORIGIN_ALIASES): return None
    if not any(term in text for term in FLIGHT_TERMS): return None
    destinations = destinations_in(text)
    if not destinations: return None
    link = canonical_url(entry.get("link", ""))
    price = parse_price(title + " " + summary)
    airlines = [name for name in AIRLINES if name.lower() in text]
    return {
        "title": title, "summary": summary, "link": link, "price": price,
        "airlines": airlines, "destinations": destinations,
        "published": parse_published(entry),
    }


def fingerprint(deal):
    # Price is part of the key: if an article is updated with a new price,
    # the updated deal can be sent again instead of being hidden forever.
    basis = "|".join((deal["link"], re.sub(r"\s+", " ", deal["title"].lower()).strip(), deal["price"] or ""))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def load_seen() -> set[str]:
    if not SEEN_FILE.exists(): return set()
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list): return {str(x) for x in data}  # old format
        if isinstance(data, dict): return {str(x) for x in data.get("sent", [])}
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Chyba pri čítaní seen.json: %s", exc)
    return set()


def save_seen(seen: set[str]) -> None:
    payload = {"version": 2, "updated_at": datetime.now(timezone.utc).isoformat(), "sent": sorted(seen)[-MAX_SEEN:]}
    tmp = SEEN_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(SEEN_FILE)


def fetch_feed(session, source, url):
    try:
        response = session.get(url, timeout=TIMEOUT)
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
        if parsed.bozo and not parsed.entries:
            logging.warning("%s: RSS chyba: %s", source, parsed.bozo_exception)
            return []
        entries = list(parsed.entries[:100])
        logging.info("%s: načítaných %d článkov", source, len(entries))
        return entries
    except requests.RequestException as exc:
        logging.error("%s: sťahovanie zlyhalo: %s", source, exc)
        return []
    except Exception:
        logging.exception("%s: neočakávaná chyba", source)
        return []


def send_telegram(session, token, chat_id, message) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in range(3):
        try:
            r = session.post(url, json={"chat_id": chat_id, "text": message[:4000], "disable_web_page_preview": True}, timeout=TIMEOUT)
            if r.status_code == 429:
                try: wait = int(r.json().get("parameters", {}).get("retry_after", 5))
                except (ValueError, TypeError): wait = 5
                time.sleep(min(wait, 30)); continue
            r.raise_for_status()
            body = r.json()
            if body.get("ok"): return True
            logging.error("Telegram API chyba: %s", body)
            return False
        except requests.RequestException as exc:
            logging.warning("Telegram pokus %d/3 zlyhal: %s", attempt + 1, exc)
            if attempt < 2: time.sleep(2 ** attempt)
    return False


def make_message(deal, source):
    route = ", ".join(f"{city} ({country})" for country, city in deal["destinations"])
    lines = ["✈️ NOVÁ LETENKOVÁ PONUKA", "", f"Trasa: Viedeň → {route}", f"Ponuka: {deal['title']}"]
    lines.append(f"Zachytená cena: {deal['price']}" if deal["price"] else "Cena: v článku sa nepodarilo automaticky rozpoznať")
    if deal["airlines"]: lines.append("Aerolínia: " + ", ".join(deal["airlines"]))
    if deal["published"]: lines.append("Zverejnené: " + deal["published"])
    lines += ["", "Príručnú batožinu, prestupy a konečnú cenu over pred platbou.", f"Zdroj: {source}", f"Odkaz: {deal['link']}"]
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    token, chat_id = os.getenv("TELEGRAM_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logging.error("Chýba TELEGRAM_TOKEN alebo TELEGRAM_CHAT_ID")
        return 1

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
    seen = load_seen()
    updated_seen = set(seen)
    stats = {"feeds_ok": 0, "feeds_failed": 0, "entries": 0, "matched": 0, "duplicates": 0, "sent": 0, "send_failed": 0}

    for source, url in FEEDS:
        entries = fetch_feed(session, source, url)
        if entries: stats["feeds_ok"] += 1
        else: stats["feeds_failed"] += 1
        for entry in entries:
            stats["entries"] += 1
            deal = classify(entry)
            if not deal: continue
            stats["matched"] += 1
            key = fingerprint(deal)
            # Backward compatibility: the previous tracker stored RSS GUIDs
            # or raw links in seen.json, rather than SHA-256 fingerprints.
            legacy_ids = {
                str(entry.get("id", "")),
                str(entry.get("link", "")),
                canonical_url(entry.get("link", "")),
            }
            legacy_ids.discard("")
            if key in updated_seen or legacy_ids.intersection(updated_seen):
                stats["duplicates"] += 1
                continue
            if send_telegram(session, token, chat_id, make_message(deal, source)):
                updated_seen.add(key)  # mark only after Telegram confirms delivery
                stats["sent"] += 1
                logging.info("Odoslaná ponuka: %s", deal["title"])
            else:
                stats["send_failed"] += 1  # retry next scheduled run

    save_seen(updated_seen)
    logging.info("SUMÁR | funkčné feedy: %(feeds_ok)s | nefunkčné feedy: %(feeds_failed)s | články: %(entries)s | zhody: %(matched)s | duplicity: %(duplicates)s | odoslané: %(sent)s | zlyhané odoslania: %(send_failed)s", stats)
    if stats["feeds_ok"] == 0:
        logging.error("Všetky RSS zdroje zlyhali")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

