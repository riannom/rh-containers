"""Scrape all members from an X list by scrolling the members page.

Returns a flat list of handles currently on the list — used by
reconcile_x_lists to compute ground-truth diffs instead of relying
on a potentially-stale local cache.
"""
from __future__ import annotations

import asyncio
import json
import os
import traceback
from pathlib import Path

from mcp_browser import ChromeMCPBrowser, looks_challenged, looks_logged_in, looks_rate_limited, jitter

OUT_DIR = Path(os.environ.get("X_AUTOMATION_OUT_DIR", Path(__file__).resolve().parent.parent / "out"))
LIST_URL = os.environ.get("X_LIST_URL", "")
MAX_SCROLLS = int(os.environ.get("X_SCRAPE_MAX_SCROLLS", "40"))
SESSION_TIMEOUT_SECONDS = int(os.environ.get("X_SCRAPE_SESSION_TIMEOUT", "120"))

OUT_DIR.mkdir(parents=True, exist_ok=True)


def write_json(name: str, payload: dict) -> None:
    (OUT_DIR / name).write_text(json.dumps(payload, indent=2))


async def collect_member_handles(browser: ChromeMCPBrowser, limit: int = 500) -> list[str]:
    """Extract handles from visible UserCell elements on the page."""
    data = await browser.evaluate(
        f"""() => {{
            const cards = Array.from(document.querySelectorAll('[data-testid="UserCell"]')).slice(0, {limit});
            return cards.map((card) => {{
                const links = Array.from(card.querySelectorAll('a[href]'))
                    .map((link) => link.getAttribute('href'))
                    .filter(Boolean);
                const profileHref = links.find((href) => /^\\/[A-Za-z0-9_]+$/.test(href));
                return profileHref ? profileHref.slice(1).toLowerCase() : null;
            }}).filter(Boolean);
        }}"""
    )
    return data if isinstance(data, list) else []


async def scrape_all_members(browser: ChromeMCPBrowser) -> list[str]:
    """Scroll the members page and collect all unique handles."""
    seen: set[str] = set()
    consecutive_empty = 0

    for _ in range(MAX_SCROLLS):
        handles = await collect_member_handles(browser)
        new_count = 0
        for handle in handles:
            if handle not in seen:
                seen.add(handle)
                new_count += 1

        if new_count == 0:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
        else:
            consecutive_empty = 0

        await browser.scroll_page()
        await browser.sleep(jitter(800, 300))

    return sorted(seen)


async def main() -> None:
    result = {
        "status": "unknown",
        "task_type": "scrape_list_members",
        "list_url": LIST_URL,
        "members": [],
        "member_count": 0,
    }
    browser_url = os.environ.get("BROWSER_URL") or os.environ.get("CDP_URL") or "http://127.0.0.1:9222"

    try:
        async with ChromeMCPBrowser(browser_url) as browser:
            await browser.navigate("https://x.com/home")
            await browser.wait_for_text(["For you", "Following", "Sign in to X"], timeout=20000)
            home = await browser.get_page_payload(3000)
            if looks_rate_limited(home["text"], home.get("url", "")):
                result["status"] = "error"
                result["error"] = "rate-limited"
                result["rate_limited"] = True
                write_json("scrape_list_members.json", result)
                print(json.dumps(result))
                return
            if looks_challenged(home["text"]):
                result["status"] = "error"
                result["error"] = "challenge-detected"
                write_json("scrape_list_members.json", result)
                print(json.dumps(result))
                return
            if not looks_logged_in(home["text"]):
                result["status"] = "error"
                result["error"] = "session-not-authenticated"
                write_json("scrape_list_members.json", result)
                print(json.dumps(result))
                return

            if not LIST_URL:
                result["status"] = "error"
                result["error"] = "missing-list-url"
                write_json("scrape_list_members.json", result)
                print(json.dumps(result))
                return

            # Navigate to the list members page
            members_url = LIST_URL.rstrip("/") + "/members"
            await browser.navigate(members_url)
            await browser.wait_for_text(
                ["Members", "Edit List", "people in this List"],
                timeout=20000,
            )
            await browser.sleep(jitter(2000, 500))

            page_payload = await browser.get_page_payload(2500)
            if looks_rate_limited(page_payload["text"], page_payload.get("url", "")):
                result["status"] = "error"
                result["error"] = "rate-limited"
                result["rate_limited"] = True
                result["phase"] = "members-page"
                write_json("scrape_list_members.json", result)
                print(json.dumps(result))
                return
            if looks_challenged(page_payload["text"]):
                result["status"] = "error"
                result["error"] = "challenge-detected"
                result["phase"] = "members-page"
                write_json("scrape_list_members.json", result)
                print(json.dumps(result))
                return

            members = await scrape_all_members(browser)
            result["status"] = "ok"
            result["members"] = members
            result["member_count"] = len(members)

            result["evidence"] = {"screenshot_path": str(OUT_DIR / "scrape_list_members.png")}
            await browser.screenshot(result["evidence"]["screenshot_path"], full_page=True)

    except Exception as error:
        result["status"] = "error"
        result["error"] = str(error)
        result["traceback"] = traceback.format_exc()

    write_json("scrape_list_members.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        asyncio.run(asyncio.wait_for(main(), timeout=SESSION_TIMEOUT_SECONDS))
    except TimeoutError:
        payload = {
            "status": "error",
            "task_type": "scrape_list_members",
            "error": f"session-timeout-{SESSION_TIMEOUT_SECONDS}s",
        }
        write_json("scrape_list_members.json", payload)
        print(json.dumps(payload))
