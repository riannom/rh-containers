from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp_browser import ChromeMCPBrowser, looks_logged_in, scroll_and_collect
from shared import OUT_DIR, write_json, resolve_browser_url


SEEN_IDS_FILE = Path(os.environ["X_SEEN_IDS_FILE"]) if os.environ.get("X_SEEN_IDS_FILE") else None
TWITTER_EPOCH_MS = 1288834974657
SEEN_TTL_DAYS = 7
HWM_FILE = Path(os.environ["X_HIGH_WATER_MARKS_FILE"]) if os.environ.get("X_HIGH_WATER_MARKS_FILE") else (
    SEEN_IDS_FILE.parent / "high_water_marks.json" if SEEN_IDS_FILE else None
)
LIST_URLS = json.loads(os.environ.get("X_LIST_URLS_JSON", "[]"))
LIST_LABELS = json.loads(os.environ.get("X_LIST_LABELS_JSON", "[]"))
COLLECT_FEED = os.environ.get("X_COLLECT_FEED") == "1"
MAX_SCROLLS = int(os.environ.get("X_MAX_SCROLLS", "8"))
MAX_SCROLLS_MIN = int(os.environ.get("X_MAX_SCROLLS_MIN", str(MAX_SCROLLS)))
MAX_SCROLLS_MAX = int(os.environ.get("X_MAX_SCROLLS_MAX", str(MAX_SCROLLS)))


def snowflake_timestamp_ms(tweet_id: str) -> int:
    return (int(tweet_id) >> 22) + TWITTER_EPOCH_MS


def prune_seen_ids(seen_ids: set[str], ttl_days: int = SEEN_TTL_DAYS) -> set[str]:
    cutoff_ms = (time.time() * 1000) - (ttl_days * 86400 * 1000)
    pruned = set()
    for tid in seen_ids:
        try:
            if snowflake_timestamp_ms(tid) >= cutoff_ms:
                pruned.add(tid)
        except (ValueError, OverflowError):
            pruned.add(tid)
    return pruned


def load_seen_ids() -> set[str]:
    if not SEEN_IDS_FILE or not SEEN_IDS_FILE.exists():
        return set()
    try:
        return set(json.loads(SEEN_IDS_FILE.read_text()).get("tweet_ids", []))
    except Exception:
        return set()


def save_seen_ids(seen_ids: set[str]) -> None:
    if not SEEN_IDS_FILE:
        return
    try:
        pruned = prune_seen_ids(seen_ids)
        SEEN_IDS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SEEN_IDS_FILE.write_text(
            json.dumps({"tweet_ids": sorted(pruned), "last_updated": datetime.now(timezone.utc).isoformat()}, indent=2)
        )
    except PermissionError:
        print(f"WARNING: Cannot write seen IDs to {SEEN_IDS_FILE} (permission denied)", flush=True)


def load_high_water_marks() -> dict[str, str]:
    if not HWM_FILE or not HWM_FILE.exists():
        return {}
    try:
        return json.loads(HWM_FILE.read_text()).get("marks", {})
    except Exception:
        return {}


def save_high_water_marks(marks: dict[str, str]) -> None:
    if not HWM_FILE:
        return
    try:
        HWM_FILE.parent.mkdir(parents=True, exist_ok=True)
        HWM_FILE.write_text(json.dumps({
            "marks": marks,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }, indent=2))
    except PermissionError:
        print(f"WARNING: Cannot write high-water marks to {HWM_FILE}", flush=True)


def update_high_water_mark(marks: dict[str, str], label: str, posts: list[dict]) -> None:
    max_id = marks.get(label, "0")
    for post in posts:
        tid = post.get("tweet_id")
        if tid:
            try:
                if int(tid) > int(max_id):
                    max_id = tid
            except (ValueError, TypeError):
                pass
    if max_id != "0":
        marks[label] = max_id


def is_empty_or_missing(body: str) -> bool:
    return bool(re.search(r"doesn't exist|not found|This List is empty", body, re.IGNORECASE) and len(body) < 500)


def collect_profiles_from_posts(posts: list[dict]) -> list[dict]:
    profiles = {}
    for post in posts:
        handle = str(post.get("handle") or "").strip()
        if not handle:
            continue
        profiles[handle.lower()] = {
            "handle": handle,
            "display_name": post.get("display_name"),
            "avatar_url": post.get("avatar_url"),
            "verified": post.get("verified"),
        }
    return list(profiles.values())


def choose_scroll_budget() -> int:
    low = min(MAX_SCROLLS_MIN, MAX_SCROLLS_MAX)
    high = max(MAX_SCROLLS_MIN, MAX_SCROLLS_MAX)
    return random.randint(low, high)


async def collect_source(browser: ChromeMCPBrowser, label: str, url: str, seen_ids: set[str], hwm: dict[str, str] | None = None) -> dict:
    source = {"label": label, "url": url, "status": "unknown", "posts": [], "profiles": []}
    last_error = None
    scroll_budget = choose_scroll_budget()
    source["max_scrolls_used"] = scroll_budget

    for attempt in range(3):
        try:
            await browser.navigate(url)
            await browser.sleep(4000 + attempt * 1500)
            page = await browser.get_page_payload(5000)
            body = page["text"]

            if is_empty_or_missing(body):
                source["status"] = "empty-or-not-found"
                return source

            posts = await scroll_and_collect(
                browser,
                seen_tweet_ids=seen_ids,
                max_scrolls=scroll_budget,
                posts_per_scroll=20,
                since_id=(hwm or {}).get(label),
            )
            for post in posts:
                post["collection_method"] = "list"
                post["collection_source"] = label
                if post.get("tweet_id"):
                    seen_ids.add(post["tweet_id"])
            source["posts"] = posts
            source["profiles"] = collect_profiles_from_posts(posts)
            source["post_count"] = len(posts)
            source["status"] = "ok"

            # Diagnostic: capture page state when a list yields 0 posts
            if not posts and "/lists/" in url:
                snippet = body[:500] if body else "(empty page)"
                source["diagnostic"] = {
                    "page_snippet": snippet,
                    "page_length": len(body) if body else 0,
                    "article_count": await browser.evaluate("() => document.querySelectorAll('article').length"),
                    "warning": f"List '{label}' loaded but returned 0 posts after {scroll_budget} scrolls",
                }
                print(f"WARNING: list '{label}' returned 0 posts — page_len={len(body)}, articles={source['diagnostic']['article_count']}", flush=True)
            return source
        except Exception as error:
            last_error = error
            if "Execution context was destroyed" in str(error) and attempt < 2:
                await browser.sleep(2500 + attempt * 1000)
                continue
            raise

    if last_error:
        raise last_error
    return source


async def main() -> None:
    result = {
        "status": "unknown",
        "task_type": "collect_feeds",
        "sources": [],
        "total_posts": 0,
        "collection_method": "feed",
    }
    browser_url = resolve_browser_url()
    seen_ids = load_seen_ids()
    hwm = load_high_water_marks()

    try:
        async with ChromeMCPBrowser(browser_url) as browser:
            await browser.navigate("https://x.com/home")
            await browser.wait_for_text(["For you", "Following", "Sign in to X"], timeout=20000)
            home = await browser.get_page_payload(3000)
            if not looks_logged_in(home["text"]):
                result["status"] = "error"
                result["error"] = "session-not-authenticated"
                write_json("collect_feeds.json", result)
                print(json.dumps(result))
                return

            for idx, list_url in enumerate(LIST_URLS):
                label = LIST_LABELS[idx] if idx < len(LIST_LABELS) else f"list-{idx}"
                try:
                    source = await collect_source(browser, label, list_url, seen_ids, hwm)
                except Exception as error:
                    source = {"label": label, "url": list_url, "status": "error", "error": str(error), "posts": []}
                update_high_water_mark(hwm, label, source.get("posts", []))
                result["sources"].append(source)
                result["total_posts"] += source.get("post_count", 0)
                if idx < len(LIST_URLS) - 1:
                    await browser.sleep(2500)

            if COLLECT_FEED:
                source = {"label": "following-feed", "url": "https://x.com/home", "status": "unknown", "posts": [], "profiles": []}
                source["max_scrolls_used"] = choose_scroll_budget()
                try:
                    await browser.navigate("https://x.com/home")
                    await browser.wait_for_text(["For you", "Following"], timeout=15000)
                    await browser.click_following_tab()
                    await browser.sleep(3000)
                    posts = await scroll_and_collect(
                        browser,
                        seen_tweet_ids=seen_ids,
                        max_scrolls=source["max_scrolls_used"],
                        posts_per_scroll=20,
                        since_id=hwm.get("following-feed"),
                    )
                    for post in posts:
                        post["collection_method"] = "feed"
                        post["collection_source"] = "following-feed"
                        if post.get("tweet_id"):
                            seen_ids.add(post["tweet_id"])
                    source["posts"] = posts
                    source["profiles"] = collect_profiles_from_posts(posts)
                    source["post_count"] = len(posts)
                    source["status"] = "ok"
                    update_high_water_mark(hwm, "following-feed", posts)
                except Exception as error:
                    source["status"] = "error"
                    source["error"] = str(error)
                result["sources"].append(source)
                result["total_posts"] += source.get("post_count", 0)

            save_seen_ids(seen_ids)
            save_high_water_marks(hwm)
            result["status"] = "ok"
            result["evidence"] = {
                "screenshot_path": str(OUT_DIR / "collect_feeds_final.png"),
                "visited_urls": [source["url"] for source in result["sources"]],
            }
            await browser.screenshot(result["evidence"]["screenshot_path"], full_page=True)
    except Exception as error:
        result["status"] = "error"
        result["error"] = str(error)

    write_json("collect_feeds.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
