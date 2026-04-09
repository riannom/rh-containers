from __future__ import annotations

import asyncio
import json
import os
import random
import re

from mcp_browser import ChromeMCPBrowser, looks_challenged, looks_logged_in, jitter
from shared import OUT_DIR, write_json, resolve_browser_url


HANDLES = json.loads(os.environ.get("X_REL_HANDLES_JSON", "[]"))
MAX_PROFILE_SCROLLS = int(os.environ.get("X_REL_PROFILE_SCROLLS", "2"))
MAX_EDGE_SCROLLS = int(os.environ.get("X_REL_EDGE_SCROLLS", "5"))
MAX_PROFILE_SCROLLS_MIN = int(
    os.environ.get("X_REL_PROFILE_SCROLLS_MIN", str(MAX_PROFILE_SCROLLS))
)
MAX_PROFILE_SCROLLS_MAX = int(
    os.environ.get("X_REL_PROFILE_SCROLLS_MAX", str(MAX_PROFILE_SCROLLS))
)
MAX_EDGE_SCROLLS_MIN = int(
    os.environ.get("X_REL_EDGE_SCROLLS_MIN", str(MAX_EDGE_SCROLLS))
)
MAX_EDGE_SCROLLS_MAX = int(
    os.environ.get("X_REL_EDGE_SCROLLS_MAX", str(MAX_EDGE_SCROLLS))
)
EDGE_LIMIT = int(os.environ.get("X_REL_EDGE_LIMIT", "100"))
DIRECTION = os.environ.get("X_REL_DIRECTION", "both")


def unavailable(body: str) -> bool:
    return bool(
        re.search(
            r"This account doesn't exist|Account suspended|These posts are protected",
            body,
            re.I,
        )
    )


def choose_scroll_budget(low: int, high: int) -> int:
    return random.randint(min(low, high), max(low, high))


async def scrape_connections(
    browser: ChromeMCPBrowser, url: str | None, edge_type: str, scroll_budget: int
) -> list[dict]:
    if not url:
        return []

    await browser.navigate(url)
    await browser.sleep(jitter(2500, 800))
    seen: set[str] = set()
    edges: list[dict] = []

    for _ in range(scroll_budget):
        batch = await browser.collect_connections_from_page(edge_type, limit=50)
        new_count = 0
        for item in batch:
            key = f"{edge_type}:{item['handle'].lower()}"
            if key in seen:
                continue
            seen.add(key)
            edges.append(item)
            new_count += 1
            if len(edges) >= EDGE_LIMIT:
                return edges
        if new_count == 0:
            break
        await browser.scroll_page()

    return edges


async def main() -> None:
    result = {
        "status": "unknown",
        "task_type": "collect_relationships",
        "profiles": [],
        "edges": [],
        "collected_at": None,
        "handle_results": [],
        "handle_errors": [],
    }
    browser_url = resolve_browser_url()

    try:
        async with ChromeMCPBrowser(browser_url) as browser:
            await browser.navigate("https://x.com/home")
            await browser.wait_for_text(
                ["For you", "Following", "Sign in to X"], timeout=20000
            )
            home = await browser.get_page_payload(3000)
            if looks_challenged(home["text"]):
                result["status"] = "error"
                result["error"] = "challenge-detected"
                write_json("collect_relationships.json", result)
                print(json.dumps(result))
                return
            if not looks_logged_in(home["text"]):
                result["status"] = "error"
                result["error"] = "session-not-authenticated"
                write_json("collect_relationships.json", result)
                print(json.dumps(result))
                return

            for handle in HANDLES:
                handle_result = {
                    "handle": handle,
                    "status": "unknown",
                    "profile_ok": False,
                    "following_edges": 0,
                    "follower_edges": 0,
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
                        write_json("collect_relationships.json", result)
                        break
                    if unavailable(payload["text"]):
                        result["profiles"].append(
                            {"handle": handle, "status": "unavailable"}
                        )
                        handle_result["status"] = "unavailable"
                        result["handle_results"].append(handle_result)
                        write_json("collect_relationships.json", result)
                        continue

                    following_scroll_budget = choose_scroll_budget(
                        MAX_EDGE_SCROLLS_MIN, MAX_EDGE_SCROLLS_MAX
                    )
                    follower_scroll_budget = choose_scroll_budget(
                        MAX_EDGE_SCROLLS_MIN, MAX_EDGE_SCROLLS_MAX
                    )
                    handle_result["following_scrolls_used"] = following_scroll_budget
                    handle_result["follower_scrolls_used"] = follower_scroll_budget

                    profile = await browser.get_profile_summary(handle)
                    profile["handle"] = handle
                    if not profile.get("handle_match"):
                        handle_result["status"] = "profile-handle-mismatch"
                        handle_result["resolved_handle"] = profile.get(
                            "resolved_handle"
                        )
                        result["handle_results"].append(handle_result)
                        result["handle_errors"].append(
                            {
                                "handle": handle,
                                "phase": "profile",
                                "error": f"resolved-handle-mismatch:{profile.get('resolved_handle')}",
                            }
                        )
                        write_json("collect_relationships.json", result)
                        continue
                    profile["status"] = "ok"
                    result["profiles"].append(profile)
                    handle_result["profile_ok"] = True

                    try:
                        following = (
                            []
                            if DIRECTION == "followers"
                            else await scrape_connections(
                                browser,
                                profile.get("following_url"),
                                "following",
                                following_scroll_budget,
                            )
                        )
                    except Exception as error:
                        following = []
                        result["handle_errors"].append(
                            {
                                "handle": handle,
                                "phase": "following",
                                "error": str(error),
                            }
                        )

                    try:
                        followers = (
                            []
                            if DIRECTION == "following"
                            else await scrape_connections(
                                browser,
                                profile.get("followers_url"),
                                "followers",
                                follower_scroll_budget,
                            )
                        )
                    except Exception as error:
                        followers = []
                        result["handle_errors"].append(
                            {
                                "handle": handle,
                                "phase": "followers",
                                "error": str(error),
                            }
                        )

                    for edge in [*following, *followers]:
                        result["edges"].append({"source_handle": handle, **edge})
                    handle_result["following_edges"] = len(following)
                    handle_result["follower_edges"] = len(followers)
                    handle_result["status"] = "ok"
                    result["handle_results"].append(handle_result)
                    write_json("collect_relationships.json", result)
                    await browser.sleep(jitter(800, 300))
                except Exception as error:
                    handle_result["status"] = "error"
                    handle_result["error"] = str(error)
                    result["handle_results"].append(handle_result)
                    result["handle_errors"].append(
                        {"handle": handle, "phase": "handle", "error": str(error)}
                    )
                    write_json("collect_relationships.json", result)
                    continue

            result["profile_count"] = sum(
                1 for p in result["profiles"] if p.get("status") == "ok"
            )
            result["edge_count"] = len(result["edges"])
            if result.get("status") != "error":
                if result["profile_count"] > 0 or result["edge_count"] > 0:
                    result["status"] = "partial" if result["handle_errors"] else "ok"
                else:
                    result["status"] = "error"
                    result["error"] = result.get("error") or "no-usable-results"
            result["evidence"] = {
                "screenshot_path": str(OUT_DIR / "collect_relationships.png"),
                "handles_visited": HANDLES,
            }
            try:
                await browser.screenshot(
                    result["evidence"]["screenshot_path"], full_page=True
                )
            except Exception as error:
                result["handle_errors"].append(
                    {"handle": "_session", "phase": "screenshot", "error": str(error)}
                )
    except Exception as error:
        if result["profiles"] or result["edges"]:
            result["status"] = "partial"
            result.setdefault("handle_errors", []).append(
                {"handle": "_session", "phase": "runner", "error": str(error)}
            )
        else:
            result["status"] = "error"
            result["error"] = str(error)

    write_json("collect_relationships.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
