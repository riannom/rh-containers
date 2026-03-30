from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import quote

from mcp_browser import ChromeMCPBrowser, jitter, looks_logged_in, scroll_and_collect
from shared import OUT_DIR, resolve_browser_url, write_json


QUERY = os.environ.get("X_QUERY", "").strip()
LIMIT = int(os.environ.get("X_LIMIT", "10"))


async def main() -> None:
    result = {"status": "unknown", "query": QUERY, "limit": LIMIT}
    browser_url = resolve_browser_url()

    try:
        async with ChromeMCPBrowser(browser_url) as browser:
            await browser.navigate("https://x.com/home")
            await browser.sleep(jitter(4000, 3000))
            home = await browser.get_page_payload(2000)
            if not looks_logged_in(home.get("text", "")):
                result["status"] = "fail"
                result["reason"] = "session-not-authenticated"
            elif not QUERY:
                result["status"] = "fail"
                result["reason"] = "missing-query"
            else:
                search_url = f"https://x.com/search?q={quote(QUERY)}&src=typed_query&f=live"
                await browser.navigate(search_url)
                await browser.sleep(jitter(6000, 5000))
                page = await browser.get_page_payload(1600)
                posts = await scroll_and_collect(
                    browser,
                    seen_tweet_ids=set(),
                    max_scrolls=8,
                    posts_per_scroll=max(LIMIT, 20),
                )
                result["status"] = "pass"
                result["reason"] = "search-collected"
                result["url"] = page.get("url")
                result["title"] = page.get("title")
                result["excerpt"] = page.get("text", "")[:1600]
                result["posts"] = posts[:LIMIT]
                screenshot_path = OUT_DIR / "search_research.png"
                await browser.screenshot(str(screenshot_path), full_page=True)
    except Exception as error:
        result["status"] = "error"
        result["reason"] = str(error)

    write_json("search_research.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
