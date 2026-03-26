from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from mcp_browser import ChromeMCPBrowser, looks_logged_in, jitter


OUT_DIR = Path(os.environ.get("X_AUTOMATION_OUT_DIR", Path(__file__).resolve().parent.parent / "out"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

LIST_NAME = str(os.environ.get("X_LIST_NAME", "")).strip()
LIST_DESC = str(os.environ.get("X_LIST_DESC", "")).strip()
LIST_PRIVATE = str(os.environ.get("X_LIST_PRIVATE", "1")).strip() != "0"


def write_json(name: str, payload: dict) -> None:
    (OUT_DIR / name).write_text(json.dumps(payload, indent=2))


async def _fill_create_form(browser: ChromeMCPBrowser) -> dict:
    return await browser.evaluate(
        f"""() => {{
            const result = {{
                name_set: false,
                description_set: false,
                private_set: false,
            }};

            const setNativeValue = (el, value) => {{
                const proto = el.tagName === 'TEXTAREA'
                    ? window.HTMLTextAreaElement.prototype
                    : window.HTMLInputElement.prototype;
                const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
                if (descriptor && descriptor.set) {{
                    descriptor.set.call(el, value);
                }} else {{
                    el.value = value;
                }}
                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }};

            const findNameInput = () =>
                document.querySelector('input[name="name"]')
                || document.querySelector('input[placeholder*="Name"]')
                || Array.from(document.querySelectorAll('input')).find((el) => /name/i.test(el.getAttribute('aria-label') || ''));

            const findDescriptionInput = () =>
                document.querySelector('textarea[name="description"]')
                || document.querySelector('textarea[placeholder*="Description"]')
                || Array.from(document.querySelectorAll('textarea')).find((el) => /description/i.test(el.getAttribute('aria-label') || ''));

            const nameInput = findNameInput();
            if (nameInput) {{
                nameInput.focus();
                setNativeValue(nameInput, {json.dumps(LIST_NAME)});
                result.name_set = true;
            }}

            const descriptionInput = findDescriptionInput();
            if (descriptionInput) {{
                descriptionInput.focus();
                setNativeValue(descriptionInput, {json.dumps(LIST_DESC)});
                result.description_set = true;
            }}

            const privateToggle = document.querySelector('[role="checkbox"]') || document.querySelector('input[type="checkbox"]');
            if (privateToggle) {{
                const ariaChecked = privateToggle.getAttribute('aria-checked');
                const checked = ariaChecked === 'true' || privateToggle.checked === true;
                const shouldBeChecked = {json.dumps(LIST_PRIVATE)};
                if (checked !== shouldBeChecked) {{
                    privateToggle.click();
                }}
                result.private_set = true;
            }}

            return result;
        }}"""
    )


async def main() -> None:
    result = {
        "status": "unknown",
        "task_type": "create_list",
        "list_name": LIST_NAME,
        "list_description": LIST_DESC,
        "list_private": LIST_PRIVATE,
    }
    browser_url = os.environ.get("BROWSER_URL") or os.environ.get("CDP_URL") or "http://127.0.0.1:9222"

    if not LIST_NAME:
        result["status"] = "fail"
        result["reason"] = "missing-list-name"
        write_json("create_list.json", result)
        print(json.dumps(result))
        return

    try:
        async with ChromeMCPBrowser(browser_url) as browser:
            await browser.navigate("https://x.com/home")
            await browser.wait_for_text(["For you", "Following", "Sign in to X"], timeout=20000)
            payload = await browser.get_page_payload(1600)
            if not looks_logged_in(payload["text"]):
                result["status"] = "fail"
                result["reason"] = "session-not-authenticated"
                write_json("create_list.json", result)
                print(json.dumps(result))
                return

            await browser.navigate("https://x.com/i/lists/create")
            await browser.wait_for_text(["Create", "List", "Name"], timeout=25000)
            await browser.sleep(jitter(1500, 500))
            fill_result = await _fill_create_form(browser)
            result["form"] = fill_result

            await browser.sleep(jitter(800, 300))
            clicked_any = False
            if not await browser.click_button_matching(r"^Next$|^Create$"):
                result["status"] = "fail"
                result["reason"] = "create-button-not-found"
                await browser.screenshot(str(OUT_DIR / "create_list_error.png"), full_page=True)
                result["evidence"] = {"screenshot_path": str(OUT_DIR / "create_list_error.png")}
                write_json("create_list.json", result)
                print(json.dumps(result))
                return
            clicked_any = True

            # X's list composer can require a multi-step progression:
            # "Next" -> "Create" -> optional "Done".
            for pattern in (r"^Create$", r"^Done$", r"^Done$"):
                await browser.sleep(jitter(2200, 700))
                if await browser.click_button_matching(pattern):
                    clicked_any = True

            await browser.sleep(jitter(1800, 500))
            post_payload = await browser.get_page_payload(1600)
            current_url = post_payload.get("url") or ""
            result["clicked_submit_steps"] = clicked_any

            result["status"] = "ok"
            result["url"] = current_url
            result["title"] = post_payload.get("title")
            result["reason"] = (
                "list-created"
                if "/lists/" in current_url and not current_url.endswith("/lists/create")
                else "create-flow-complete"
            )
            result["evidence"] = {"screenshot_path": str(OUT_DIR / "create_list.png")}
            await browser.screenshot(result["evidence"]["screenshot_path"], full_page=True)
    except Exception as error:
        result["status"] = "error"
        result["error"] = str(error)

    write_json("create_list.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
