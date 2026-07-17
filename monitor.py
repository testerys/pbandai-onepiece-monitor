import asyncio
import hashlib
import html as html_lib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

TIMEZONE_NAME = os.getenv("TIMEZONE", "America/Toronto")
TZ = ZoneInfo(TIMEZONE_NAME)
STATE_FILE = Path(os.getenv("STATE_FILE", "state/state.json"))
DASHBOARD_FILE = Path(os.getenv("DASHBOARD_FILE", "docs/index.html"))

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_MENTION = os.getenv("DISCORD_MENTION", "").strip()
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_URL = os.getenv("NTFY_URL", "https://ntfy.sh").rstrip("/")

REMINDER_MINUTES = (1440, 60, 15, 5)

SOURCES = (
    {
        "key": "pbandai_us",
        "name": "Premium Bandai US — ONE PIECE CARD GAME",
        "region": "US",
        "url": "https://p-bandai.com/us/brand/onepiececardgame",
        "kind": "products",
    },
    {
        "key": "onepiece_news",
        "name": "Official ONE PIECE CARD GAME News",
        "region": "Global",
        "url": "https://en.onepiece-cardgame.com/topics/",
        "kind": "news",
    },
)


@dataclass
class Item:
    source_key: str
    source_name: str
    region: str
    item_id: str
    title: str
    url: str
    image_url: Optional[str] = None
    preorder_at: Optional[str] = None
    status: str = "unknown"
    button_text: str = ""
    item_type: str = "product"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def stable_id(url: str, title: str = "") -> str:
    match = re.search(r"/item/([A-Za-z0-9_-]+)", url)
    if match:
        return match.group(1)
    return hashlib.sha256(f"{url}|{title}".encode()).hexdigest()[:24]


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"initialized_sources": [], "items": {}, "last_check": None}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def discord_time(dt: datetime) -> str:
    stamp = int(dt.timestamp())
    return f"<t:{stamp}:F> (<t:{stamp}:R>)"


def ascii_header(value: str) -> str:
    value = re.sub(r"[^\x20-\x7E]+", "", value)
    return value.strip() or "P-Bandai Alert"


async def fetch(url: str, retries: int = 3) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/131 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml",
    }
    timeout = httpx.Timeout(40.0)
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=timeout) as client:
        last_error = None
        for attempt in range(retries):
            try:
                response = await client.get(url)
                response.raise_for_status()
                return response.text
            except Exception as exc:
                last_error = exc
                if attempt + 1 < retries:
                    await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"Failed to fetch {url}: {last_error}")


async def browser_html(browser, url: str) -> str:
    page = await browser.new_page(
        viewport={"width": 1440, "height": 1400},
        locale="en-US",
        timezone_id=TIMEZONE_NAME,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/131 Safari/537.36"
        ),
    )
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        try:
            await page.wait_for_load_state("networkidle", timeout=25000)
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(3000)
        return await page.content()
    finally:
        await page.close()


def parse_preorder_time(text: str) -> Optional[datetime]:
    text = clean(text)
    pattern = (
        r"(?:pre[- ]?order|orders?|sales?)\s*"
        r"(?:start|starts|open|opens|begin|begins|available from)"
        r"\s*[:\-]?\s*(.{0,140}?(?:AM|PM|am|pm)"
        r"(?:\s*(?:EDT|EST|ET|PDT|PST|PT|CDT|CST|CT))?)"
    )
    match = re.search(pattern, text, re.I)
    if not match:
        return None

    candidate = clean(match.group(1))
    offsets = {
        "EDT": "-0400", "EST": "-0500",
        "CDT": "-0500", "CST": "-0600",
        "PDT": "-0700", "PST": "-0800",
    }
    candidate = re.sub(
        r"\b(EDT|EST|CDT|CST|PDT|PST)\b",
        lambda m: offsets[m.group(1).upper()],
        candidate,
        flags=re.I,
    )
    candidate = re.sub(r"\b(?:ET|CT|PT)\b", "", candidate, flags=re.I)

    try:
        dt = dateparser.parse(candidate, fuzzy=True)
    except Exception:
        return None

    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(timezone.utc)


def detect_status(text: str, button_text: str = "") -> str:
    low = f"{clean(text)} {clean(button_text)}".lower()
    if any(x in low for x in (
        "sold out", "out of stock", "pre-orders closed",
        "preorders closed", "orders closed",
    )):
        return "sold_out"
    if any(x in low for x in (
        "add to cart", "pre-order now", "preorder now",
        "order now", "in stock",
    )):
        return "available"
    if any(x in low for x in (
        "coming soon", "pre-order starts",
        "preorder starts", "orders start",
    )):
        return "coming_soon"
    return "unknown"


def extract_product_items(source: dict, page_html: str) -> list[Item]:
    soup = BeautifulSoup(page_html, "html.parser")
    items: dict[str, Item] = {}

    def add(url: str, title: str = "", image: Optional[str] = None) -> None:
        if not url:
            return
        url = urljoin(source["url"], url).split("#")[0]
        parsed = urlparse(url)
        if "p-bandai.com" not in parsed.netloc or "/item/" not in parsed.path:
            return
        title = clean(title) or "ONE PIECE CARD GAME product"
        item_id = stable_id(url, title)
        candidate = Item(
            source_key=source["key"],
            source_name=source["name"],
            region=source["region"],
            item_id=item_id,
            title=title[:250],
            url=url,
            image_url=urljoin(source["url"], image) if image else None,
        )
        existing = items.get(item_id)
        if not existing or len(candidate.title) > len(existing.title):
            items[item_id] = candidate

    for anchor in soup.select('a[href*="/item/"]'):
        image_el = anchor.find("img")
        image = None
        alt = ""
        if image_el:
            image = (
                image_el.get("src")
                or image_el.get("data-src")
                or image_el.get("data-lazy-src")
            )
            alt = image_el.get("alt", "")
        title = clean(anchor.get_text(" ", strip=True)) or clean(alt)
        add(anchor.get("href", ""), title, image)

    for match in re.finditer(
        r"(?:https?:\\/\\/p-bandai\.com)?\\/us\\/item\\/[A-Za-z0-9_-]+"
        r"|https?://p-bandai\.com/us/item/[A-Za-z0-9_-]+"
        r"|/us/item/[A-Za-z0-9_-]+",
        page_html,
    ):
        add(match.group(0).replace("\\/", "/"))

    return list(items.values())


def extract_news_items(source: dict, page_html: str) -> list[Item]:
    soup = BeautifulSoup(page_html, "html.parser")
    items: dict[str, Item] = {}

    for anchor in soup.select('a[href*="/topics/"]'):
        url = urljoin(source["url"], anchor.get("href", "")).split("#")[0]
        if url.rstrip("/") == source["url"].rstrip("/"):
            continue
        title = clean(anchor.get_text(" ", strip=True))
        image_el = anchor.find("img")
        if not title and image_el:
            title = clean(image_el.get("alt", ""))
        if not title or len(title) < 5:
            continue
        item_id = stable_id(url, title)
        items[item_id] = Item(
            source_key=source["key"],
            source_name=source["name"],
            region=source["region"],
            item_id=item_id,
            title=title[:250],
            url=url,
            image_url=urljoin(source["url"], image_el.get("src"))
            if image_el and image_el.get("src") else None,
            item_type="news",
        )

    return list(items.values())


async def enrich_product(browser, item: Item) -> Item:
    # Direct HTTP is tried first. Premium Bandai currently returns 501 for some
    # detail pages, so Chromium is the fallback.
    try:
        page_html = await fetch(item.url, retries=1)
        print(f"Detail HTTP OK: {item.item_id}")
    except Exception as exc:
        print(f"Detail HTTP failed for {item.item_id}: {exc}")
        try:
            page_html = await browser_html(browser, item.url)
            print(f"Detail browser fallback OK: {item.item_id}")
        except Exception as browser_exc:
            print(f"Detail browser fallback failed for {item.item_id}: {browser_exc}")
            return item

    soup = BeautifulSoup(page_html, "html.parser")
    text = clean(soup.get_text(" ", strip=True))
    buttons = []
    for el in soup.select("button,a.btn,a.button,[role=button],.cart,.purchase,.order,.stock"):
        value = clean(el.get_text(" ", strip=True))
        if value and len(value) <= 120:
            buttons.append(value)

    item.button_text = " | ".join(dict.fromkeys(buttons))[:500]
    item.status = detect_status(text, item.button_text)

    opening = parse_preorder_time(text)
    if opening:
        item.preorder_at = opening.isoformat()

    og_title = soup.select_one('meta[property="og:title"]')
    og_image = soup.select_one('meta[property="og:image"]')
    if og_title and clean(og_title.get("content", "")):
        item.title = clean(og_title["content"])[:250]
    if og_image and og_image.get("content"):
        item.image_url = urljoin(item.url, og_image["content"])

    return item


async def discord_send(title: str, description: str, item: Optional[Item] = None, color: int = 0x5865F2):
    if not DISCORD_WEBHOOK_URL:
        return
    embed = {
        "title": title[:256],
        "description": description[:4096],
        "color": color,
        "footer": {"text": "P-Bandai Monitor — GitHub V4.1"},
    }
    if item:
        embed["url"] = item.url
        if item.image_url:
            embed["thumbnail"] = {"url": item.image_url}

    payload = {
        "username": "P-Bandai Preorder Alerts",
        "content": DISCORD_MENTION or None,
        "allowed_mentions": {
            "parse": ["roles", "users", "everyone"] if DISCORD_MENTION else []
        },
        "embeds": [embed],
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
        response.raise_for_status()


async def ntfy_send(title: str, description: str, item: Optional[Item] = None, priority: str = "high"):
    if not NTFY_TOPIC:
        return
    headers = {"Title": ascii_header(title), "Priority": priority}
    if item:
        headers["Click"] = item.url
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{NTFY_URL}/{NTFY_TOPIC}",
            content=description.replace("**", "").encode("utf-8"),
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()


async def notify(title: str, description: str, item: Optional[Item] = None, color: int = 0x5865F2, priority: str = "high"):
    results = await asyncio.gather(
        discord_send(title, description, item, color),
        ntfy_send(title, description, item, priority),
        return_exceptions=True,
    )
    for service, result in zip(("Discord", "ntfy"), results):
        if isinstance(result, Exception):
            print(f"{service} notification failed: {result}")


def generate_dashboard(state: dict) -> None:
    items = list(state.get("items", {}).values())
    rows = []
    for item in items:
        rows.append(
            "<tr>"
            f"<td>{html_lib.escape(item.get('region', ''))}</td>"
            f"<td>{html_lib.escape(item.get('item_type', 'product'))}</td>"
            f"<td><a href=\"{html_lib.escape(item.get('url', ''))}\" target=\"_blank\">"
            f"{html_lib.escape(item.get('title', 'Untitled'))}</a></td>"
            f"<td>{html_lib.escape(item.get('status', 'unknown'))}</td>"
            f"<td>{html_lib.escape(item.get('preorder_at') or 'Not detected')}</td>"
            "</tr>"
        )

    DASHBOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_FILE.write_text(
        f"""<!doctype html><html><head><meta charset="utf-8">
<title>P-Bandai Monitor</title>
<style>
body{{font-family:Arial;max-width:1400px;margin:30px auto;padding:0 20px;background:#111;color:#eee}}
table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;border-bottom:1px solid #333;text-align:left}}
a{{color:#78b7ff}}
</style></head><body>
<h1>P-Bandai ONE PIECE Monitor</h1>
<p>Last check: {html_lib.escape(state.get('last_check') or 'Not run')}</p>
<table><tr><th>Region</th><th>Type</th><th>Item</th><th>Status</th><th>Preorder time</th></tr>
{''.join(rows) if rows else '<tr><td colspan="5">No items yet.</td></tr>'}
</table></body></html>""",
        encoding="utf-8",
    )


async def main():
    state = load_state()
    initialized = set(state.get("initialized_sources", []))
    saved_items = state.setdefault("items", {})
    check_time = now_utc()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            for source in SOURCES:
                try:
                    page_html = await fetch(source["url"])
                    discovered = (
                        extract_product_items(source, page_html)
                        if source["kind"] == "products"
                        else extract_news_items(source, page_html)
                    )
                    if not discovered:
                        raise RuntimeError("No matching items found")

                    print(f"{source['key']}: found {len(discovered)} item(s)")

                    for base_item in discovered:
                        item = (
                            await enrich_product(browser, base_item)
                            if source["kind"] == "products"
                            else base_item
                        )
                        key = f"{item.source_key}:{item.item_id}"
                        previous = saved_items.get(key)
                        is_new = previous is None

                        if is_new:
                            saved_items[key] = {
                                **asdict(item),
                                "first_seen": check_time.isoformat(),
                                "last_seen": check_time.isoformat(),
                                "alerts_sent": [],
                            }
                            if source["key"] in initialized:
                                await notify(
                                    "🆕 New public listing detected"
                                    if item.item_type == "product"
                                    else "📰 New official ONE PIECE news post",
                                    f"**{item.title}**\n\n[Open item]({item.url})",
                                    item,
                                    0x57F287,
                                    "high",
                                )
                        else:
                            old_status = previous.get("status", "unknown")
                            old_time = previous.get("preorder_at")
                            previous.update({
                                name: value
                                for name, value in asdict(item).items()
                                if value not in (None, "")
                            })
                            previous["last_seen"] = check_time.isoformat()

                            if not old_time and item.preorder_at:
                                await notify(
                                    "📅 Preorder opening time detected",
                                    f"**{item.title}**\n\n"
                                    f"**Opens:** {discord_time(parse_iso(item.preorder_at))}",
                                    item,
                                    0x3498DB,
                                    "high",
                                )

                            if (
                                item.item_type == "product"
                                and old_status in {"sold_out", "unknown", "coming_soon"}
                                and item.status == "available"
                            ):
                                alert_key = f"available:{item.button_text}"
                                sent = previous.setdefault("alerts_sent", [])
                                if alert_key not in sent:
                                    await notify(
                                        "🔥 Availability/restock detected",
                                        f"**{item.title}** changed from `{old_status}` "
                                        f"to **available**.\n\n[Open product page]({item.url})",
                                        item,
                                        0x9B59B6,
                                        "max",
                                    )
                                    sent.append(alert_key)

                    initialized.add(source["key"])

                except Exception as exc:
                    print(f"Source failed: {source['key']}: {exc}")

            for key, saved in saved_items.items():
                opening = parse_iso(saved.get("preorder_at"))
                if not opening:
                    continue

                item = Item(
                    source_key=saved.get("source_key", ""),
                    source_name=saved.get("source_name", ""),
                    region=saved.get("region", ""),
                    item_id=saved.get("item_id", key),
                    title=saved.get("title", "ONE PIECE product"),
                    url=saved.get("url", ""),
                    image_url=saved.get("image_url"),
                    preorder_at=saved.get("preorder_at"),
                    status=saved.get("status", "unknown"),
                    button_text=saved.get("button_text", ""),
                    item_type=saved.get("item_type", "product"),
                )
                sent = saved.setdefault("alerts_sent", [])

                if opening > check_time:
                    for minutes in REMINDER_MINUTES:
                        alert_key = f"reminder:{minutes}:{opening.isoformat()}"
                        target = opening - timedelta(minutes=minutes)
                        if target <= check_time < opening and alert_key not in sent:
                            label = (
                                "24 hours" if minutes == 1440
                                else "1 hour" if minutes == 60
                                else f"{minutes} minutes"
                            )
                            await notify(
                                f"⏰ Preorder opens in {label}",
                                f"**{item.title}**\n\n"
                                f"**Opens:** {discord_time(opening)}\n\n"
                                f"[Open product page]({item.url})",
                                item,
                                0xED4245 if minutes <= 15 else 0xFEE75C,
                                "max" if minutes <= 15 else "high",
                            )
                            sent.append(alert_key)

                live_key = f"live:{opening.isoformat()}"
                if opening <= check_time < opening + timedelta(minutes=10) and live_key not in sent:
                    await notify(
                        "🚨 Preorder should be LIVE now",
                        f"**{item.title}**\n\n[Open product page]({item.url})",
                        item,
                        0xED4245,
                        "max",
                    )
                    sent.append(live_key)

            state["initialized_sources"] = sorted(initialized)
            state["last_check"] = check_time.isoformat()
            save_state(state)
            generate_dashboard(state)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
