from __future__ import annotations

import asyncio
import json
import os

from mcp_browser import ChromeMCPBrowser, looks_challenged, looks_logged_in, jitter
from shared import write_json, resolve_browser_url


HANDLES = json.loads(os.environ.get("X_AVATAR_HANDLES_JSON", "[]"))


async def main() -> None:
    result = {"status": "unknown", "task_type": "pull_avatars", "profiles": []}
    browser_url = resolve_browser_url()

    try:
        async with ChromeMCPBrowser(browser_url) as browser:
            await browser.navigate("https://x.com/home")
            await browser.wait_for_text(["For you", "Following", "Sign in to X"], timeout=20000)
            home = await browser.get_page_payload(3000)
            if looks_challenged(home["text"]):
                result["status"] = "error"
                result["error"] = "challenge-detected"
                write_json("pull_avatars.json", result)
                print(json.dumps(result))
                return
            if not looks_logged_in(home["text"]):
                result["status"] = "error"
                result["error"] = "session-not-authenticated"
                write_json("pull_avatars.json", result)
                print(json.dumps(result))
                return

            for handle in HANDLES:
                try:
                    await browser.navigate(f"https://x.com/{handle}")
                    await browser.wait_for_text([f"@{handle}", "Posts", "This account doesn't exist"], timeout=15000)
                    await browser.sleep(jitter(1200, 400))
                    payload = await browser.get_page_payload(3000)
                    if looks_challenged(payload["text"]):
                        result["status"] = "error"
                        result["error"] = "challenge-detected"
                        result["challenge_handle"] = handle
                        write_json("pull_avatars.json", result)
                        print(json.dumps(result))
                        return
                    profile = await browser.get_profile_summary(handle)
                    result["profiles"].append({"status": "ok", **profile})
                except Exception as error:
                    result["profiles"].append({"handle": handle, "status": "error", "error": str(error)})

            result["status"] = "ok"
    except Exception as error:
        result["status"] = "error"
        result["error"] = str(error)

    write_json("pull_avatars.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
