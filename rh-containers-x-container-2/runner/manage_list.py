from __future__ import annotations

import asyncio
import json
import os
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mcp_browser import ChromeMCPBrowser, looks_challenged, looks_logged_in, looks_rate_limited, jitter


OUT_DIR = Path(os.environ.get("X_AUTOMATION_OUT_DIR", Path(__file__).resolve().parent.parent / "out"))
STATE_DIR = Path(os.environ.get("X_AUTOMATION_STATE_DIR", Path(__file__).resolve().parent.parent / "state"))
LIST_URL = os.environ.get("X_LIST_URL", "")
ADD = json.loads(os.environ.get("X_LIST_ADD_JSON", "[]"))
REMOVE = json.loads(os.environ.get("X_LIST_REMOVE_JSON", "[]"))
PRIVATE_RETRY_DAYS = int(os.environ.get("X_PRIVATE_RETRY_DAYS", "7"))
FORCE_ADD = os.environ.get("X_MANAGE_LIST_FORCE_ADD", "").lower() in ("1", "true", "yes")
DEBUG_DIALOG = os.environ.get("X_MANAGE_LIST_DEBUG_DIALOG", "").lower() in ("1", "true", "yes")
PER_HANDLE_TIMEOUT_SECONDS = int(os.environ.get("X_MANAGE_LIST_PER_HANDLE_TIMEOUT", "75"))
SESSION_TIMEOUT_SECONDS = int(os.environ.get("X_MANAGE_LIST_SESSION_TIMEOUT", "240"))
PROFILE_WAIT_TIMEOUT_MS = int(
    os.environ.get(
        "X_MANAGE_LIST_PROFILE_WAIT_MS",
        str(max(20000, min(PER_HANDLE_TIMEOUT_SECONDS * 1000 - 10000, 60000))),
    )
)
LIST_WAIT_TIMEOUT_MS = int(
    os.environ.get(
        "X_MANAGE_LIST_LIST_WAIT_MS",
        str(max(20000, min(PER_HANDLE_TIMEOUT_SECONDS * 1000 - 5000, 60000))),
    )
)
PRIVATE_STATE_FILE = STATE_DIR / "private_accounts.json"
LIST_CONFIG_DIR = (
    Path(__file__).resolve().parents[3]
    / "pipeline"
    / "research-pipeline"
    / "collectors"
    / "x"
)
LIST_CONFIG_PATHS = [
    LIST_CONFIG_DIR / "feed_lists_config.json",
    LIST_CONFIG_DIR / "feed_lists_breadth_config.json",
    LIST_CONFIG_DIR / "feed_lists_outlier_config.json",
]
OUT_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)


def write_json(name: str, payload: dict) -> None:
    (OUT_DIR / name).write_text(json.dumps(payload, indent=2))


def utc_now() -> datetime:
    return datetime.now(UTC)


def load_private_state() -> dict:
    if not PRIVATE_STATE_FILE.exists():
        return {"accounts": {}}
    try:
        data = json.loads(PRIVATE_STATE_FILE.read_text())
    except Exception:
        return {"accounts": {}}
    if not isinstance(data, dict):
        return {"accounts": {}}
    data.setdefault("accounts", {})
    return data


def save_private_state(state: dict) -> None:
    PRIVATE_STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def mark_private_account(handle: str, list_name: str, list_url: str) -> dict:
    state = load_private_state()
    now = utc_now()
    retry_at = now + timedelta(days=PRIVATE_RETRY_DAYS)
    accounts = state.setdefault("accounts", {})
    entry = accounts.get(handle, {})
    entry.update(
        {
            "handle": handle,
            "status": "private-account",
            "list_name": list_name,
            "list_url": list_url,
            "last_checked_at": now.isoformat(),
            "next_retry_after": retry_at.isoformat(),
        }
    )
    accounts[handle] = entry
    save_private_state(state)
    return entry


def clear_private_account(handle: str) -> None:
    state = load_private_state()
    accounts = state.setdefault("accounts", {})
    if handle in accounts:
        del accounts[handle]
        save_private_state(state)


def resolve_list_name(list_url: str) -> str | None:
    for config_path in LIST_CONFIG_PATHS:
        try:
            items = json.loads(config_path.read_text())
        except Exception:
            continue
        for item in items:
            if item.get("url") != list_url:
                continue
            return item.get("list_name") or item.get("title") or item.get("label")
    return None


async def add_handle(browser: ChromeMCPBrowser, handle: str, list_name: str) -> dict:
    item = {"handle": handle, "status": "unknown"}
    await browser.navigate(f"https://x.com/{handle}")
    await browser.wait_for_text(
        [f"@{handle}", "More", "Follow", "Following", "Posts", "Posts & replies"],
        timeout=PROFILE_WAIT_TIMEOUT_MS,
    )
    payload = await browser.get_page_payload(2500)
    if looks_rate_limited(payload["text"], payload.get("url", "")):
        item["status"] = "rate-limited"
        item["rate_limited"] = True
        return item
    if looks_challenged(payload["text"]):
        item["status"] = "challenge-detected"
        return item
    profile_summary = await browser.get_profile_summary(handle)
    item["profile"] = profile_summary
    if profile_summary.get("protected"):
        item["account_visibility"] = "protected"
    if not await browser.click_profile_more_menu():
        # Some large/verified profiles hydrate their action buttons slowly.
        await browser.wait_for_text(
            ["Posts", "Posts & replies", "Joined", "Followers", "Following"],
            timeout=PROFILE_WAIT_TIMEOUT_MS,
        )
        await browser.sleep(jitter(8000, 2000))
        if not await browser.click_profile_more_menu():
            if item.get("account_visibility") == "protected":
                item["status"] = "private-account"
                return item
            item["status"] = "no-more-button"
            return item
    await browser.sleep(jitter(1200, 400))
    if not await browser.click_menu_item_matching(r"Add/remove.*Lists|Lists"):
        await browser.wait_for_text(["Lists", "Block", "Mute", "Report"], timeout=12000)
        await browser.sleep(jitter(1000, 300))
        if not await browser.click_menu_item_matching(r"Add/remove.*Lists|Lists"):
            if item.get("account_visibility") == "protected":
                item["status"] = "private-account"
                return item
            item["status"] = "no-list-option"
            return item
    await browser.wait_for_text([list_name, "Pick a List"], timeout=LIST_WAIT_TIMEOUT_MS)
    await browser.sleep(jitter(1200, 400))

    if DEBUG_DIALOG:
        # Dump checkbox state and a11y snapshot for debugging
        checkboxes = await browser.evaluate("""() => {
            const rows = Array.from(document.querySelectorAll('[role="checkbox"], [aria-checked], [aria-selected]'));
            return rows.map((el, i) => {
                let node = el;
                let texts = [];
                for (let d = 0; node && d < 3; d++, node = node.parentElement) {
                    texts.push((node.innerText || '').trim().substring(0, 80));
                }
                return {
                    index: i,
                    ariaChecked: el.getAttribute('aria-checked'),
                    texts: texts,
                };
            });
        }""")
        snapshot = await browser.take_a11y_snapshot()
        snapshot_lines = [l for l in snapshot.split("\n") if any(k in l.lower() for k in ["check", "list", "save", "tier", "macro", "tactical", "option"])]
        item["debug_checkboxes"] = checkboxes if isinstance(checkboxes, list) else []
        item["debug_snapshot"] = snapshot_lines[:20]

    # Find checkbox in a11y snapshot — trusted clicks required for React onChange
    target_uid, is_checked = await browser.find_list_checkbox(list_name)
    if not target_uid:
        item["status"] = "list-not-found-in-dialog"
        await browser.close_dialog()
        return item

    if FORCE_ADD:
        item["force_toggled"] = True
        # Uncheck first if already checked (stale state), then recheck
        if is_checked:
            await browser.trusted_click(target_uid)
            await browser.sleep(jitter(1000, 300))
        await browser.trusted_click(target_uid)
    elif is_checked:
        item["status"] = "already-member"
    else:
        await browser.trusted_click(target_uid)

    # Save if we toggled something — wait for React to re-render and enable Save
    if item["status"] == "unknown":
        await browser.sleep(jitter(1500, 500))
        if not await browser.trusted_click_button(r"Save"):
            item["status"] = "save-not-found"
            await browser.close_dialog()
            return item
        await browser.sleep(jitter(1500, 500))
        post_save = await browser.get_page_payload(2500)
        if looks_rate_limited(post_save["text"], post_save.get("url", "")):
            item["status"] = "rate-limited"
            item["rate_limited"] = True
            return item
        item["status"] = "verified-added"
    await browser.close_dialog()
    await browser.sleep(jitter(700, 300))

    # Follow the user while we're on their profile — no extra navigation needed
    if item["status"] in ("verified-added", "already-member"):
        try:
            follow_status = await browser.click_follow_button()
            item["followed"] = follow_status  # "clicked-follow", "already-following", or "not-found"
            if follow_status == "clicked-follow":
                await browser.sleep(jitter(1000, 400))
        except Exception:
            item["followed"] = "error"

    return item


async def remove_handle(browser: ChromeMCPBrowser, handle: str, list_name: str) -> dict:
    item = {"handle": handle, "status": "unknown"}
    await browser.navigate(f"https://x.com/{handle}")
    await browser.wait_for_text(
        [f"@{handle}", "More", "Follow", "Following", "Posts", "Posts & replies"],
        timeout=PROFILE_WAIT_TIMEOUT_MS,
    )
    payload = await browser.get_page_payload(2500)
    if looks_challenged(payload["text"]):
        item["status"] = "challenge-detected"
        return item
    profile_summary = await browser.get_profile_summary(handle)
    item["profile"] = profile_summary
    if not await browser.click_profile_more_menu():
        await browser.wait_for_text(
            ["Posts", "Posts & replies", "Joined", "Followers", "Following"],
            timeout=PROFILE_WAIT_TIMEOUT_MS,
        )
        await browser.sleep(jitter(8000, 2000))
        if not await browser.click_profile_more_menu():
            item["status"] = "no-more-button"
            return item
    await browser.sleep(jitter(1200, 400))
    if not await browser.click_menu_item_matching(r"Add/remove.*Lists|Lists"):
        await browser.wait_for_text(["Lists", "Block", "Mute", "Report"], timeout=12000)
        await browser.sleep(jitter(1000, 300))
        if not await browser.click_menu_item_matching(r"Add/remove.*Lists|Lists"):
            item["status"] = "no-list-option"
            return item
    await browser.wait_for_text([list_name, "Pick a List"], timeout=LIST_WAIT_TIMEOUT_MS)
    await browser.sleep(jitter(1200, 400))

    # Find checkbox — trusted clicks required for React onChange
    target_uid, is_checked = await browser.find_list_checkbox(list_name)
    if not target_uid:
        item["status"] = "list-not-found-in-dialog"
        await browser.close_dialog()
        return item

    if not is_checked:
        item["status"] = "not-member"
    else:
        await browser.trusted_click(target_uid)
        await browser.sleep(jitter(500, 200))
        if not await browser.trusted_click_button(r"Save"):
            item["status"] = "save-not-found"
            await browser.close_dialog()
            return item
        await browser.sleep(jitter(1500, 500))
        post_save = await browser.get_page_payload(2500)
        if looks_rate_limited(post_save["text"], post_save.get("url", "")):
            item["status"] = "rate-limited"
            item["rate_limited"] = True
            return item
        item["status"] = "verified-removed"
    await browser.close_dialog()
    await browser.sleep(jitter(700, 300))
    return item


async def main() -> None:
    result = {
        "status": "unknown",
        "task_type": "manage_list",
        "list_url": LIST_URL,
        "added": [],
        "skipped": [],
        "removed": [],
        "failed": [],
    }
    browser_url = os.environ.get("BROWSER_URL") or os.environ.get("CDP_URL") or "http://127.0.0.1:9222"

    try:
        async with ChromeMCPBrowser(browser_url) as browser:
            await browser.navigate("https://x.com/home")
            await browser.wait_for_text(["For you", "Following", "Sign in to X"], timeout=20000)
            home = await browser.get_page_payload(3000)
            if looks_rate_limited(home["text"], home.get("url", "")):
                result["status"] = "error"
                result["error"] = "rate-limited"
                result["rate_limited"] = True
                write_json("manage_list.json", result)
                print(json.dumps(result))
                return
            if looks_challenged(home["text"]):
                result["status"] = "error"
                result["error"] = "challenge-detected"
                write_json("manage_list.json", result)
                print(json.dumps(result))
                return
            if not looks_logged_in(home["text"]):
                result["status"] = "error"
                result["error"] = "session-not-authenticated"
                write_json("manage_list.json", result)
                print(json.dumps(result))
                return

            if not LIST_URL:
                result["status"] = "error"
                result["error"] = "missing-list-url"
                write_json("manage_list.json", result)
                print(json.dumps(result))
                return

            await browser.navigate(LIST_URL)
            await browser.wait_for_text(["Edit List", "Waiting for posts", "Members"], timeout=20000)
            list_payload = await browser.get_page_payload(2500)
            if looks_rate_limited(list_payload["text"], list_payload.get("url", "")):
                result["status"] = "error"
                result["error"] = "rate-limited"
                result["rate_limited"] = True
                result["phase"] = "list-page"
                write_json("manage_list.json", result)
                print(json.dumps(result))
                return
            if looks_challenged(list_payload["text"]):
                result["status"] = "error"
                result["error"] = "challenge-detected"
                result["phase"] = "list-page"
                write_json("manage_list.json", result)
                print(json.dumps(result))
                return
            list_name = os.environ.get("X_LIST_NAME_OVERRIDE") or resolve_list_name(LIST_URL) or await browser.get_list_title()
            if not list_name:
                result["status"] = "error"
                result["error"] = "could-not-resolve-list-name"
                write_json("manage_list.json", result)
                print(json.dumps(result))
                return

            result["list_name"] = list_name
            result["list_url"] = LIST_URL

            for handle in ADD:
                try:
                    item = await asyncio.wait_for(add_handle(browser, handle, list_name), timeout=PER_HANDLE_TIMEOUT_SECONDS)
                except Exception as error:
                    item = {"handle": handle, "status": "error", "error": str(error)}
                    try:
                        await browser.screenshot(str(OUT_DIR / f"manage_list_error_{handle}.png"), full_page=True)
                    except Exception:
                        pass
                if item["status"] in ("verified-added", "already-member"):
                    clear_private_account(handle)
                    result["added"].append(item)
                elif item["status"] == "private-account":
                    item.update(mark_private_account(handle, list_name, LIST_URL))
                    result["skipped"].append(item)
                elif item["status"] == "rate-limited":
                    result["status"] = "error"
                    result["error"] = "rate-limited"
                    result["rate_limited"] = True
                    result["failed"].append(item)
                    break
                elif item["status"] == "challenge-detected":
                    result["status"] = "error"
                    result["error"] = "challenge-detected"
                    result["challenge_handle"] = handle
                    result["failed"].append(item)
                    break
                else:
                    result["failed"].append(item)
                try:
                    await browser.sleep(jitter(1500, 500))
                except RuntimeError:
                    # X can invalidate the page execution context mid-navigation after a failed add.
                    pass

            if result.get("status") != "error" and REMOVE:
                for handle in REMOVE:
                    try:
                        item = await asyncio.wait_for(remove_handle(browser, handle, list_name), timeout=PER_HANDLE_TIMEOUT_SECONDS)
                    except Exception as error:
                        item = {"handle": handle, "status": "error", "error": str(error)}
                        try:
                            await browser.screenshot(str(OUT_DIR / f"manage_list_remove_error_{handle}.png"), full_page=True)
                        except Exception:
                            pass
                    if item["status"] in ("verified-removed", "not-member"):
                        result["removed"].append(item)
                    elif item["status"] == "challenge-detected":
                        result["status"] = "error"
                        result["error"] = "challenge-detected"
                        result["challenge_handle"] = handle
                        result["failed"].append(item)
                        break
                    else:
                        result["failed"].append(item)
                    try:
                        await browser.sleep(jitter(1500, 500))
                    except RuntimeError:
                        pass

            result.setdefault("status", "ok")
            result["evidence"] = {"screenshot_path": str(OUT_DIR / "manage_list.png")}
            await browser.screenshot(result["evidence"]["screenshot_path"], full_page=True)
    except Exception as error:
        result["status"] = "error"
        result["error"] = str(error)
        result["traceback"] = traceback.format_exc()

    write_json("manage_list.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        asyncio.run(asyncio.wait_for(main(), timeout=SESSION_TIMEOUT_SECONDS))
    except TimeoutError:
        payload = {"status": "error", "task_type": "manage_list", "error": f"session-timeout-{SESSION_TIMEOUT_SECONDS}s"}
        write_json("manage_list.json", payload)
        print(json.dumps(payload))
