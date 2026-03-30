from __future__ import annotations

import asyncio
import json

from mcp_browser import ChromeMCPBrowser, looks_challenged, looks_logged_in, looks_rate_limited
from shared import OUT_DIR, write_json, resolve_browser_url


async def main() -> None:
    result = {"status": "unknown", "task_type": "verify_session"}
    browser_url = resolve_browser_url()

    try:
        async with ChromeMCPBrowser(browser_url) as browser:
            await browser.navigate("https://x.com/home")
            await browser.wait_for_text(["For you", "Following", "Sign in to X"], timeout=20000)
            payload = await browser.get_page_payload(1600)
            body = payload["text"]
            result["url"] = payload["url"]
            result["title"] = payload["title"]
            result["logged_in"] = looks_logged_in(body)
            result["excerpt"] = body
            result["account_handle"] = await browser.evaluate(
                """() => {
                    const extractHandle = (href) => {
                        if (!href) return null;
                        const match = href.match(/^\\/([A-Za-z0-9_]+)(?:\\/|$)/);
                        if (!match) return null;
                        const handle = match[1];
                        if (/^(home|explore|notifications|messages|i|settings|compose|search)$/i.test(handle)) return null;
                        return handle;
                    };
                    const selectors = [
                        'a[data-testid="AppTabBar_Profile_Link"]',
                        'a[aria-label*="Profile"]',
                        'nav a[href]',
                        '[data-testid="SideNav_AccountSwitcher_Button"] a[href]',
                        '[data-testid="SideNav_AccountSwitcher_Button"]'
                    ];
                    for (const selector of selectors) {
                        for (const node of Array.from(document.querySelectorAll(selector))) {
                            const href = node.getAttribute('href') || '';
                            const handle = extractHandle(href);
                            if (handle) return handle;
                            const text = [node.innerText || '', node.textContent || '', node.getAttribute('aria-label') || '']
                                .join(' ');
                            const textMatch = text.match(/@([A-Za-z0-9_]+)/);
                            if (textMatch) return textMatch[1];
                        }
                    }
                    return null;
                }"""
            )
            if looks_rate_limited(body, payload.get("url", "")):
                result["error"] = "rate-limited"
            elif looks_challenged(body):
                result["error"] = "challenge-required"
            elif not result["logged_in"]:
                result["error"] = "session-not-authenticated"
            result["status"] = "ok"
            result["evidence"] = {
                "screenshot_path": str(OUT_DIR / "verify_session.png"),
                "screenshot_taken": True,
            }
            await browser.screenshot(result["evidence"]["screenshot_path"], full_page=True)
    except Exception as error:
        result["status"] = "error"
        result["error"] = str(error)

    write_json("verify_session.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
