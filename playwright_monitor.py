from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE_URL = "https://trouverunlogement.lescrous.fr"
STATE_FILE = Path("data/seen_listings.json")
STORAGE_FILE = Path("crous_storage_state.json")
LISTING_RE = re.compile(r"/tools/(\d+)/accommodations/([^/?#]+)", re.I)
TIMEOUT = int(os.getenv("TIMEOUT_SECONDS", "30")) * 1000
DELAY = float(os.getenv("REQUEST_DELAY_SECONDS", "1.2"))

logging.basicConfig(level="INFO", format="%(asctime)s | %(levelname)s | %(message)s")


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Secret GitHub manquant : {name}")
    return value


def search_urls() -> list[str]:
    value = required_env("CROUS_SEARCH_URLS")
    urls = [x.strip() for x in value.splitlines() if x.strip()]
    if not urls:
        raise RuntimeError("Aucune URL CROUS")
    return list(dict.fromkeys(urls))


def load_storage_state() -> Path:
    encoded = required_env("CROUS_STORAGE_STATE_B64")
    try:
        STORAGE_FILE.write_bytes(base64.b64decode(encoded, validate=True))
    except Exception as exc:
        raise RuntimeError("CROUS_STORAGE_STATE_B64 n'est pas un base64 valide") from exc
    return STORAGE_FILE


def canonical_url(href: str) -> str:
    if href.startswith("/"):
        return BASE_URL + href
    return href


def uid(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    return hashlib.sha256(path.encode()).hexdigest()[:20]


def clean(text: str) -> str:
    return " ".join((text or "").split())


def card_from_link(link):
    for selector in ["article", "li", "[data-testid*='accommodation']", "[class*='accommodation']"]:
        try:
            card = link.locator("xpath=ancestor::" + selector, has=link).first
            if card.count():
                return card
        except Exception:
            pass
    return link.locator("xpath=..")


def extract_listing(link, source_url: str) -> dict | None:
    href = link.get_attribute("href") or ""
    match = LISTING_RE.search(href)
    if not match:
        return None

    url = canonical_url(match.group(0))
    card = card_from_link(link)
    try:
        text = clean(card.inner_text(timeout=2_000))
    except Exception:
        text = clean(link.inner_text(timeout=2_000))

    title = ""
    for selector in ["h1", "h2", "h3", "h4", "[role='heading']"]:
        try:
            candidate = clean(card.locator(selector).first.inner_text(timeout=1_000))
            if candidate:
                title = candidate
                break
        except Exception:
            pass
    if not title:
        title = clean(link.inner_text(timeout=2_000)) or "Logement CROUS disponible"

    price_match = re.search(r"\b\d{2,4}(?:[,.]\d{1,2})?\s*€", text)
    address_match = re.search(r"[^|\n]{0,120}\b\d{5}\b[^|\n]{0,120}", text)

    return {
        "uid": uid(url),
        "title": title[:160],
        "url": url,
        "price": price_match.group(0) if price_match else "",
        "surface": "",
        "address": clean(address_match.group(0)) if address_match else "",
        "details": text[:1000],
        "source": source_url,
    }


def fetch_search(page, source_url: str) -> dict[str, dict]:
    page.goto(source_url, wait_until="domcontentloaded", timeout=TIMEOUT)
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except PlaywrightTimeoutError:
        logging.info("networkidle non atteint ; la page est quand même continuee")

    # L'application charge parfois les cartes après le HTML initial.
    page.wait_for_timeout(2_000)

    for _ in range(6):
        page.mouse.wheel(0, 1800)
        page.wait_for_timeout(500)

    links = page.locator("a[href*='/tools/'][href*='/accommodations/']")
    count = links.count()
    logging.info("Playwright : %d lien(s) d'annonce visible(s) pour %s", count, source_url)

    found: dict[str, dict] = {}
    for i in range(count):
        try:
            item = extract_listing(links.nth(i), source_url)
        except Exception as exc:
            logging.warning("Annonce %s illisible : %s", i + 1, exc)
            continue
        if item:
            found[item["uid"]] = item

    body_text = clean(page.locator("body").inner_text(timeout=5_000)).lower()
    if count == 0 and "aucun logement" not in body_text and "aucune offre" not in body_text:
        logging.warning("Aucune carte extraite et aucun message 'aucun logement' détecté : possible changement de page.")

    return found


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"initialized": False, "seen": {}, "last_current": []}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def send_discord(item: dict) -> None:
    webhook = required_env("DISCORD_WEBHOOK_URL")
    fields = []
    if item["price"]:
        fields.append({"name": "Loyer", "value": item["price"], "inline": True})
    if item["surface"]:
        fields.append({"name": "Surface", "value": item["surface"], "inline": True})
    if item["address"]:
        fields.append({"name": "Adresse", "value": item["address"], "inline": False})

    payload = {
        "username": "Alerte CROUS",
        "content": "@everyone 🏠 **Nouveau logement CROUS détecté !**",
        "allowed_mentions": {"parse": ["everyone"]},
        "embeds": [{
            "title": item["title"],
            "url": item["url"],
            "description": "Une nouvelle disponibilité est visible dans ta recherche CROUS.",
            "fields": fields,
            "footer": {"text": "Surveillance CROUS via navigateur"},
        }],
    }
    response = requests.post(webhook, json=payload, timeout=30)
    response.raise_for_status()


def main() -> int:
    storage_file = load_storage_state()
    state = load_state()
    current: dict[str, dict] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(storage_file), locale="fr-FR")
        page = context.new_page()

        for url in search_urls():
            try:
                items = fetch_search(page, url)
                logging.info("Recherche %s : %d logement(s) visible(s)", url.split("/tools/")[-1], len(items))
                current.update(items)
            except Exception as exc:
                logging.error("Recherche Playwright en échec : %s", exc)
            time.sleep(DELAY)

        browser.close()

    if not current:
        logging.warning("Aucun logement visible dans la session CROUS.")

    seen: dict[str, dict] = state.get("seen", {})

    if not state.get("initialized", False):
        seen.update(current)
        save_state({"initialized": True, "seen": seen, "last_current": sorted(current)})
        logging.info("Initialisation : %d logement(s) mémorisé(s), aucune alerte envoyée.", len(current))
        return 0

    new_ids = sorted(set(current) - set(seen))
    logging.info("%d nouvelle(s) annonce(s) détectée(s).", len(new_ids))

    for item_id in new_ids:
        send_discord(current[item_id])
        seen[item_id] = current[item_id]
        time.sleep(1)

    # Les annonces disparues restent dans seen : si elles reviennent, elles seront de nouveau considérées comme nouvelles.
    save_state({"initialized": True, "seen": seen, "last_current": sorted(current)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
