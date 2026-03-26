const fs = require('fs');
const path = require('path');
const playwright = require(process.env.PLAYWRIGHT_PACKAGE || '/Users/azayaka/.openclaw/workspace/researchhub/labs/x-research-lab-v4-playwright-test/runner/node_modules/playwright');

const OUT_DIR = process.env.X_AUTOMATION_OUT_DIR || path.join(__dirname, '..', 'out');
const CDP_URL = process.env.CDP_URL || 'http://127.0.0.1:9223';

async function launchContext() {
  const browser = await playwright.chromium.connectOverCDP(CDP_URL);
  const context = browser.contexts()[0] || await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: 'en-US',
    timezoneId: 'Pacific/Honolulu',
  });
  return { browser, context };
}

function ensureOut() {
  fs.mkdirSync(OUT_DIR, { recursive: true });
}

function writeJson(name, obj) {
  ensureOut();
  fs.writeFileSync(path.join(OUT_DIR, name), JSON.stringify(obj, null, 2));
}

async function getBody(page) {
  return page.locator('body').innerText().catch(() => '');
}

function looksLoggedIn(body) {
  return /For you|Following|What is happening|Post|Home/.test(body)
    && !/Sign in to X|Phone, email, or username/.test(body);
}

function jitter(base, variance) {
  return base + Math.random() * variance;
}

async function collectVisiblePosts(page, limit = 10) {
  return page.evaluate((pageLimit) => {
    const articles = Array.from(document.querySelectorAll('article')).slice(0, pageLimit);
    return articles.map((article, i) => {
      const text = (article.innerText || '').trim();
      const links = Array.from(article.querySelectorAll('a[href]')).map((x) => x.href).filter(Boolean);
      const timeEl = article.querySelector('time');
      const tsIso = timeEl?.getAttribute('datetime') || null;
      const tsText = timeEl?.textContent?.trim() || null;
      const profileLink = Array.from(article.querySelectorAll('a[href]'))
        .map((x) => x.getAttribute('href'))
        .find((href) => /^\/[A-Za-z0-9_]+$/.test(href || '')) || null;
      let tweetId = null;
      for (const link of links) {
        const match = link.match(/\/status\/(\d+)/);
        if (match) {
          tweetId = match[1];
          break;
        }
      }
      return {
        index: i + 1,
        tweet_id: tweetId,
        text: text.slice(0, 2000),
        links: Array.from(new Set(links)).slice(0, 10),
        timestamp_iso: tsIso,
        timestamp_text: tsText,
        profile_path: profileLink,
      };
    });
  }, limit).catch(() => []);
}

async function scrollAndCollect(page, opts = {}) {
  const seenTweetIds = opts.seenTweetIds || new Set();
  const maxScrolls = opts.maxScrolls || 8;
  const postsPerScroll = opts.postsPerScroll || 20;
  const collected = [];
  const localSeen = new Set(seenTweetIds);
  let consecutiveEmpty = 0;

  for (let scroll = 0; scroll < maxScrolls; scroll++) {
    const posts = await collectVisiblePosts(page, postsPerScroll);
    let newCount = 0;
    for (const post of posts) {
      if (post.tweet_id && localSeen.has(post.tweet_id)) continue;
      if (post.tweet_id) localSeen.add(post.tweet_id);
      collected.push(post);
      newCount++;
    }
    if (newCount === 0) {
      consecutiveEmpty++;
      if (consecutiveEmpty >= 2) break;
    } else {
      consecutiveEmpty = 0;
    }
    await page.evaluate(() => window.scrollBy(0, window.innerHeight * 2));
    await page.waitForTimeout(jitter(2000, 3000));
  }
  return collected;
}

module.exports = {
  launchContext,
  writeJson,
  getBody,
  looksLoggedIn,
  jitter,
  collectVisiblePosts,
  scrollAndCollect,
  OUT_DIR,
};
