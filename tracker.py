#!/usr/bin/env python3
"""Sleduje RSS feedy s akciami na letenky a posiela zhody na Telegram.

Filter: odlet z Viedne + jedna z tvojich aerolínií (+ voliteľne Ázia).
Beží v GitHub Actions, už videné ponuky si pamätá v seen.json.
"""
import json
import os
import re
import sys
from pathlib import Path

import feedparser
import requests

# ----------------------------------------------------------------------
# NASTAVENIA - uprav podľa seba
# ----------------------------------------------------------------------

# Sem vlož adresy RSS feedov. Na stránke portálu hľadaj ikonu RSS alebo
# skús pripísať /feed/ k adrese. Pred použitím si každú adresu over
# v prehliadači (musí sa zobraziť XML s ponukami).
FEEDS = [
     "https://www.secretflying.com/feed/",   # over, či funguje
    # "https://www.fly4free.com/feed/",        # over, či funguje
]

# Odlet - aspoň jedno z týchto slov musí byť v texte ponuky
ORIGIN_KEYWORDS = ["vienna", "viedeň", "viedne", "vídeň", "wien", "vie "]

# Aerolínie - aspoň jedna musí byť v texte ponuky
AIRLINES = {
    "EVA Air": ["eva air", "evaair"],
    "Austrian": ["austrian"],
    "Etihad": ["etihad"],
    "Qatar Airways": ["qatar"],
    "Emirates": ["emirates"],
}

# Priama stránka aerolínie, kde si ponuku overíš a kúpiš
AIRLINE_SITES = {
    "EVA Air": "https://www.evaair.com",
    "Austrian": "https://www.austrian.com",
    "Etihad": "https://www.etihad.com",
    "Qatar Airways": "https://www.qatarairways.com",
    "Emirates": "https://www.emirates.com",
}

# Priorita Ázia: True = posielať len ponuky do Ázie
ASIA_ONLY = True
ASIA_KEYWORDS = [
    "asia", "ázia", "ázie", "asien", "bangkok", "phuket", "singapore",
    "singapur", "tokyo", "tokio", "osaka", "japan", "japonsko", "seoul",
    "soul", "korea", "taipei", "taiwan", "taiwan", "hong kong", "hongkong",
    "bali", "denpasar", "jakarta", "kuala lumpur", "malaysia", "malajzia",
    "manila", "philippines", "filipíny", "hanoi", "ho chi minh", "vietnam",
    "delhi", "mumbai", "india", "indie", "maldives", "maldivy", "colombo",
    "sri lanka", "kathmandu", "nepal", "beijing", "peking", "shanghai",
    "šanghaj", "china", "čína", "thailand", "thajsko", "cambodia", "kambodža",
]

# Ponuky cez sprostredkovateľov: "flag" = označiť, "skip" = vynechať
RESELLER_KEYWORDS = ["kiwi.com", "skyscanner", "momondo", "kayak", "esky", "pelikan"]
RESELLER_MODE = "flag"

SEEN_FILE = Path(__file__).with_name("seen.json")

# ----------------------------------------------------------------------


def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(sorted(seen)[-2000:], indent=0))


def clean(text):
    return re.sub(r"<[^>]+>", " ", text or "")


def contains_any(text, words):
    return any(w in text for w in words)


def match(entry):
    text = (clean(entry.get("title", "")) + " " + clean(entry.get("summary", ""))).lower()
    text += " "  # aby sa "vie " zhodovalo aj na konci

    if not contains_any(text, ORIGIN_KEYWORDS):
        return None
    airlines = [name for name, kws in AIRLINES.items() if contains_any(text, kws)]
    if not airlines:
        return None
    if ASIA_ONLY and not contains_any(text, ASIA_KEYWORDS):
        return None

    link = entry.get("link", "")
    reseller = contains_any((link + " " + text), RESELLER_KEYWORDS)
    if reseller and RESELLER_MODE == "skip":
        return None
    return airlines, reseller


def send_telegram(token, chat_id, message):
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
        timeout=30,
    )
    r.raise_for_status()


def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        sys.exit("Chýba TELEGRAM_TOKEN alebo TELEGRAM_CHAT_ID")
    if not FEEDS:
        sys.exit("V FEEDS nie je žiadna adresa feedu")

    seen = load_seen()
    new_count = 0

    for url in FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            uid = entry.get("id") or entry.get("link")
            if not uid or uid in seen:
                continue
            seen.add(uid)

            result = match(entry)
            if not result:
                continue
            airlines, reseller = result

            lines = [
                "✈️ " + clean(entry.get("title", "")).strip(),
                "Aerolínia: " + ", ".join(airlines),
                "Ponuka: " + entry.get("link", ""),
            ]
            for a in airlines:
                lines.append(f"Kúpa priamo: {AIRLINE_SITES[a]}")
            if reseller:
                lines.append("⚠️ Ponuka môže ísť cez sprostredkovateľa, over na webe aerolínie.")
            send_telegram(token, chat_id, "\n".join(lines))
            new_count += 1

    save_seen(seen)
    print(f"Odoslaných ponúk: {new_count}")


if __name__ == "__main__":
    main()
