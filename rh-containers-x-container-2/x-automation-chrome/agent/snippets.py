"""
Reusable JS evaluation snippets for X DOM interaction.
Ported from x-research-lab-v4-playwright-*/runner/lib.js
These are sent via MCP's evaluate tool to run inside Chrome's page context.
They are the only browser-side extraction patterns the collection agent should use.
"""

LOOKS_LOGGED_IN = """
(() => {
  const body = document.body?.innerText || '';
  const loggedIn = /For you|Following|What is happening|Post|Home/.test(body);
  const loginPage = /Sign in to X|Phone, email, or username/.test(body);
  return { logged_in: loggedIn && !loginPage, url: window.location.href };
})()
"""

COLLECT_VISIBLE_POSTS = """
((limit) => {
  const articles = Array.from(document.querySelectorAll('article')).slice(0, limit);
  return articles.map((a, i) => {
    const text = (a.innerText || '').trim();
    const links = Array.from(a.querySelectorAll('a[href]')).map(x => x.href).filter(Boolean);
    const timeEl = a.querySelector('time');
    const tsIso = timeEl?.getAttribute('datetime') || null;
    const tsText = timeEl?.textContent?.trim() || null;
    const profileLink = Array.from(a.querySelectorAll('a[href]'))
      .map(x => x.getAttribute('href'))
      .find(h => /^\\/[A-Za-z0-9_]+$/.test(h || '')) || null;
    let tweet_id = null;
    for (const link of links) {
      const m = link.match(/\\/status\\/(\\d+)/);
      if (m) { tweet_id = m[1]; break; }
    }
    return {
      index: i + 1,
      tweet_id,
      text: text.slice(0, 2000),
      links: Array.from(new Set(links)).slice(0, 10),
      timestamp_iso: tsIso,
      timestamp_text: tsText,
      profile_path: profileLink
    };
  });
})(%d)
"""

SCROLL_DOWN = """
window.scrollBy(0, window.innerHeight * 2);
"""

EXTRACT_AVATAR_URL = """
(() => {
  const img = document.querySelector('img[src*="profile_images"]');
  if (!img) return null;
  return img.src.replace(/_normal\\./, '_400x400.');
})()
"""

EXTRACT_PROFILE_METADATA = """
(() => {
  const name = document.querySelector('[data-testid="UserName"]')?.innerText || '';
  const bio = document.querySelector('[data-testid="UserDescription"]')?.innerText || '';
  const links = Array.from(document.querySelectorAll('[data-testid="UserProfileHeader_Items"] a'))
    .map(a => a.href).filter(Boolean);
  const stats = Array.from(document.querySelectorAll('a[href*="/following"], a[href*="/verified_followers"]'))
    .map(a => ({ text: a.innerText?.trim(), href: a.getAttribute('href') }));
  return { name, bio, links, stats, url: window.location.href };
})()
"""

COLLECT_USER_CELLS = """
((limit) => {
  const cells = Array.from(document.querySelectorAll('[data-testid="UserCell"]')).slice(0, limit);
  return cells.map(cell => {
    const links = Array.from(cell.querySelectorAll('a[href]'))
      .map(a => a.getAttribute('href'))
      .filter(h => /^\\/[A-Za-z0-9_]+$/.test(h || ''));
    const handle = links[0]?.replace('/', '') || null;
    const text = (cell.innerText || '').trim();
    return { handle, text: text.slice(0, 500) };
  });
})(%d)
"""

CHECK_FOLLOW_STATE = """
(() => {
  const followBtn = document.querySelector('[data-testid="placementTracking"] [role="button"]');
  const unfollowBtn = document.querySelector('[data-testid="placementTracking"] [data-testid$="-unfollow"]');
  const text = followBtn?.innerText?.trim() || '';
  return {
    has_follow_button: !!followBtn,
    has_unfollow_button: !!unfollowBtn,
    button_text: text,
    already_following: text === 'Following' || !!unfollowBtn,
  };
})()
"""


def collect_visible_posts_js(limit: int = 20) -> str:
    return COLLECT_VISIBLE_POSTS % limit


def collect_user_cells_js(limit: int = 50) -> str:
    return COLLECT_USER_CELLS % limit


APPROVED_SNIPPETS = {
    "check_login_state": LOOKS_LOGGED_IN,
    "collect_visible_posts": "collect_visible_posts_js(limit)",
    "scroll_down": SCROLL_DOWN,
    "extract_avatar_url": EXTRACT_AVATAR_URL,
    "extract_profile_metadata": EXTRACT_PROFILE_METADATA,
    "collect_user_cells": "collect_user_cells_js(limit)",
    "check_follow_state": CHECK_FOLLOW_STATE,
}
