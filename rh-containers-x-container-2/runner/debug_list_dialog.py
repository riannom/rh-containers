"""Debug: open the Add to List dialog for a handle and dump checkbox state + a11y snapshot."""
from __future__ import annotations
import asyncio, json, os
from pathlib import Path
from mcp_browser import ChromeMCPBrowser, looks_logged_in, jitter

OUT_DIR = Path(os.environ.get("X_AUTOMATION_OUT_DIR", Path(__file__).resolve().parent.parent / "out"))
HANDLE = os.environ.get("X_DEBUG_HANDLE", "bbands")
OUT_DIR.mkdir(parents=True, exist_ok=True)

async def main():
    browser_url = os.environ.get("BROWSER_URL") or "http://127.0.0.1:9222"
    result = {"handle": HANDLE, "checkboxes": [], "snapshot_excerpt": ""}

    async with ChromeMCPBrowser(browser_url) as browser:
        await browser.navigate("https://x.com/home")
        await browser.wait_for_text(["For you", "Following"], timeout=20000)

        # Navigate to profile
        await browser.navigate(f"https://x.com/{HANDLE}")
        await browser.wait_for_text([f"@{HANDLE}", "Posts", "Following"], timeout=20000)
        await browser.sleep(jitter(2000, 500))

        # Open More menu
        await browser.click_profile_more_menu()
        await browser.sleep(jitter(1500, 400))

        # Click Add/remove Lists
        await browser.click_menu_item_matching(r"Add/remove.*Lists|Lists")
        await browser.sleep(jitter(2000, 500))

        # Dump all checkboxes via evaluate
        checkboxes = await browser.evaluate("""() => {
            const rows = Array.from(document.querySelectorAll('[role="checkbox"], [aria-checked], [aria-selected]'));
            return rows.map((el, i) => {
                let texts = [];
                let node = el;
                for (let d = 0; node && d < 5; d++, node = node.parentElement) {
                    texts.push((node.innerText || '').trim().substring(0, 100));
                }
                return {
                    index: i,
                    tagName: el.tagName,
                    role: el.getAttribute('role'),
                    ariaChecked: el.getAttribute('aria-checked'),
                    ariaSelected: el.getAttribute('aria-selected'),
                    innerText: (el.innerText || '').trim().substring(0, 50),
                    parentTexts: texts,
                };
            });
        }""")
        result["checkboxes"] = checkboxes if isinstance(checkboxes, list) else []

        # Take a11y snapshot
        snapshot = await browser.take_a11y_snapshot()
        # Find the relevant section
        lines = snapshot.split("\n")
        relevant = [l for l in lines if any(k in l.lower() for k in ["check", "list", "save", "tier", "macro", "tactical"])]
        result["snapshot_relevant"] = relevant[:30]
        result["snapshot_full_length"] = len(lines)

        await browser.screenshot(str(OUT_DIR / "debug_list_dialog.png"), full_page=True)
        await browser.close_dialog()

    (OUT_DIR / "debug_list_dialog.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=60))
