from __future__ import annotations

import asyncio
import json
import os
import random
import re
import shutil
from glob import glob
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _default_mcp_bin() -> str:
    override = os.environ.get("CHROME_MCP_BIN")
    if override:
        return override

    candidates = sorted(
        glob(
            str(
                Path.home()
                / ".npm"
                / "_npx"
                / "*"
                / "node_modules"
                / "chrome-devtools-mcp"
                / "build"
                / "src"
                / "bin"
                / "chrome-devtools-mcp.js"
            )
        )
    )
    if not candidates:
        raise RuntimeError("Could not locate cached chrome-devtools-mcp binary")
    return candidates[-1]


def _default_node_bin() -> str:
    override = os.environ.get("NODE_BIN")
    if override:
        return override

    discovered = shutil.which("node")
    if discovered:
        return discovered

    for candidate in ("/opt/homebrew/bin/node", "/usr/local/bin/node"):
        if Path(candidate).exists():
            return candidate

    raise RuntimeError("Could not locate node binary")


def _result_text(result: Any) -> str:
    return "\n".join(
        item.text for item in getattr(result, "content", []) if getattr(item, "type", None) == "text"
    )


def _extract_json_payload(text: str) -> Any:
    fenced = re.search(r"```json\n(.*?)\n```", text, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))

    inline = re.search(r"(\{.*\}|\[.*\])\s*$", text, re.DOTALL)
    if inline:
        return json.loads(inline.group(1))

    raise ValueError(f"Could not extract JSON payload from MCP response: {text[:400]}")


def looks_logged_in(body: str) -> bool:
    return bool(
        re.search(r"For you|Following|What is happening|What’s happening|Post|Home", body)
        and not re.search(r"Sign in to X|Phone, email, or username", body)
    )


def looks_signed_out(body: str) -> bool:
    return bool(
        re.search(
            r"Sign in to X|Phone, email, or username|Don't have an account\?|Create account|Sign up",
            body,
            re.I,
        )
    )


def looks_challenged(body: str) -> bool:
    # Note: "Something went wrong. Try reloading" is a generic X error, not a
    # challenge/CAPTCHA. Including it caused false positives on transient page
    # load failures (e.g. cberry1). Only match actual anti-bot challenges.
    return bool(
        re.search(
            r"verify you are human|unusual activity|rate limit exceeded|access denied|temporarily limited|captcha|complete the challenge",
            body,
            re.I,
        )
    )


def looks_rate_limited(body: str, url: str = "") -> bool:
    """Detect X rate limiting — explicit messages or the black/empty page pattern."""
    # Explicit rate limit text
    if re.search(r"rate limit|too many requests|temporarily limited", body, re.I):
        return True
    # Black/empty page: on an x.com URL but body has almost no content
    # (normal X pages have at least navigation text, timeline content, etc.)
    if "x.com" in url and len(body.strip()) < 20:
        return True
    return False


SET_NATIVE_VALUE_JS = """
function setNativeValue(el, value) {
    const proto = el.tagName === 'TEXTAREA'
        ? window.HTMLTextAreaElement.prototype
        : window.HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
    if (descriptor && descriptor.set) {
        descriptor.set.call(el, value);
    } else {
        el.value = value;
    }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
}
"""


def jitter(base: int, variance: int) -> int:
    return int(base + random.random() * max(variance, 0))


def _is_shutdown_cancel_scope_error(error: BaseException) -> bool:
    message = str(error).lower()
    if (
        "cancel scope" in message
        or "different task" in message
        or "unhandled errors in a taskgroup" in message
        or "asyncio.run() shutdown" in message
    ):
        return True
    for child in getattr(error, "exceptions", ()) or ():
        if _is_shutdown_cancel_scope_error(child):
            return True
    cause = getattr(error, "__cause__", None)
    if cause is not None and _is_shutdown_cancel_scope_error(cause):
        return True
    context = getattr(error, "__context__", None)
    if context is not None and _is_shutdown_cancel_scope_error(context):
        return True
    return False


def _is_transient_connect_error(message: str) -> bool:
    lowered = message.lower()
    return (
        "could not connect to chrome" in lowered
        or "failed to fetch browser websocket url" in lowered
        or "fetch failed" in lowered
    )


def _is_execution_context_error(error: BaseException | str) -> bool:
    message = str(error).lower()
    return "execution context was destroyed" in message or "cannot find context with specified id" in message


class ChromeMCPBrowser:
    def __init__(self, browser_url: str):
        self.browser_url = browser_url
        self._stdio_ctx = None
        self._session_ctx = None
        self.session: ClientSession | None = None

    async def __aenter__(self) -> "ChromeMCPBrowser":
        ws_endpoint = os.environ.get("BROWSER_WS_ENDPOINT") or self.browser_url
        server = StdioServerParameters(
            command=_default_node_bin(),
            args=[
                _default_mcp_bin(),
                (
                    f"--wsEndpoint={ws_endpoint}"
                    if ws_endpoint.startswith("ws://") or ws_endpoint.startswith("wss://")
                    else f"--browserUrl={ws_endpoint}"
                ),
                "--no-usage-statistics",
            ],
            env={},
        )
        self._stdio_ctx = stdio_client(server)
        read, write = await self._stdio_ctx.__aenter__()
        self._session_ctx = ClientSession(read, write)
        self.session = await self._session_ctx.__aenter__()
        await self.session.initialize()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        session_error = None
        if self._session_ctx is not None:
            try:
                await self._session_ctx.__aexit__(exc_type, exc, tb)
            except BaseException as error:
                session_error = error

        if self._stdio_ctx is not None:
            try:
                await self._stdio_ctx.__aexit__(exc_type, exc, tb)
            except BaseException as error:
                if not _is_shutdown_cancel_scope_error(error):
                    raise

        if session_error is not None:
            if not _is_shutdown_cancel_scope_error(session_error) and not _is_execution_context_error(session_error):
                raise session_error

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if self.session is None:
            raise RuntimeError("MCP session is not initialized")
        last_error = None
        for attempt in range(8):
            result = await self.session.call_tool(name, arguments or {})
            if not getattr(result, "isError", False):
                return result
            message = _result_text(result)
            last_error = RuntimeError(message)
            if attempt == 7 or not _is_transient_connect_error(message):
                raise last_error
            await asyncio.sleep(2.0 + attempt)
        raise last_error or RuntimeError("Unknown MCP call failure")

    async def ensure_page(self) -> None:
        pages = await self.call("list_pages", {})
        if "## Pages" not in _result_text(pages):
            await self.call("new_page", {"url": "about:blank", "isolatedContext": "x-automation"})

    async def navigate(self, url: str, timeout: int = 60000) -> None:
        await self.ensure_page()
        await self.call("navigate_page", {"type": "url", "url": url, "timeout": timeout})

    async def wait_for_text(self, texts: list[str], timeout: int = 15000) -> str:
        result = await self.call("wait_for", {"text": texts, "timeout": timeout})
        return _result_text(result)

    async def sleep(self, delay_ms: int) -> None:
        try:
            await self.evaluate(
                f"async () => {{ await new Promise(resolve => setTimeout(resolve, {delay_ms})); return true; }}"
            )
        except RuntimeError as error:
            if not _is_execution_context_error(error):
                raise

    async def evaluate(self, function: str) -> Any:
        result = await self.call("evaluate_script", {"function": function})
        return _extract_json_payload(_result_text(result))

    async def screenshot(self, file_path: str, full_page: bool = True) -> None:
        await self.call(
            "take_screenshot",
            {"filePath": file_path, "fullPage": full_page, "format": "png"},
        )

    async def get_page_payload(self, text_limit: int = 1600) -> dict[str, Any]:
        return await self.evaluate(
            f"""() => ({{
                url: location.href,
                title: document.title,
                text: (document.body && document.body.innerText || '').slice(0, {text_limit})
            }})"""
        )

    async def click_following_tab(self) -> bool:
        return bool(
            await self.evaluate(
                """() => {
                    const candidates = Array.from(document.querySelectorAll('[role="tab"], button, a, div'));
                    const target = candidates.find((el) => (el.innerText || '').trim() === 'Following');
                    if (!target) return false;
                    target.click();
                    return true;
                }"""
            )
        )

    async def click_profile_more_menu(self) -> bool:
        return bool(
            await self.evaluate(
                """() => {
                    window.scrollTo({ top: 0, behavior: 'instant' });
                    const direct = document.querySelector('main [data-testid="userActions"] button, main [data-testid="userActions"] [role="button"]');
                    if (direct) { direct.click(); return true; }
                    const container = document.querySelector('main [data-testid="userActions"]');
                    if (container) { container.click(); return true; }
                    const buttons = Array.from(document.querySelectorAll('main button, main [role="button"]'));
                    const target = buttons.find((el) => {
                        const label = [
                            el.getAttribute('aria-label') || '',
                            el.innerText || '',
                            el.textContent || ''
                        ].join(' ').trim();
                        if (el.closest('article')) return false;
                        if (el.closest('[data-testid="userActions"]')) return true;
                        if (el.getAttribute('aria-haspopup') !== 'menu') return false;
                        if (/Share|Repost|Grok|Analytics/i.test(label)) return false;
                        return label === '' || label === 'More' || /^More /.test(label);
                    });
                    if (!target) return false;
                    target.click();
                    return true;
                }"""
            )
        )

    async def click_menu_item_matching(self, pattern: str) -> bool:
        return bool(
            await self.evaluate(
                f"""() => {{
                    const re = new RegExp({json.dumps(pattern)}, 'i');
                    const items = Array.from(document.querySelectorAll('[role="menuitem"], [role="option"], button'));
                    const target = items.find((el) => re.test((el.innerText || el.textContent || '').trim()));
                    if (!target) return false;
                    target.click();
                    return true;
                }}"""
            )
        )

    async def take_a11y_snapshot(self) -> str:
        """Take an accessibility tree snapshot. Returns text with UIDs."""
        result = await self.call("take_snapshot", {"verbose": False})
        return _result_text(result)

    async def trusted_click(self, uid: str) -> bool:
        """Click an element by UID using Puppeteer (trusted event).

        Unlike evaluate-based .click(), this dispatches a real mouse event
        that passes X's isTrusted checks.
        """
        try:
            await self.call("click", {"uid": uid})
            return True
        except Exception:
            return False

    async def find_list_checkbox(self, list_name: str) -> tuple[str | None, bool]:
        """Find a list checkbox in the a11y snapshot by name.

        Returns (uid, is_checked) or (None, False) if not found.
        """
        import re as _re
        snapshot = await self.take_a11y_snapshot()
        for line in snapshot.split("\n"):
            if "checkbox" in line.lower() and list_name.lower() in line.lower():
                uid_match = _re.search(r"uid=([^\s\]\[]+)", line)
                if uid_match:
                    is_checked = " checked" in line.lower() and "not checked" not in line.lower()
                    return uid_match.group(1), is_checked
        return None, False

    async def trusted_click_by_name(self, pattern: str, role: str | None = None) -> bool:
        """Find an element by name/text in the a11y snapshot and click with a trusted event.

        Args:
            pattern: regex to match against the snapshot line text
            role: optional role filter (e.g. 'button', 'checkbox', 'option')
        """
        import re as _re
        snapshot = await self.take_a11y_snapshot()
        regex = _re.compile(pattern, _re.IGNORECASE)
        for line in snapshot.split("\n"):
            if regex.search(line):
                if role and role.lower() not in line.lower():
                    continue
                uid_match = _re.search(r"uid=([^\s\]\[]+)", line)
                if uid_match:
                    return await self.trusted_click(uid_match.group(1))
        return False

    async def trusted_click_button(self, pattern: str) -> bool:
        """Find a button by name and click with a trusted event."""
        return await self.trusted_click_by_name(pattern, role="button")

    async def click_button_matching(self, pattern: str) -> bool:
        return bool(
            await self.evaluate(
                f"""() => {{
                    const re = new RegExp({json.dumps(pattern)}, 'i');
                    const items = Array.from(document.querySelectorAll('button,[role="button"]'));
                    const target = items.find((el) => re.test((el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim()));
                    if (!target) return false;
                    target.click();
                    return true;
                }}"""
            )
        )

    async def close_dialog(self) -> bool:
        return bool(
            await self.evaluate(
                """() => {
                    const target = document.querySelector('[data-testid="app-bar-close"], [aria-label="Close"]');
                    if (!target) return false;
                    target.click();
                    return true;
                }"""
            )
        )

    async def click_follow_button(self) -> str:
        return str(
            await self.evaluate(
                """() => {
                    const isProfileAction = (el) => !el.closest('[data-testid="UserCell"]');
                    const followByTestId = Array.from(document.querySelectorAll('[data-testid$="-follow"]'))
                        .find((el) => isProfileAction(el));
                    if (followByTestId) {
                        followByTestId.click();
                        return 'clicked-follow';
                    }

                    const followingByTestId = Array.from(document.querySelectorAll('[data-testid$="-unfollow"]'))
                        .find((el) => isProfileAction(el));
                    if (followingByTestId) return 'already-following';

                    const requestedByTestId = Array.from(document.querySelectorAll('[data-testid$="-follow-requested"]'))
                        .find((el) => isProfileAction(el));
                    if (requestedByTestId) return 'requested';

                    const buttons = Array.from(document.querySelectorAll('button,[role="button"]'));
                    const labels = (el) => [
                        el.getAttribute('data-testid') || '',
                        el.getAttribute('aria-label') || '',
                        el.innerText || '',
                        el.textContent || '',
                    ].join(' ').replace(/\\s+/g, ' ').trim();

                    const follow = buttons.find((el) => isProfileAction(el) && /^Follow(\\s|$)/i.test(labels(el)));
                    if (follow) {
                        follow.click();
                        return 'clicked-follow';
                    }

                    const following = buttons.find((el) => isProfileAction(el) && /Following/i.test(labels(el)));
                    if (following) return 'already-following';

                    const requested = buttons.find((el) => isProfileAction(el) && /Requested/i.test(labels(el)));
                    if (requested) return 'requested';

                    return 'no-follow-button';
                }"""
            )
        )

    async def click_unfollow_button(self) -> str:
        """Click the Following button to unfollow, then confirm the unfollow dialog."""
        return str(
            await self.evaluate(
                """() => {
                    const isProfileAction = (el) => !el.closest('[data-testid="UserCell"]');
                    const followingByTestId = Array.from(document.querySelectorAll('[data-testid$="-unfollow"]'))
                        .find((el) => isProfileAction(el));
                    if (followingByTestId) {
                        followingByTestId.click();
                        return 'clicked-following';
                    }

                    const followByTestId = Array.from(document.querySelectorAll('[data-testid$="-follow"]'))
                        .find((el) => isProfileAction(el));
                    if (followByTestId) return 'not-following';

                    const buttons = Array.from(document.querySelectorAll('button,[role="button"]'));
                    const labels = (el) => [
                        el.getAttribute('data-testid') || '',
                        el.getAttribute('aria-label') || '',
                        el.innerText || '',
                        el.textContent || '',
                    ].join(' ').replace(/\\s+/g, ' ').trim();

                    const following = buttons.find((el) => isProfileAction(el) && /Following/i.test(labels(el)));
                    if (following) {
                        following.click();
                        return 'clicked-following';
                    }

                    const follow = buttons.find((el) => isProfileAction(el) && /^Follow(\\s|$)/i.test(labels(el)));
                    if (follow) return 'not-following';

                    return 'no-follow-button';
                }"""
            )
        )

    async def confirm_unfollow(self) -> str:
        """Confirm the unfollow dialog that appears after clicking Following."""
        return str(
            await self.evaluate(
                """() => {
                    const buttons = Array.from(document.querySelectorAll('[data-testid="confirmationSheetConfirm"], button,[role="button"]'));
                    const labels = (el) => [
                        el.getAttribute('data-testid') || '',
                        el.innerText || '',
                        el.textContent || '',
                    ].join(' ').trim();

                    const confirm = buttons.find((el) => /Unfollow/i.test(labels(el)));
                    if (confirm) {
                        confirm.click();
                        return 'confirmed-unfollow';
                    }
                    return 'no-confirm-button';
                }"""
            )
        )

    async def get_list_title(self) -> str | None:
        value = await self.evaluate(
            """() => {
                const heading = document.querySelector('main h1, main h2, [role="heading"]');
                return heading ? heading.textContent.trim() : null;
            }"""
        )
        return value or None

    async def toggle_list_membership(self, list_name: str) -> str:
        return str(
            await self.evaluate(
                f"""() => {{
                    const targetName = {json.dumps(list_name)}.trim().toLowerCase();
                    const rows = Array.from(document.querySelectorAll('[role="checkbox"], [aria-checked], [aria-selected]'));
                    const row = rows.find((el) => {{
                        let node = el;
                        for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {{
                            const text = [
                                node.innerText || '',
                                node.textContent || '',
                                node.getAttribute ? node.getAttribute('aria-label') || '' : '',
                            ].join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
                            if (text.includes(targetName)) return true;
                        }}
                        return false;
                    }});
                    if (!row) return 'list-not-found-in-dialog';
                    const state = row.getAttribute('aria-checked') || row.getAttribute('aria-selected');
                    if (state === 'true') return 'already-member';
                    const clickTarget = row.closest('[role="option"], label, li, div') || row;
                    clickTarget.click();
                    return 'added';
                }}"""
            )
        )

    async def remove_from_list(self, list_name: str) -> str:
        return str(
            await self.evaluate(
                f"""() => {{
                    const targetName = {json.dumps(list_name)}.trim().toLowerCase();
                    const rows = Array.from(document.querySelectorAll('[role="checkbox"], [aria-checked], [aria-selected]'));
                    const row = rows.find((el) => {{
                        let node = el;
                        for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {{
                            const text = [
                                node.innerText || '',
                                node.textContent || '',
                                node.getAttribute ? node.getAttribute('aria-label') || '' : '',
                            ].join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
                            if (text.includes(targetName)) return true;
                        }}
                        return false;
                    }});
                    if (!row) return 'list-not-found-in-dialog';
                    const state = row.getAttribute('aria-checked') || row.getAttribute('aria-selected');
                    if (state !== 'true') return 'not-member';
                    const clickTarget = row.closest('[role="option"], label, li, div') || row;
                    clickTarget.click();
                    return 'removed';
                }}"""
            )
        )

    async def get_list_membership_state(self, list_name: str) -> str:
        return str(
            await self.evaluate(
                f"""() => {{
                    const targetName = {json.dumps(list_name)}.trim().toLowerCase();
                    const rows = Array.from(document.querySelectorAll('[role="checkbox"], [aria-checked], [aria-selected]'));
                    const row = rows.find((el) => {{
                        let node = el;
                        for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {{
                            const text = [
                                node.innerText || '',
                                node.textContent || '',
                                node.getAttribute ? node.getAttribute('aria-label') || '' : '',
                            ].join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
                            if (text.includes(targetName)) return true;
                        }}
                        return false;
                    }});
                    if (!row) return 'list-not-found-in-dialog';
                    const state = row.getAttribute('aria-checked') || row.getAttribute('aria-selected');
                    return state === 'true' ? 'selected' : 'not-selected';
                }}"""
            )
        )

    async def get_profile_summary(self, handle: str) -> dict[str, Any]:
        return await self.evaluate(
            f"""() => {{
                const handleLower = {json.dumps(handle.lower())};
                const requestedHandle = {json.dumps(handle)};
                function simpleProfilePath(href) {{
                    if (!href) return null;
                    const match = href.match(/^\\/([A-Za-z0-9_]+)$/);
                    return match ? match[1] : null;
                }}
                function handleFromPath(pathname) {{
                    if (!pathname) return null;
                    const match = pathname.match(/^\\/([A-Za-z0-9_]+)(?:\\/|$)/);
                    return match ? match[1] : null;
                }}
                function extractDisplayName(container) {{
                    if (!container) return null;
                    const spans = Array.from(container.querySelectorAll('span'))
                        .map((node) => (node.textContent || '').trim())
                        .filter(Boolean);
                    return spans.find((text) => !text.startsWith('@') && text !== '·') || null;
                }}
                function candidateHandlesFromPrimaryColumn() {{
                    const roots = Array.from(document.querySelectorAll('main, [data-testid="primaryColumn"]'));
                    const handles = new Set();
                    for (const root of roots) {{
                        const links = Array.from(root.querySelectorAll('a[href]'));
                        for (const link of links) {{
                            const profileHandle = simpleProfilePath(link.getAttribute('href'));
                            if (profileHandle) handles.add(profileHandle);
                        }}
                    }}
                    return Array.from(handles);
                }}
                function findProfileHeader() {{
                    const candidates = Array.from(document.querySelectorAll('main [data-testid="UserName"], main [data-testid="User-Name"]'))
                        .filter((node) => !node.closest('article'));
                    for (const candidate of candidates) {{
                        const hrefs = Array.from(candidate.querySelectorAll('a[href]'))
                            .map((link) => link.getAttribute('href'));
                        if (hrefs.some((href) => simpleProfilePath(href)?.toLowerCase() === handleLower)) {{
                            return candidate;
                        }}
                    }}
                    return null;
                }}
                function parseCount(hrefSuffix) {{
                    const links = Array.from(document.querySelectorAll('a[href]'));
                    const hit = links.find((link) => link.getAttribute('href')?.toLowerCase() === '/' + handleLower + '/' + hrefSuffix);
                    if (!hit) return null;
                    const raw = hit.textContent || '';
                    const match = raw.replace(/,/g, '').match(/([\\d.]+)([KMB])?/i);
                    if (!match) return null;
                    const base = Number(match[1]);
                    const suffix = (match[2] || '').toUpperCase();
                    if (suffix === 'K') return Math.round(base * 1000);
                    if (suffix === 'M') return Math.round(base * 1000000);
                    if (suffix === 'B') return Math.round(base * 1000000000);
                    return Math.round(base);
                }}
                function pickProfileLink(suffix) {{
                    const links = Array.from(document.querySelectorAll('a[href]'));
                    const hit = links.find((link) => link.getAttribute('href')?.toLowerCase() === '/' + handleLower + '/' + suffix);
                    return hit ? 'https://x.com' + hit.getAttribute('href') : null;
                }}
                const descriptionNode = document.querySelector('[data-testid="UserDescription"]');
                const locationNode = Array.from(document.querySelectorAll('[data-testid="UserProfileHeader_Items"] span'))
                  .map((node) => node.textContent?.trim())
                  .filter(Boolean);
                const header = findProfileHeader();
                const headerHandle = header
                    ? Array.from(header.querySelectorAll('a[href]'))
                        .map((link) => simpleProfilePath(link.getAttribute('href')))
                        .find((value) => value)
                    : null;
                const pathHandle = handleFromPath(location.pathname);
                const canonicalHref = document.querySelector('link[rel="canonical"]')?.getAttribute('href') || null;
                const canonicalHandle = canonicalHref ? handleFromPath(new URL(canonicalHref, location.origin).pathname) : null;
                const primaryColumnHandles = candidateHandlesFromPrimaryColumn();
                const resolvedHandle =
                    headerHandle ||
                    canonicalHandle ||
                    pathHandle ||
                    primaryColumnHandles.find((value) => value && value.toLowerCase() === handleLower) ||
                    primaryColumnHandles[0] ||
                    null;
                const displayName = extractDisplayName(header);
                const main = document.querySelector('main');
                const avatarSelectors = [
                    `a[href="/${{requestedHandle}}/photo"] img[src*="profile_images"]`,
                    `a[href$="/${{requestedHandle}}/photo"] img[src*="profile_images"]`,
                    `[data-testid="primaryColumn"] a[href*="/${{requestedHandle}}/photo"] img[src*="profile_images"]`,
                    `[data-testid="primaryColumn"] img[src*="profile_images"]`
                ];
                let avatar = null;
                for (const selector of avatarSelectors) {{
                    avatar = (main || document).querySelector(selector);
                    if (avatar) break;
                }}
                return {{
                    handle: {json.dumps(handle)},
                    page_url: location.href,
                    page_path_handle: pathHandle,
                    canonical_handle: canonicalHandle,
                    resolved_handle: resolvedHandle,
                    handle_match: resolvedHandle ? resolvedHandle.toLowerCase() === handleLower : false,
                    display_name: displayName,
                    bio: descriptionNode?.textContent?.trim() || null,
                    location_items: locationNode,
                    verified: !!document.querySelector('[data-testid="icon-verified"]'),
                    protected: !!document.querySelector('[data-testid="icon-lock"]'),
                    followers_count: parseCount('followers') ?? parseCount('verified_followers'),
                    following_count: parseCount('following'),
                    followers_url: pickProfileLink('followers') || pickProfileLink('verified_followers'),
                    following_url: pickProfileLink('following'),
                    avatar_url: avatar ? avatar.src.replace(/_normal\\./, '_400x400.') : null
                }};
            }}"""
        )

    async def collect_connections_from_page(self, edge_type: str, limit: int = 50) -> list[dict[str, Any]]:
        data = await self.evaluate(
            f"""() => {{
                const cards = Array.from(document.querySelectorAll('[data-testid="UserCell"]')).slice(0, {limit});
                return cards.map((card) => {{
                    const links = Array.from(card.querySelectorAll('a[href]'))
                        .map((link) => link.getAttribute('href'))
                        .filter(Boolean);
                    const profileHref = links.find((href) => /^\\/[A-Za-z0-9_]+$/.test(href));
                    const handle = profileHref ? profileHref.slice(1) : null;
                    const avatar = card.querySelector('img[src*="profile_images"]');
                    const spans = Array.from(card.querySelectorAll('span'))
                        .map((node) => node.textContent?.trim())
                        .filter(Boolean);
                    const description = card.innerText || '';
                    return {{
                        edge_type: {json.dumps(edge_type)},
                        handle,
                        display_name: spans[0] || null,
                        verified: !!card.querySelector('[data-testid="icon-verified"]'),
                        bio_excerpt: description.slice(0, 240),
                        profile_path: profileHref,
                        avatar_url: avatar ? avatar.src.replace(/_normal\\./, '_400x400.') : null,
                    }};
                }}).filter((item) => item.handle);
            }}"""
        )
        return data if isinstance(data, list) else []

    async def collect_visible_posts(self, limit: int = 20) -> list[dict[str, Any]]:
        posts = await self.evaluate(
            f"""() => {{
                function simpleProfilePath(href) {{
                    if (!href) return null;
                    const match = href.match(/^\\/([A-Za-z0-9_]+)$/);
                    return match ? match[1] : null;
                }}
                function extractAuthorInfo(article) {{
                    const containers = Array.from(article.querySelectorAll('[data-testid="User-Name"], [data-testid="UserName"]'));
                    for (const container of containers) {{
                        const profileHref = Array.from(container.querySelectorAll('a[href]'))
                            .map((node) => node.getAttribute('href'))
                            .find((href) => simpleProfilePath(href || ''));
                        if (!profileHref) continue;
                        const spans = Array.from(container.querySelectorAll('span'))
                            .map((node) => (node.textContent || '').trim())
                            .filter(Boolean);
                        const displayName = spans.find((text) => !text.startsWith('@') && text !== '·') || null;
                        return {{
                            profileHref,
                            handle: profileHref.replace(/^\\//, ''),
                            displayName,
                        }};
                    }}
                    const fallbackHref = Array.from(article.querySelectorAll('a[href]'))
                        .map((node) => node.getAttribute('href'))
                        .find((href) => simpleProfilePath(href || '')) || null;
                    return {{
                        profileHref: fallbackHref,
                        handle: fallbackHref ? fallbackHref.replace(/^\\//, '') : null,
                        displayName: null,
                    }};
                }}
                const articles = Array.from(document.querySelectorAll('article')).slice(0, {limit});
                return articles.map((article, index) => {{
                    const text = (article.innerText || '').trim();
                    const links = Array.from(article.querySelectorAll('a[href]'))
                        .map((node) => node.href)
                        .filter(Boolean);
                    const timeEl = article.querySelector('time');
                    const author = extractAuthorInfo(article);
                    const profileLink = author.profileHref;
                    const avatar = article.querySelector('img[src*="profile_images"]');
                    const handle = author.handle;
                    let tweetId = null;
                    for (const link of links) {{
                        const match = link.match(/\\/status\\/(\\d+)/);
                        if (match) {{
                            tweetId = match[1];
                            break;
                        }}
                    }}
                    return {{
                        index: index + 1,
                        tweet_id: tweetId,
                        text: text.slice(0, 2000),
                        links: Array.from(new Set(links)).slice(0, 10),
                        timestamp_iso: timeEl?.getAttribute('datetime') || null,
                        timestamp_text: timeEl?.textContent?.trim() || null,
                        profile_path: profileLink,
                        handle,
                        display_name: author.displayName,
                        avatar_url: avatar ? avatar.src.replace(/_normal\\./, '_400x400.') : null,
                        verified: !!article.querySelector('[data-testid="icon-verified"]'),
                    }};
                }});
            }}"""
        )
        return posts if isinstance(posts, list) else []

    async def scroll_page(self) -> int:
        position = await self.evaluate(
            """async () => {
                window.scrollBy(0, window.innerHeight * 2);
                await new Promise((resolve) => setTimeout(resolve, 2500));
                return Math.floor(window.scrollY);
            }"""
        )
        return int(position or 0)

    async def scroll_modal(self) -> int:
        """Scroll inside a modal/dialog overlay instead of the main page.

        X's /members page shows the member list inside a modal with its own
        scroll container. This finds the scrollable container and scrolls it.
        """
        position = await self.evaluate(
            """async () => {
                // Find the scrollable modal container — typically a div with
                // overflow-y: auto/scroll inside a dialog or overlay
                const candidates = Array.from(document.querySelectorAll(
                    '[role="dialog"] [style*="overflow"], [aria-modal] [style*="overflow"], ' +
                    '[data-testid="sheetDialog"] *, [role="dialog"] *'
                ));
                // Find the tallest scrollable element
                let scroller = null;
                let maxHeight = 0;
                for (const el of document.querySelectorAll('*')) {
                    const style = window.getComputedStyle(el);
                    if ((style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                        el.scrollHeight > el.clientHeight &&
                        el.scrollHeight > maxHeight) {
                        maxHeight = el.scrollHeight;
                        scroller = el;
                    }
                }
                if (!scroller) {
                    // Fallback: scroll the main page
                    window.scrollBy(0, window.innerHeight * 2);
                    await new Promise(r => setTimeout(r, 2500));
                    return -1;
                }
                scroller.scrollBy(0, scroller.clientHeight);
                await new Promise(r => setTimeout(r, 2500));
                return Math.floor(scroller.scrollTop);
            }"""
        )
        return int(position or 0)


async def scroll_and_collect(
    browser: ChromeMCPBrowser,
    *,
    seen_tweet_ids: set[str] | None = None,
    max_scrolls: int = 8,
    posts_per_scroll: int = 20,
) -> list[dict[str, Any]]:
    seen_tweet_ids = seen_tweet_ids or set()
    collected: list[dict[str, Any]] = []
    local_seen = set(seen_tweet_ids)
    consecutive_empty = 0

    for _ in range(max_scrolls):
        posts = await browser.collect_visible_posts(posts_per_scroll)
        new_count = 0
        for post in posts:
            tweet_id = post.get("tweet_id")
            if tweet_id and tweet_id in local_seen:
                continue
            if tweet_id:
                local_seen.add(tweet_id)
            collected.append(post)
            new_count += 1

        if new_count == 0:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                break
        else:
            consecutive_empty = 0

        await browser.scroll_page()

    return collected
