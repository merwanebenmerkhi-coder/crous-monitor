from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://trouverunlogement.lescrous.fr"
STATE_FILE = Path("data/seen_listings.json")
LISTING_PATTERN = re.compile(r"/tools/(\d+)/accommodations/[^/?#]+", re.IGNORECASE)
TOOL_PATTERN = re.compile(r"/tools/(\d+)/search(?:/|$)", re.IGNORECASE)
MAX_PAGES = int(os.getenv("MAX_PAGES", "10"))
REQUEST_DELAY_SECONDS = float(os.getenv("REQUEST_DELAY_SECONDS", "1.2"))
TIMEOUT_SECONDS = int(os.getenv("TIMEOUT_SECONDS", "25"))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": "CrousDiscordMonitor/3.0 (personal availability notifier)",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7",
        "Cache-Control": "no-cache",
    }
)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Secret GitHub manquant : {name}")
    return value


def get_search_urls() -> list[str]:
    urls = [
        line.strip()
        for line in required_env("CROUS_SEARCH_URLS").splitlines()
        if line.strip()
    ]
    if not urls:
        raise RuntimeError("Aucune URL CROUS fournie.")
    if any(not url.startswith(BASE_URL) for url in urls):
        raise RuntimeError("Chaque URL doit provenir du site trouverunlogement.lescrous.fr")
    return list(dict.fromkeys(urls))


def clean_text(text: str) -> str:
    return " ".join(text.split())


def add_page_parameter(url: str, page: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["page"] = str(page)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def listing_id(url: str) -> str:
    canonical_path = urlsplit(url).path.rstrip("/")
    return hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()[:20]


def extract_card(anchor):
    for tag in ("article", "li"):
        card = anchor.find_parent(tag)
        if card is not None:
            return card
    return anchor.parent


def extract_listing(anchor, source_url: str) -> dict | None:
    href = anchor.get("href", "")
    match = LISTING_PATTERN.search(href)
    if not match:
        return None

    url = urljoin(BASE_URL, match.group(0))
    card = extract_card(anchor)
    text = clean_text(card.get_text(" ", strip=True))

    heading = card.find(["h2", "h3", "h4"])
    title = clean_text(heading.get_text(" ", strip=True)) if heading else ""
    if not title:
        title = clean_text(anchor.get_text(" ", strip=True))
    if not title:
        title = "Logement CROUS disponible"

    price_match = re.search(r"\b\d{2,4}(?:[,.]\d{1,2})?\s*€", text)
    surface_match = re.search(
        r"\b(?:de\s+)?\d{1,3}(?:[,.]\d+)?(?:\s+à\s+\d{1,3}(?:[,.]\d+)?)?\s*m²",
        text,
        flags=re.IGNORECASE,
    )

    address = ""
    for element in card.find_all(["p", "address", "span"]):
        candidate = clean_text(element.get_text(" ", strip=True))
        if re.search(r"\b\d{5}\b", candidate) and len(candidate) <= 180:
            address = candidate
            break

    return {
        "uid": listing_id(url),
        "title": title[:160],
        "url": url,
        "price": price_match.group(0) if price_match else "",
        "surface": surface_match.group(0) if surface_match else "",
        "address": address[:220],
        "details": text[:900],
        "source": source_url,
    }


def extract_tool_id(search_url: str) -> int | None:
    match = TOOL_PATTERN.search(urlsplit(search_url).path)
    return int(match.group(1)) if match else None


def api_listing(item: dict, search_url: str, tool_id: int) -> dict | None:
    item_id = item.get("id")
    residence = item.get("residence") or {}
    if item_id is None:
        return None

    url = f"{BASE_URL}/tools/{tool_id}/accommodations/{item_id}"
    label = clean_text(str(item.get("label") or "Logement CROUS disponible"))
    residence_label = clean_text(str(residence.get("label") or ""))
    title = residence_label or label
    if residence_label and label and label.lower() not in residence_label.lower():
        title = f"{residence_label} — {label}"

    address = clean_text(str(residence.get("address") or ""))
    area = item.get("area") or {}
    area_min = area.get("min")
    area_max = area.get("max")
    if area_min is not None and area_max is not None and area_min != area_max:
        surface = f"de {area_min} à {area_max} m²"
    elif area_min is not None:
        surface = f"{area_min} m²"
    elif area_max is not None:
        surface = f"{area_max} m²"
    else:
        surface = ""

    rents = []
    for mode in item.get("occupationModes") or []:
        rent = mode.get("rent") or {}
        for key in ("min", "max"):
            value = rent.get(key)
            if isinstance(value, (int, float)):
                rents.append(value / 100)
    if rents:
        low, high = min(rents), max(rents)
        price = f"{low:.2f} €" if low == high else f"de {low:.2f} à {high:.2f} €"
    else:
        price = ""

    return {
        "uid": listing_id(url),
        "title": title[:160],
        "url": url,
        "price": price,
        "surface": surface,
        "address": address[:220],
        "details": json.dumps(item, ensure_ascii=False)[:900],
        "source": search_url,
    }


def fetch_api_search(search_url: str) -> dict[str, dict]:
    tool_id = extract_tool_id(search_url)
    if tool_id is None:
        logging.warning("Impossible de déterminer l'id du moteur CROUS : %s", search_url)
        return {}

    endpoint = f"{BASE_URL}/api/fr/search/{tool_id}"
    found: dict[str, dict] = {}

    for page_number in range(1, MAX_PAGES + 1):
        body = {
            "precision": 5,
            "need_aggregation": False,
            "page": page_number,
            "pageSize": 100,
            "sector": None,
            "idTool": tool_id,
            "occupationModes": ["alone", "couple", "house_sharing"],
            "equipment": [],
            "price": {"min": 0, "max": None},
            "location": [
                {"lon": -5.2, "lat": 51.2},
                {"lon": 9.7, "lat": 41.2},
            ],
        }
        response = SESSION.post(
            endpoint,
            json=body,
            headers={"Accept": "application/ld+json, application/json"},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
        items = ((data.get("results") or {}).get("items") or [])
        logging.info("API CROUS tool=%s page=%s : %d logement(s)", tool_id, page_number, len(items))

        for raw_item in items:
            item = api_listing(raw_item, search_url, tool_id)
            if item:
                found[item["uid"]] = item

        total = (data.get("results") or {}).get("total")
        if not items or (isinstance(total, int) and len(found) >= total):
            break
        time.sleep(REQUEST_DELAY_SECONDS)

    return found


def fetch_search(search_url: str) -> tuple[dict[str, dict], bool]:
    found: dict[str, dict] = {}
    html_succeeded = False

    for page_number in range(1, MAX_PAGES + 1):
        url = add_page_parameter(search_url, page_number)
        response = SESSION.get(url, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        page_text = clean_text(soup.get_text(" ", strip=True)).lower()

        if "vous êtes trop nombreux" in page_text:
            logging.warning("Surcharge temporaire CROUS : %s", url)
            break

        page_items: dict[str, dict] = {}
        for anchor in soup.find_all("a", href=True):
            item = extract_listing(anchor, search_url)
            if item:
                page_items[item["uid"]] = item

        if page_items:
            html_succeeded = True
            logging.info("HTML CROUS page=%s : %d logement(s)", page_number, len(page_items))
            before = len(found)
            found.update(page_items)
            if len(found) == before:
                break
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        logging.warning(
            "HTML CROUS sans annonce : status=%s, bytes=%d, page=%s",
            response.status_code,
            len(response.content),
            page_number,
        )
        break

    if found:
        return found, True

    logging.info("Tentative de récupération via l'API interne CROUS...")
    api_found = fetch_api_search(search_url)
    if api_found:
        return api_found, True

    if html_succeeded:
        return found, True

    raise RuntimeError(
        "Le CROUS répond mais aucune annonce n'a pu être extraite. "
        "La page peut nécessiter une authentification ou la structure/API a changé."
    )


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"initialized": False, "seen": {}, "last_current": []}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def post_discord(payload: dict) -> None:
    response = requests.post(
        required_env("DISCORD_WEBHOOK_URL"),
        json=payload,
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def send_listing(item: dict) -> None:
    fields = []
    if item["price"]:
        fields.append({"name": "Loyer", "value": item["price"], "inline": True})
    if item["surface"]:
        fields.append({"name": "Surface", "value": item["surface"], "inline": True})
    if item["address"]:
        fields.append({"name": "Adresse", "value": item["address"], "inline": False})

    post_discord(
        {
            "username": "Alerte CROUS",
            "content": "@everyone 🏠 **Nouveau logement CROUS détecté !**",
            "allowed_mentions": {"parse": ["everyone"]},
            "embeds": [
                {
                    "title": item["title"],
                    "url": item["url"],
                    "description": (
                        "Une nouvelle disponibilité correspond à ta recherche.\n"
                        "**Ouvre immédiatement l’annonce.**"
                    ),
                    "fields": fields,
                    "footer": {"text": "Surveillance planifiée toutes les 5 minutes"},
                }
            ],
        }
    )


def main() -> int:
    state = load_state()
    current: dict[str, dict] = {}
    successful_searches = 0

    for search_url in get_search_urls():
        try:
            listings, succeeded = fetch_search(search_url)
            logging.info("Recherche %s : %d logement(s) récupéré(s)", search_url.split("/tools/")[-1], len(listings))
            current.update(listings)
            successful_searches += int(succeeded)
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            logging.error("Recherche en échec : %s", exc)

    if successful_searches == 0:
        logging.error("Aucune recherche n’a abouti. L’état précédent est conservé.")
        return 2

    seen: dict[str, dict] = state.get("seen", {})

    if not state.get("initialized", False):
        seen.update(current)
        save_state(
            {
                "initialized": True,
                "seen": seen,
                "last_current": sorted(current),
            }
        )
        post_discord(
            {
                "username": "Alerte CROUS",
                "content": (
                    f"✅ Surveillance activée : {len(current)} logement(s) "
                    "déjà présent(s) mémorisé(s)."
                ),
            }
        )
        return 0

    new_ids = sorted(set(current) - set(seen))
    logging.info("%d nouvelle(s) annonce(s) détectée(s).", len(new_ids))

    for item_id in new_ids:
        send_listing(current[item_id])
        seen[item_id] = current[item_id]
        time.sleep(1)

    save_state(
        {
            "initialized": True,
            "seen": seen,
            "last_current": sorted(current),
        }
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logging.exception("Erreur fatale")
        sys.exit(1)
