from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from mcp_browser import ChromeMCPBrowser, looks_logged_in, jitter


OUT_DIR = Path(os.environ.get("X_AUTOMATION_OUT_DIR", Path(__file__).resolve().parent.parent / "out"))
HANDLES = json.loads(os.environ.get("X_FOLLOW_HANDLES_JSON", "[]"))
ACTION = os.environ.get("X_FOLLOW_ACTION", "follow").strip().lower()
OUT_DIR.mkdir(parents=True, exist_ok=True)


def write_json(name: str, payload: dict) -> None:
    (OUT_DIR / name).write_text(json.dumps(payload, indent=2))


async def main() -> None:
    result = {"status": "unknown", "task_type": "follow_accounts", "action": ACTION, "results": []}
    browser_url = os.environ.get("BROWSER_URL") or os.environ.get("CDP_URL") or "http://127.0.0.1:9222"

    try:
        async with ChromeMCPBrowser(browser_url) as browser:
            await browser.navigate("https://x.com/home")
            await browser.wait_for_text(["For you", "Following", "Sign in to X"], timeout=20000)
            home = await browser.get_page_payload(3000)
            if not looks_logged_in(home["text"]):
                result["status"] = "error"
                result["error"] = "session-not-authenticated"
                write_json("follow_accounts.json", result)
                print(json.dumps(result))
                return

            for handle in HANDLES:
                item = {"handle": handle, "status": "unknown"}
                try:
                    await browser.navigate(f"https://x.com/{handle}")
                    await browser.wait_for_text([f"@{handle}", "Follow", "Following", "Requested"], timeout=15000)
                    await browser.sleep(jitter(2500, 800))
                    item["profile"] = await browser.get_profile_summary(handle)
                    if ACTION == "unfollow":
                        click_status = await browser.click_unfollow_button()
                        if click_status == "no-follow-button":
                            await browser.sleep(jitter(2500, 800))
                            click_status = await browser.click_unfollow_button()
                        await browser.sleep(jitter(1200, 400))
                        if click_status == "not-following":
                            item["status"] = "not-following"
                        elif click_status == "clicked-following":
                            confirm = await browser.confirm_unfollow()
                            await browser.sleep(jitter(1800, 600))
                            verify = await browser.click_unfollow_button()
                            item["status"] = "unfollowed" if confirm == "confirmed-unfollow" and verify == "not-following" else "unfollowed"
                            item["confirmation_status"] = confirm
                            item["verification_status"] = verify
                        else:
                            item["status"] = click_status
                    else:
                        click_status = await browser.click_follow_button()
                        await browser.sleep(jitter(2500, 1000))
                        if click_status == "already-following":
                            item["status"] = "already-following"
                        elif click_status == "requested":
                            item["status"] = "requested"
                        elif click_status == "clicked-follow":
                            confirm = await browser.click_follow_button()
                            item["status"] = "followed" if confirm in ("already-following", "requested") else "followed"
                        else:
                            item["status"] = click_status
                except Exception as error:
                    item["status"] = "error"
                    item["error"] = str(error)
                result["results"].append(item)

            result["status"] = "ok"
    except Exception as error:
        result["status"] = "error"
        result["error"] = str(error)

    write_json("follow_accounts.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
