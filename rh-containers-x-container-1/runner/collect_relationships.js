const path = require('path');
const { launchContext, writeJson, getBody, looksLoggedIn, jitter, OUT_DIR } = require('./lib');

const HANDLES = JSON.parse(process.env.X_REL_HANDLES_JSON || '[]');
const MAX_PROFILE_SCROLLS = Number(process.env.X_REL_PROFILE_SCROLLS || '2');
const MAX_EDGE_SCROLLS = Number(process.env.X_REL_EDGE_SCROLLS || '5');
const EDGE_LIMIT = Number(process.env.X_REL_EDGE_LIMIT || '100');
const DIRECTION = process.env.X_REL_DIRECTION || 'both';

async function scrapeProfile(page, handle) {
  await page.goto(`https://x.com/${handle}`, { waitUntil: 'domcontentloaded', timeout: 45000 });
  await page.waitForTimeout(jitter(2500, 1500));
  const body = await getBody(page);
  if (/This account doesn't exist|Account suspended|These posts are protected/i.test(body)) {
    return { handle, status: 'unavailable' };
  }
  for (let i = 0; i < MAX_PROFILE_SCROLLS; i++) {
    await page.evaluate(() => window.scrollBy(0, window.innerHeight));
    await page.waitForTimeout(jitter(1200, 800));
  }
  return page.evaluate(({ profileHandle }) => {
    function text(selector) {
      return document.querySelector(selector)?.textContent?.trim() || null;
    }
    function pickProfileLink(suffix) {
      const links = Array.from(document.querySelectorAll('a[href]'));
      const hit = links.find((link) => link.getAttribute('href') === `/${profileHandle}/${suffix}`);
      return hit ? `https://x.com${hit.getAttribute('href')}` : null;
    }
    function parseCount(hrefSuffix) {
      const links = Array.from(document.querySelectorAll('a[href]'));
      const hit = links.find((link) => link.getAttribute('href') === `/${profileHandle}/${hrefSuffix}`);
      if (!hit) return null;
      const raw = hit.textContent || '';
      const match = raw.replace(/,/g, '').match(/([\d.]+)([KMB])?/i);
      if (!match) return null;
      const base = Number(match[1]);
      const suffix = (match[2] || '').toUpperCase();
      if (suffix === 'K') return Math.round(base * 1000);
      if (suffix === 'M') return Math.round(base * 1000000);
      if (suffix === 'B') return Math.round(base * 1000000000);
      return Math.round(base);
    }
    const descriptionNode = document.querySelector('[data-testid="UserDescription"]');
    const locationNode = Array.from(document.querySelectorAll('[data-testid="UserProfileHeader_Items"] span'))
      .map((node) => node.textContent?.trim())
      .filter(Boolean);
    return {
      handle: profileHandle,
      status: 'ok',
      display_name: text('[data-testid="UserName"] span'),
      bio: descriptionNode?.textContent?.trim() || null,
      location_items: locationNode,
      verified: !!document.querySelector('[data-testid="icon-verified"]'),
      protected: !!document.querySelector('[data-testid="icon-lock"]'),
      followers_count: parseCount('followers'),
      following_count: parseCount('following'),
      followers_url: pickProfileLink('followers'),
      following_url: pickProfileLink('following'),
    };
  }, { profileHandle: handle });
}

async function scrapeConnections(page, url, edgeType) {
  if (!url) return [];
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
  await page.waitForTimeout(jitter(2500, 1500));
  const seen = new Set();
  const edges = [];

  for (let scroll = 0; scroll < MAX_EDGE_SCROLLS && edges.length < EDGE_LIMIT; scroll++) {
    const batch = await page.evaluate((edgeKind) => {
      const cards = Array.from(document.querySelectorAll('[data-testid="UserCell"]')).slice(0, 50);
      return cards.map((card) => {
        const links = Array.from(card.querySelectorAll('a[href]'))
          .map((link) => link.getAttribute('href'))
          .filter(Boolean);
        const profileHref = links.find((href) => /^\/[A-Za-z0-9_]+$/.test(href));
        const handle = profileHref ? profileHref.slice(1) : null;
        const spans = Array.from(card.querySelectorAll('span')).map((node) => node.textContent?.trim()).filter(Boolean);
        const description = card.innerText || '';
        return {
          edge_type: edgeKind,
          handle,
          display_name: spans[0] || null,
          verified: !!card.querySelector('[data-testid="icon-verified"]'),
          bio_excerpt: description.slice(0, 240),
          profile_path: profileHref,
        };
      }).filter((item) => item.handle);
    }, edgeType);

    let newCount = 0;
    for (const item of batch) {
      const key = `${edgeType}:${item.handle.toLowerCase()}`;
      if (seen.has(key)) continue;
      seen.add(key);
      edges.push(item);
      newCount++;
      if (edges.length >= EDGE_LIMIT) break;
    }
    if (newCount === 0) break;
    await page.evaluate(() => window.scrollBy(0, window.innerHeight * 1.5));
    await page.waitForTimeout(jitter(1800, 1200));
  }
  return edges;
}

(async () => {
  const result = {
    status: 'unknown',
    task_type: 'collect_relationships',
    profiles: [],
    edges: [],
    collected_at: new Date().toISOString(),
  };
  let browser;
  let context;
  try {
    ({ browser, context } = await launchContext());
    const page = context.pages()[0] || await context.newPage();
    await page.goto('https://x.com/home', { waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.waitForTimeout(jitter(2500, 1500));
    const body = await getBody(page);
    if (!looksLoggedIn(body)) {
      result.status = 'error';
      result.error = 'session-not-authenticated';
      writeJson('collect_relationships.json', result);
      console.log(JSON.stringify(result));
      return;
    }

    for (const handle of HANDLES) {
      const profile = await scrapeProfile(page, handle);
      result.profiles.push(profile);
      if (profile.status !== 'ok') continue;
      const following = DIRECTION === 'followers' ? [] : await scrapeConnections(page, profile.following_url, 'following');
      const followers = DIRECTION === 'following' ? [] : await scrapeConnections(page, profile.followers_url, 'followers');
      for (const edge of [...following, ...followers]) {
        result.edges.push({ source_handle: handle, ...edge });
      }
      await page.waitForTimeout(jitter(1800, 900));
    }

    result.profile_count = result.profiles.filter((profile) => profile.status === 'ok').length;
    result.edge_count = result.edges.length;
    result.status = 'ok';
    result.evidence = {
      screenshot_path: path.join(OUT_DIR, 'collect_relationships.png'),
      handles_visited: HANDLES,
    };
    await page.screenshot({ path: result.evidence.screenshot_path, fullPage: true });
  } catch (error) {
    result.status = 'error';
    result.error = String(error?.message || error);
  }

  writeJson('collect_relationships.json', result);
  console.log(JSON.stringify(result));
  if (context) await context.close();
  if (browser) await browser.close();
})();
