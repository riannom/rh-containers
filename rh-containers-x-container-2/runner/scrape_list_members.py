"""Scrape and validate X list membership.

Two-phase approach:
1. Navigate to the list's main page, read member count from header
2. Scroll /members and validate each handle against a desired set

Phase 1 is fast (~5s) and always runs — gives the member count.
Phase 2 is slow (30-300s) and only runs when the caller provides a
desired_handles set to validate against. It classifies each handle:
  - present + desired = correct
  - present + not desired = extra (shouldn't be here)
  - desired + not seen after full scroll = missing (needs adding)

Empty lists show "It's lonely here" — detected as 0 members.
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
DESIRED_HANDLES_JSON = os.environ.get("X_DESIRED_HANDLES_JSON", "[]")
SKIP_MEMBERS = os.environ.get("X_SCRAPE_SKIP_MEMBERS", "").lower() in ("1", "true", "yes")
MAX_SCROLLS = int(os.environ.get("X_SCRAPE_MAX_SCROLLS", "200"))
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


async def validate_members(browser: ChromeMCPBrowser, desired: set[str], expected_count: int = 0) -> dict:
    """Scroll /members and validate each handle against the desired set.

    Scrolls until we've seen all members (matching expected_count from
    phase 1) or exhausted the page. Uses a generous empty threshold
    to handle X's lazy loading.

    Returns:
        present: handles on the list that are in the desired set
        extra: handles on the list that are NOT in the desired set
        missing: desired handles not found on the list after scrolling
    """
    seen: set[str] = set()
    consecutive_empty = 0
    # Keep scrolling until we've found all members or truly hit bottom
    max_empty = 8  # generous threshold for lazy loading gaps
    target = expected_count if expected_count > 0 else 9999

    for _ in range(MAX_SCROLLS):
        handles = await collect_member_handles(browser)
        new_count = 0
        for handle in handles:
            if handle not in seen:
                seen.add(handle)
                new_count += 1

        if new_count == 0:
            consecutive_empty += 1
            # Stop if we've seen all expected members OR truly exhausted
            if len(seen) >= target or consecutive_empty >= max_empty:
                break
        else:
            consecutive_empty = 0

        # /members shows content in a modal — scroll the modal, not the page
        await browser.scroll_modal()
        await browser.sleep(jitter(1500, 500))

    present = sorted(seen & desired)
    extra = sorted(seen - desired)
    missing = sorted(desired - seen)

    return {
        "present": present,
        "extra": extra,
        "missing": missing,
        "total_seen": len(seen),
    }


async def main() -> None:
    desired = set(h.lower().strip() for h in json.loads(DESIRED_HANDLES_JSON) if h.strip())

    result = {
        "status": "unknown",
        "task_type": "scrape_list_members",
        "list_url": LIST_URL,
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

            list_url = LIST_URL.rstrip("/")

            # When desired handles are provided, skip the list main page
            # (count comes from bulk scraper) and go straight to /members.
            # Only visit the list page when we need the count (no desired set).
            if not desired or SKIP_MEMBERS:
                # ── Count-only mode: visit list page for member count ──
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

                if looks_empty_list(page_text):
                    result["status"] = "ok"
                    result["member_count"] = 0
                    result["empty_list_detected"] = True
                    result["evidence"] = {"screenshot_path": str(OUT_DIR / "scrape_list_members.png")}
                    await browser.screenshot(result["evidence"]["screenshot_path"], full_page=True)
                    write_json("scrape_list_members.json", result)
                    print(json.dumps(result))
                    return

                header_count = parse_member_count(page_text)
                result["member_count"] = header_count if header_count is not None else 0
                result["status"] = "ok"
                write_json("scrape_list_members.json", result)

            if SKIP_MEMBERS or not desired:
                result["evidence"] = {"screenshot_path": str(OUT_DIR / "scrape_list_members.png")}
                await browser.screenshot(result["evidence"]["screenshot_path"], full_page=True)
                write_json("scrape_list_members.json", result)
                print(json.dumps(result))
                return

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
                    result["missing"] = sorted(desired)
                    result["present"] = []
                    result["extra"] = []
                elif not looks_rate_limited(members_text, members_payload.get("url", "")):
                    validation = await validate_members(browser, desired, expected_count=result.get("member_count", 0))
                    result["present"] = validation["present"]
                    result["extra"] = validation["extra"]
                    result["missing"] = validation["missing"]
                    result["total_seen"] = validation["total_seen"]
            except Exception as members_err:
                result["members_scrape_error"] = str(members_err)

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
        # Phase 1 saves partial results to disk — read them back so the
        # count (at minimum) is returned even if phase 2 timed out.
        saved = OUT_DIR / "scrape_list_members.json"
        if saved.exists():
            try:
                payload = json.loads(saved.read_text())
                payload["timeout"] = True
                payload["error"] = f"session-timeout-{SESSION_TIMEOUT_SECONDS}s (partial results preserved)"
                write_json("scrape_list_members.json", payload)
                print(json.dumps(payload))
            except Exception:
                pass
            else:
                raise SystemExit(0)
        payload = {
            "status": "error",
            "task_type": "scrape_list_members",
            "error": f"session-timeout-{SESSION_TIMEOUT_SECONDS}s",
        }
        write_json("scrape_list_members.json", payload)
        print(json.dumps(payload))
