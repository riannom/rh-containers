from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import struct
import time
from pathlib import Path

from mcp_browser import ChromeMCPBrowser, SET_NATIVE_VALUE_JS, looks_logged_in, looks_challenged
from shared import OUT_DIR, write_json, resolve_browser_url


CREDENTIALS_PATH = Path(
    os.environ.get("X_CREDENTIALS_PATH", Path(__file__).resolve().parent.parent / "state" / "x_credentials.json")
)

RESULT_FILE = "login_session.json"


def generate_totp(secret_b32: str, period: int = 30) -> str:
    """Generate a 6-digit TOTP code from a base32 secret (stdlib only)."""
    key = base64.b32decode(secret_b32.upper())
    counter = struct.pack(">Q", int(time.time()) // period)
    mac = hmac.new(key, counter, hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{code % 1000000:06d}"


def load_credentials(account_handle: str) -> dict | None:
    if not CREDENTIALS_PATH.exists():
        return None
    with open(CREDENTIALS_PATH) as f:
        creds = json.load(f)
    return creds.get(account_handle)


def _finish(result: dict) -> None:
    """Write result to disk and stdout, then return for caller to exit."""
    write_json(RESULT_FILE, result)
    print(json.dumps(result))


def _set_error(result: dict, error: str) -> None:
    result["status"] = "error"
    result["error"] = error


def _detect_page_state(text: str) -> str:
    """Classify the current X page state from body text."""
    lowered = text.lower()

    if looks_logged_in(text):
        return "logged_in"

    if looks_challenged(text):
        return "challenged"
    if "wrong password" in lowered or "incorrect" in lowered:
        return "wrong_password"
    if "this account is suspended" in lowered or "account is locked" in lowered:
        return "account_locked"
    if "unusual login activity" in lowered:
        return "unusual_activity"

    if "phone, email, or username" in lowered or "sign in to x" in lowered:
        return "username_step"
    if "enter your phone number or email" in lowered or "confirm your email" in lowered or "verify your identity" in lowered:
        return "identity_challenge"
    if "enter your password" in lowered:
        return "password_step"
    if "enter the code" in lowered or "authentication code" in lowered or "two-factor" in lowered or "authenticator app" in lowered:
        return "totp_step"

    return "unknown"


async def _fill_and_submit(browser: ChromeMCPBrowser, selector_js: str, value: str, submit_js: str) -> dict:
    """Fill an input field and click a submit button."""
    return await browser.evaluate(
        f"""{SET_NATIVE_VALUE_JS}
        (() => {{
            const input = {selector_js};
            if (!input) return {{ filled: false, error: 'input-not-found' }};
            input.focus();
            setNativeValue(input, {json.dumps(value)});
            const button = {submit_js};
            if (button) button.click();
            return {{ filled: true, submitted: !!button }};
        }})()"""
    )


async def _screenshot(browser: ChromeMCPBrowser, result: dict) -> None:
    try:
        path = str(OUT_DIR / "login_session.png")
        await browser.screenshot(path, full_page=True)
        result["evidence"] = {"screenshot_path": path, "screenshot_taken": True}
    except Exception:
        pass


# JS selectors for the "Next" button used in multiple steps
NEXT_BUTTON_JS = """(
    Array.from(document.querySelectorAll('button,[role="button"]'))
        .find((el) => /^Next$/i.test((el.innerText || el.textContent || '').trim()))
)"""


async def main() -> None:
    result = {
        "status": "unknown",
        "task_type": "login_session",
        "logged_in": False,
        "account_handle": None,
        "step_reached": "init",
        "error": None,
    }
    browser_url = resolve_browser_url()
    account_handle = os.environ.get("X_ACCOUNT_HANDLE") or os.environ.get("BROWSER_POOL_ACCOUNT")

    if not account_handle:
        _set_error(result, "no-account-handle")
        _finish(result)
        return

    creds = load_credentials(account_handle)
    if not creds:
        _set_error(result, "no-credentials")
        _finish(result)
        return

    username = creds.get("username") or account_handle
    password = creds.get("password")
    email = creds.get("email")
    totp_secret = creds.get("totp_secret")

    if not password:
        _set_error(result, "no-password")
        _finish(result)
        return

    try:
        async with ChromeMCPBrowser(browser_url) as browser:
            await browser.navigate("https://x.com/i/flow/login")
            await asyncio.sleep(3)
            await browser.wait_for_text(
                ["Phone, email, or username", "Sign in to X", "For you", "Following"],
                timeout=20000,
            )

            payload = await browser.get_page_payload(2000)
            state = _detect_page_state(payload["text"])

            # Already logged in
            if state == "logged_in":
                result["status"] = "ok"
                result["logged_in"] = True
                result["step_reached"] = "already_logged_in"
                result["account_handle"] = account_handle
                _finish(result)
                return

            # Enter username
            result["step_reached"] = "username"
            username_selectors = """(
                document.querySelector('input[autocomplete="username"]')
                || document.querySelector('input[name="text"]')
                || document.querySelector('input[type="text"]')
            )"""
            fill_result = await _fill_and_submit(browser, username_selectors, username, NEXT_BUTTON_JS)
            if not fill_result.get("filled"):
                _set_error(result, "username-input-not-found")
                await _screenshot(browser, result)
                _finish(result)
                return

            await asyncio.sleep(3)
            payload = await browser.get_page_payload(2000)
            state = _detect_page_state(payload["text"])

            # Identity challenge (email verification)
            if state == "identity_challenge":
                result["step_reached"] = "identity_challenge"
                if not email:
                    _set_error(result, "identity-challenge-no-email")
                    await _screenshot(browser, result)
                    _finish(result)
                    return

                challenge_selectors = """(
                    document.querySelector('input[data-testid="ocfEnterTextTextInput"]')
                    || document.querySelector('input[name="text"]')
                    || document.querySelector('input[type="text"]')
                )"""
                fill_result = await _fill_and_submit(browser, challenge_selectors, email, NEXT_BUTTON_JS)
                if not fill_result.get("filled"):
                    _set_error(result, "identity-challenge-input-not-found")
                    await _screenshot(browser, result)
                    _finish(result)
                    return

                await asyncio.sleep(3)
                payload = await browser.get_page_payload(2000)
                state = _detect_page_state(payload["text"])

            # Abort on error states
            if state in ("wrong_password", "account_locked", "challenged", "unusual_activity"):
                _set_error(result, state)
                await _screenshot(browser, result)
                _finish(result)
                return

            # Enter password
            if state in ("password_step", "unknown"):
                result["step_reached"] = "password"
                password_selectors = """(
                    document.querySelector('input[name="password"]')
                    || document.querySelector('input[type="password"]')
                )"""
                login_button = """(
                    Array.from(document.querySelectorAll('button,[role="button"]'))
                        .find((el) => /^Log in$/i.test((el.innerText || el.textContent || '').trim()))
                )"""
                fill_result = await _fill_and_submit(browser, password_selectors, password, login_button)
                if not fill_result.get("filled"):
                    _set_error(result, "password-input-not-found")
                    await _screenshot(browser, result)
                    _finish(result)
                    return

                await asyncio.sleep(5)
                payload = await browser.get_page_payload(2000)
                state = _detect_page_state(payload["text"])

            # Abort on error states after password
            if state in ("wrong_password", "account_locked", "challenged", "unusual_activity"):
                _set_error(result, state)
                await _screenshot(browser, result)
                _finish(result)
                return

            # TOTP if needed
            if state == "totp_step":
                result["step_reached"] = "totp"
                if not totp_secret:
                    _set_error(result, "totp-required-no-secret")
                    await _screenshot(browser, result)
                    _finish(result)
                    return

                code = generate_totp(totp_secret)
                totp_selectors = """(
                    document.querySelector('input[name="text"]')
                    || document.querySelector('input[type="text"]')
                    || document.querySelector('input[autocomplete="one-time-code"]')
                )"""
                totp_next = """(
                    Array.from(document.querySelectorAll('button,[role="button"]'))
                        .find((el) => /^Next$|^Verify$|^Confirm$/i.test((el.innerText || el.textContent || '').trim()))
                )"""
                fill_result = await _fill_and_submit(browser, totp_selectors, code, totp_next)
                if not fill_result.get("filled"):
                    _set_error(result, "totp-input-not-found")
                    await _screenshot(browser, result)
                    _finish(result)
                    return

                await asyncio.sleep(5)
                payload = await browser.get_page_payload(2000)
                state = _detect_page_state(payload["text"])

            # Final verification
            result["step_reached"] = "verification"
            if state != "logged_in":
                try:
                    await browser.wait_for_text(["For you", "Following", "Home"], timeout=15000)
                    payload = await browser.get_page_payload(2000)
                    state = _detect_page_state(payload["text"])
                except Exception:
                    pass

            if state == "logged_in":
                result["status"] = "ok"
                result["logged_in"] = True
                result["step_reached"] = "complete"
                result["account_handle"] = account_handle
            else:
                _set_error(result, f"login-incomplete:{state}")

            await _screenshot(browser, result)

    except Exception as error:
        _set_error(result, str(error))

    _finish(result)


if __name__ == "__main__":
    asyncio.run(main())
