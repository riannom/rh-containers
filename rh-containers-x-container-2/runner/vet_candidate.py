"""Browser-level timeline scraper for candidate vetting.

Follows the collect_relationships.py pattern: env vars in, JSON out,
asyncio + ChromeMCPBrowser. Collects 30-60 original tweets per candidate
for offline analysis.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re

from mcp_browser import ChromeMCPBrowser, looks_challenged, looks_logged_in, jitter
from shared import OUT_DIR, write_json, resolve_browser_url


HANDLES = json.loads(os.environ.get("X_VET_HANDLES_JSON", "[]"))
MAX_SCROLLS = int(os.environ.get("X_VET_MAX_SCROLLS", "6"))
MAX_SCROLLS_MIN = int(os.environ.get("X_VET_MAX_SCROLLS_MIN", str(MAX_SCROLLS)))
MAX_SCROLLS_MAX = int(os.environ.get("X_VET_MAX_SCROLLS_MAX", str(MAX_SCROLLS)))


def unavailable(body: str) -> bool:
    return bool(re.search(
        r"This account doesn't exist|Account suspended|These posts are protected",
        body, re.I,
    ))


def choose_scroll_budget(low: int, high: int) -> int:
    return random.randint(min(low, high), max(low, high))


async def collect_timeline_posts(
    browser: ChromeMCPBrowser,
    handle: str,
    scroll_budget: int,
) -> list[dict]:
    """Collect visible posts from a profile page with deduplication."""
    seen_ids: set[str] = set()
    posts: list[dict] = []

    initial = await browser.collect_visible_posts(limit=20)
    for post in initial:
        post_id = post.get("id") or post.get("text", "")[:80]
        if post_id in seen_ids:
            continue
        seen_ids.add(post_id)
        posts.append(post)

    for _ in range(scroll_budget):
        await browser.scroll_page()
        await browser.sleep(jitter(1800, 600))
        batch = await browser.collect_visible_posts(limit=20)
        new_count = 0
        for post in batch:
            post_id = post.get("id") or post.get("text", "")[:80]
            if post_id in seen_ids:
                continue
            seen_ids.add(post_id)
            posts.append(post)
            new_count += 1
        if new_count == 0:
            break

    return posts


def filter_own_posts(posts: list[dict], handle: str) -> list[dict]:
    """Keep only posts authored by the target handle."""
    handle_lower = handle.lower()
    own: list[dict] = []
    for post in posts:
        post_handle = str(post.get("handle") or "").lower()
        if post_handle == handle_lower or not post_handle:
            text = str(post.get("text") or "")
            if not text.startswith("RT @"):
                own.append(post)
    return own


async def main() -> None:
    result = {
        "status": "unknown",
        "task_type": "vet_candidate",
        "handle_results": [],
        "handle_errors": [],
    }
    browser_url = resolve_browser_url()

    try:
        async with ChromeMCPBrowser(browser_url) as browser:
            # Verify session
            await browser.navigate("https://x.com/home")
            await browser.wait_for_text(["For you", "Following", "Sign in to X"], timeout=20000)
            home = await browser.get_page_payload(3000)
            if looks_challenged(home["text"]):
                result["status"] = "error"
                result["error"] = "challenge-detected"
                write_json("vet_candidate.json", result)
                print(json.dumps(result))
                return
            if not looks_logged_in(home["text"]):
                result["status"] = "error"
                result["error"] = "session-not-authenticated"
                write_json("vet_candidate.json", result)
                print(json.dumps(result))
                return

            for handle in HANDLES:
                handle_result = {
                    "handle": handle,
                    "status": "unknown",
                    "profile": None,
                    "posts": [],
                    "own_posts": [],
                    "posts_collected": 0,
                    "own_posts_collected": 0,
                }
                try:
                    await browser.navigate(f"https://x.com/{handle}")
                    await browser.sleep(jitter(2200, 600))
                    payload = await browser.get_page_payload(3000)

                    if looks_challenged(payload["text"]):
                        result["status"] = "error"
                        result["error"] = "challenge-detected"
                        result["challenge_handle"] = handle
                        handle_result["status"] = "challenge-detected"
                        result["handle_results"].append(handle_result)
                        write_json("vet_candidate.json", result)
                        break

                    if unavailable(payload["text"]):
                        handle_result["status"] = "unavailable"
                        result["handle_results"].append(handle_result)
                        write_json("vet_candidate.json", result)
                        continue

                    # Collect profile summary
                    profile = await browser.get_profile_summary(handle)
                    profile["handle"] = handle
                    handle_result["profile"] = profile

                    # Collect timeline posts
                    scroll_budget = choose_scroll_budget(MAX_SCROLLS_MIN, MAX_SCROLLS_MAX)
                    handle_result["scroll_budget"] = scroll_budget

                    all_posts = await collect_timeline_posts(browser, handle, scroll_budget)
                    own_posts = filter_own_posts(all_posts, handle)

                    handle_result["posts"] = all_posts
                    handle_result["own_posts"] = own_posts
                    handle_result["posts_collected"] = len(all_posts)
                    handle_result["own_posts_collected"] = len(own_posts)
                    handle_result["status"] = "ok"
                    result["handle_results"].append(handle_result)
                    write_json("vet_candidate.json", result)
                    await browser.sleep(jitter(1600, 500))

                except Exception as error:
                    handle_result["status"] = "error"
                    handle_result["error"] = str(error)
                    result["handle_results"].append(handle_result)
                    result["handle_errors"].append(
                        {"handle": handle, "phase": "vetting", "error": str(error)}
                    )
                    write_json("vet_candidate.json", result)
                    continue

            ok_count = sum(1 for hr in result["handle_results"] if hr["status"] == "ok")
            if result.get("status") != "error":
                if ok_count > 0:
                    result["status"] = "partial" if result["handle_errors"] else "ok"
                else:
                    result["status"] = "error"
                    result["error"] = result.get("error") or "no-usable-results"
            result["evidence"] = {
                "screenshot_path": str(OUT_DIR / "vet_candidate.png"),
                "handles_visited": HANDLES,
            }
            try:
                await browser.screenshot(result["evidence"]["screenshot_path"], full_page=True)
            except Exception as error:
                result["handle_errors"].append(
                    {"handle": "_session", "phase": "screenshot", "error": str(error)}
                )
    except Exception as error:
        ok_count = sum(1 for hr in result["handle_results"] if hr["status"] == "ok")
        if ok_count > 0:
            result["status"] = "partial"
            result.setdefault("handle_errors", []).append(
                {"handle": "_session", "phase": "runner", "error": str(error)}
            )
        else:
            result["status"] = "error"
            result["error"] = str(error)

    write_json("vet_candidate.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
