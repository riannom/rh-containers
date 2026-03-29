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

from mcp_browser import ChromeMCPBrowser, looks_challenged, looks_logged_in, looks_rate_limited, jitter
from shared import OUT_DIR, write_json, resolve_browser_url

ACCOUNT_HANDLE = os.environ.get("X_ACCOUNT_HANDLE", "")
# Optional: map list URLs to labels so we can match by URL href on the page
LIST_URLS_JSON = os.environ.get("X_LIST_URLS_JSON", "{}")
SESSION_TIMEOUT_SECONDS = int(os.environ.get("X_SCRAPE_SESSION_TIMEOUT", "60"))


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
    browser_url = resolve_browser_url()
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

            # Scroll down to load all lists — some may be below the fold.
            # Two scrolls is enough for most accounts (4-8 lists).
            for _ in range(2):
                await browser.scroll_page()
                await browser.sleep(jitter(1000, 300))

            page_payload = await browser.get_page_payload(5000)
            page_text = page_payload.get("text", "")

            if looks_rate_limited(page_text, page_payload.get("url", "")):
                result["status"] = "error"
                result["error"] = "rate-limited"
                result["rate_limited"] = True
                write_json("scrape_list_counts.json", result)
                print(json.dumps(result))
                return

            # Parse list names and member counts from the page text.
            # X renders list cards without standard <a> links, so we parse
            # the visible text instead. Format: "List Name\n...N members" or
            # "List Name\n·N members"
            # We match our labels by normalizing list names on X.
            cards = []
            lines = page_text.split("\n")
            for i, line in enumerate(lines):
                count_match = re.search(r"[·.]?\s*(\d+)\s+[Mm]embers?", line)
                if count_match:
                    count = int(count_match.group(1))
                    # Look backwards for the list name (usually a few lines above)
                    name = ""
                    for j in range(max(0, i - 5), i):
                        candidate = lines[j].strip()
                        # Skip lines that look like metadata, not names
                        if candidate and not candidate.startswith("@") and "members" not in candidate.lower() and "followers" not in candidate.lower():
                            name = candidate
                    cards.append({"name": name, "member_count": count, "line": line.strip()})

            if not isinstance(cards, list):
                cards = []

            # Match cards to our labels by list ID
            counts_by_label: dict[str, int] = {}
            # Match cards to our labels by normalizing names.
            # X list names like "Breadth | Tier A Macro" → match "tier-a-macro"
            def normalize_name(name: str) -> str:
                """Normalize X list name to match our label format."""
                n = name.lower().strip()
                # Strip common prefixes like "Breadth |", "Core |", "Outlier |"
                for prefix in ("breadth |", "core |", "outlier |", "breadth|", "core|", "outlier|"):
                    if n.startswith(prefix):
                        n = n[len(prefix):].strip()
                # "Tier A Macro" → "tier-a-macro"
                return n.replace(" ", "-")

            for card in cards:
                name = card.get("name", "")
                count = card.get("member_count")
                if not name or count is None:
                    continue
                normalized = normalize_name(name)
                # Match against our label set
                for label in label_to_list_id:
                    if normalized == label or label in normalized or normalized in label:
                        counts_by_label[label] = count
                        break

            result["status"] = "ok"
            result["counts"] = counts_by_label
            result["cards"] = cards
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
