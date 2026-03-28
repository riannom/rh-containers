"""Scrape member count and handles from an X list.

Two-phase approach:
1. Navigate to the list's main page, read member count from header
2. Click through to /members and scroll to collect actual handles

The header count is the source of truth for verification. The handle
list is used by reconcile to compute diffs. If the /members page fails
to load, we still return the count from phase 1.

Empty lists show "It's lonely here" or similar — detected and returned
as 0 members.
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
LIST_URL = os.environ.get("X_LIST_URL", "")
MAX_SCROLLS = int(os.environ.get("X_SCRAPE_MAX_SCROLLS", "60"))
SESSION_TIMEOUT_SECONDS = int(os.environ.get("X_SCRAPE_SESSION_TIMEOUT", "300"))

OUT_DIR.mkdir(parents=True, exist_ok=True)


def write_json(name: str, payload: dict) -> None:
    (OUT_DIR / name).write_text(json.dumps(payload, indent=2))


def parse_member_count(text: str) -> int | None:
    """Extract member count from page text."""
    match = re.search(r"(\d+)\s+[Mm]embers?", text)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d+)\s+people in this [Ll]ist", text)
    if match:
        return int(match.group(1))
    return None


def looks_empty_list(text: str) -> bool:
    """Detect X's empty list indicators."""
    lower = text.lower()
    return any(phrase in lower for phrase in [
        "it\u2019s lonely here",
        "it's lonely here",
        "there isn\u2019t anyone in this list",
        "there isn't anyone in this list",
        "this list is empty",
    ])


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
            if consecutive_empty >= 5:
                break
        else:
            consecutive_empty = 0

        await browser.scroll_page()
        await browser.sleep(jitter(1500, 500))

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
            # Verify session
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

            # ── Phase 1: List main page — get member count from header ──
            list_url = LIST_URL.rstrip("/")
            await browser.navigate(list_url)
            await browser.wait_for_text(
                ["Edit List", "Waiting for posts", "Members", "members", "Following", "lonely"],
                timeout=20000,
            )
            await browser.sleep(jitter(2000, 500))

            page_payload = await browser.get_page_payload(3000)
            page_text = page_payload.get("text", "")

            if looks_rate_limited(page_text, page_payload.get("url", "")):
                result["status"] = "error"
                result["error"] = "rate-limited"
                result["rate_limited"] = True
                write_json("scrape_list_members.json", result)
                print(json.dumps(result))
                return
            if looks_challenged(page_text):
                result["status"] = "error"
                result["error"] = "challenge-detected"
                write_json("scrape_list_members.json", result)
                print(json.dumps(result))
                return

            # Empty list detection
            if looks_empty_list(page_text):
                result["status"] = "ok"
                result["member_count"] = 0
                result["members"] = []
                result["empty_list_detected"] = True
                result["evidence"] = {"screenshot_path": str(OUT_DIR / "scrape_list_members.png")}
                await browser.screenshot(result["evidence"]["screenshot_path"], full_page=True)
                write_json("scrape_list_members.json", result)
                print(json.dumps(result))
                return

            header_count = parse_member_count(page_text)
            result["member_count"] = header_count if header_count is not None else 0

            # ── Phase 2: /members page — collect actual handles ──
            try:
                members_url = list_url + "/members"
                await browser.navigate(members_url)
                await browser.wait_for_text(
                    ["Members", "members", "lonely", "people in this"],
                    timeout=20000,
                )
                await browser.sleep(jitter(2000, 500))

                members_payload = await browser.get_page_payload(3000)
                members_text = members_payload.get("text", "")

                if looks_empty_list(members_text):
                    result["members"] = []
                elif not looks_rate_limited(members_text, members_payload.get("url", "")):
                    members = await scrape_all_members(browser)
                    # Cross-check: if header said 0 but we found handles, discard (suggestions)
                    if header_count == 0 and members:
                        result["discarded_suggestions"] = len(members)
                    else:
                        result["members"] = members
            except Exception as members_err:
                # Phase 2 failure is non-fatal — we still have the count from phase 1
                result["members_scrape_error"] = str(members_err)

            result["status"] = "ok"
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
