"""Scrape all list member counts from the account's lists overview page.

Navigates to x.com/{account}/lists and extracts the member count for
each list in a single page load (~5s total). This is the optimized
alternative to scrape_list_members.py which navigates to each list
individually (~5s per list).

Returns a dict mapping list name → member count. The list names on X
may not match our label names exactly, so the caller matches by URL
or by fuzzy name matching.

Falls back to scrape_list_members.py (per-list) if this approach fails.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import traceback
from pathlib import Path

from mcp_browser import ChromeMCPBrowser, looks_challenged, looks_logged_in, looks_rate_limited, jitter

OUT_DIR = Path(os.environ.get("X_AUTOMATION_OUT_DIR", Path(__file__).resolve().parent.parent / "out"))
ACCOUNT_HANDLE = os.environ.get("X_ACCOUNT_HANDLE", "")
# Optional: map list URLs to labels so we can match by URL href on the page
LIST_URLS_JSON = os.environ.get("X_LIST_URLS_JSON", "{}")
SESSION_TIMEOUT_SECONDS = int(os.environ.get("X_SCRAPE_SESSION_TIMEOUT", "60"))

OUT_DIR.mkdir(parents=True, exist_ok=True)


def write_json(name: str, payload: dict) -> None:
    (OUT_DIR / name).write_text(json.dumps(payload, indent=2))


def extract_list_id(url: str) -> str | None:
    """Extract the numeric list ID from an X list URL."""
    match = re.search(r"/lists/(\d+)", url)
    return match.group(1) if match else None


async def main() -> None:
    result = {
        "status": "unknown",
        "task_type": "scrape_list_counts",
        "account": ACCOUNT_HANDLE,
        "counts": {},
    }
    browser_url = os.environ.get("BROWSER_URL") or os.environ.get("CDP_URL") or "http://127.0.0.1:9222"
    label_to_list_id: dict[str, str] = {}
    try:
        urls_map = json.loads(LIST_URLS_JSON)
        for label, url in urls_map.items():
            lid = extract_list_id(url)
            if lid:
                label_to_list_id[label] = lid
    except (json.JSONDecodeError, TypeError):
        pass

    try:
        async with ChromeMCPBrowser(browser_url) as browser:
            # Verify session
            await browser.navigate("https://x.com/home")
            await browser.wait_for_text(["For you", "Following", "Sign in to X"], timeout=20000)
            home = await browser.get_page_payload(3000)
            if looks_rate_limited(home["text"], home.get("url", "")):
                result["status"] = "error"
                result["error"] = "rate-limited"
                result["rate_limited"] = True
                write_json("scrape_list_counts.json", result)
                print(json.dumps(result))
                return
            if looks_challenged(home["text"]):
                result["status"] = "error"
                result["error"] = "challenge-detected"
                write_json("scrape_list_counts.json", result)
                print(json.dumps(result))
                return
            if not looks_logged_in(home["text"]):
                result["status"] = "error"
                result["error"] = "session-not-authenticated"
                write_json("scrape_list_counts.json", result)
                print(json.dumps(result))
                return

            if not ACCOUNT_HANDLE:
                result["status"] = "error"
                result["error"] = "missing-account-handle"
                write_json("scrape_list_counts.json", result)
                print(json.dumps(result))
                return

            # Navigate to the account's lists page
            lists_url = f"https://x.com/{ACCOUNT_HANDLE}/lists"
            await browser.navigate(lists_url)
            await browser.wait_for_text(
                ["Your Lists", "Lists", "Create a new List", "Pinned Lists"],
                timeout=20000,
            )
            await browser.sleep(jitter(2000, 500))

            page_payload = await browser.get_page_payload(5000)
            page_text = page_payload.get("text", "")

            if looks_rate_limited(page_text, page_payload.get("url", "")):
                result["status"] = "error"
                result["error"] = "rate-limited"
                result["rate_limited"] = True
                write_json("scrape_list_counts.json", result)
                print(json.dumps(result))
                return

            # Extract list cards with names, member counts, and URLs.
            # The lists overview page shows each list as a card/row with
            # a link containing a list ID and "N Members" text nearby.
            # Links may use /i/lists/{id} or /lists/{id} format.
            cards = await browser.evaluate("""() => {
                const results = [];
                // Find all links that look like list links (various URL formats)
                const links = Array.from(document.querySelectorAll('a[href*="/lists/"]'));
                const seen = new Set();
                for (const link of links) {
                    const href = link.getAttribute('href') || '';
                    // Match list ID from any format: /i/lists/123, /user/lists/123, /lists/123
                    const match = href.match(/\\/lists\\/(\\d+)/);
                    if (!match || seen.has(match[1])) continue;
                    seen.add(match[1]);

                    // Walk up to find the row/card container
                    let container = link;
                    for (let i = 0; i < 6; i++) {
                        if (container.parentElement) container = container.parentElement;
                    }
                    const text = container.innerText || '';

                    // Extract member count
                    const countMatch = text.match(/(\\d+)\\s+[Mm]embers?/);
                    results.push({
                        list_id: match[1],
                        href: href,
                        text: text.substring(0, 300),
                        member_count: countMatch ? parseInt(countMatch[1], 10) : null,
                    });
                }
                return results;
            }""")

            if not isinstance(cards, list):
                cards = []

            # Match cards to our labels by list ID
            counts_by_label: dict[str, int] = {}
            counts_by_id: dict[str, int] = {}
            for card in cards:
                list_id = str(card.get("list_id", ""))
                count = card.get("member_count")
                if list_id and count is not None:
                    counts_by_id[list_id] = count

            for label, list_id in label_to_list_id.items():
                if list_id in counts_by_id:
                    counts_by_label[label] = counts_by_id[list_id]

            result["status"] = "ok"
            result["counts"] = counts_by_label
            result["counts_by_id"] = counts_by_id
            result["cards_found"] = len(cards)
            result["evidence"] = {"screenshot_path": str(OUT_DIR / "scrape_list_counts.png")}
            await browser.screenshot(result["evidence"]["screenshot_path"], full_page=True)

    except Exception as error:
        result["status"] = "error"
        result["error"] = str(error)
        result["traceback"] = traceback.format_exc()

    write_json("scrape_list_counts.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        asyncio.run(asyncio.wait_for(main(), timeout=SESSION_TIMEOUT_SECONDS))
    except TimeoutError:
        payload = {
            "status": "error",
            "task_type": "scrape_list_counts",
            "error": f"session-timeout-{SESSION_TIMEOUT_SECONDS}s",
        }
        write_json("scrape_list_counts.json", payload)
        print(json.dumps(payload))
