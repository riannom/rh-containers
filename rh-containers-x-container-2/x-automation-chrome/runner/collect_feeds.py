from __future__ import annotations

import asyncio
import json
import os
import random
import re
from pathlib import Path

from mcp_browser import ChromeMCPBrowser, looks_logged_in, scroll_and_collect


OUT_DIR = Path(os.environ.get("X_AUTOMATION_OUT_DIR", Path(__file__).resolve().parent.parent / "out"))
SEEN_IDS_FILE = Path(os.environ["X_SEEN_IDS_FILE"]) if os.environ.get("X_SEEN_IDS_FILE") else None
LIST_URLS = json.loads(os.environ.get("X_LIST_URLS_JSON", "[]"))
LIST_LABELS = json.loads(os.environ.get("X_LIST_LABELS_JSON", "[]"))
COLLECT_FEED = os.environ.get("X_COLLECT_FEED") == "1"
MAX_SCROLLS = int(os.environ.get("X_MAX_SCROLLS", "8"))
MAX_SCROLLS_MIN = int(os.environ.get("X_MAX_SCROLLS_MIN", str(MAX_SCROLLS)))
MAX_SCROLLS_MAX = int(os.environ.get("X_MAX_SCROLLS_MAX", str(MAX_SCROLLS)))

OUT_DIR.mkdir(parents=True, exist_ok=True)


def write_json(name: str, payload: dict) -> None:
    (OUT_DIR / name).write_text(json.dumps(payload, indent=2))


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
    SEEN_IDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_IDS_FILE.write_text(
        json.dumps({"tweet_ids": sorted(seen_ids), "last_updated": asyncio.get_event_loop().time()}, indent=2)
    )


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


async def collect_source(browser: ChromeMCPBrowser, label: str, url: str, seen_ids: set[str]) -> dict:
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
    browser_url = os.environ.get("BROWSER_URL") or os.environ.get("CDP_URL") or "http://127.0.0.1:9222"
    seen_ids = load_seen_ids()

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
                    source = await collect_source(browser, label, list_url, seen_ids)
                except Exception as error:
                    source = {"label": label, "url": list_url, "status": "error", "error": str(error), "posts": []}
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
                except Exception as error:
                    source["status"] = "error"
                    source["error"] = str(error)
                result["sources"].append(source)
                result["total_posts"] += source.get("post_count", 0)

            save_seen_ids(seen_ids)
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
