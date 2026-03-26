const fs = require('fs');
const path = require('path');
const {
  launchContext,
  writeJson,
  getBody,
  looksLoggedIn,
  scrollAndCollect,
  jitter,
  OUT_DIR,
} = require('./lib');

const LIST_URLS = JSON.parse(process.env.X_LIST_URLS_JSON || '[]');
const LIST_LABELS = JSON.parse(process.env.X_LIST_LABELS_JSON || '[]');
const COLLECT_FEED = process.env.X_COLLECT_FEED === '1';
const MAX_SCROLLS = Number(process.env.X_MAX_SCROLLS || '8');
const SEEN_IDS_FILE = process.env.X_SEEN_IDS_FILE || '';

function loadSeenIds() {
  if (!SEEN_IDS_FILE) return new Set();
  try {
    const data = JSON.parse(fs.readFileSync(SEEN_IDS_FILE, 'utf-8'));
    return new Set(data.tweet_ids || []);
  } catch {
    return new Set();
  }
}

function saveSeenIds(seenSet) {
  if (!SEEN_IDS_FILE) return;
  const data = {
    tweet_ids: Array.from(seenSet),
    last_updated: new Date().toISOString(),
  };
  fs.writeFileSync(SEEN_IDS_FILE, JSON.stringify(data, null, 2));
}

(async () => {
  const result = {
    status: 'unknown',
    task_type: 'collect_feeds',
    sources: [],
    total_posts: 0,
    collection_method: 'feed',
  };
  let browser;
  let context;
  const seenIds = loadSeenIds();

  try {
    ({ browser, context } = await launchContext());
    const page = context.pages()[0] || await context.newPage();
    await page.goto('https://x.com/home', { waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.waitForTimeout(jitter(3000, 2000));
    const homeBody = await getBody(page);

    if (!looksLoggedIn(homeBody)) {
      result.status = 'error';
      result.error = 'session-not-authenticated';
      writeJson('collect_feeds.json', result);
      console.log(JSON.stringify(result));
      return;
    }

    for (let i = 0; i < LIST_URLS.length; i++) {
      const listUrl = LIST_URLS[i];
      const label = LIST_LABELS[i] || `list-${i}`;
      const source = { label, url: listUrl, status: 'unknown', posts: [] };
      try {
        await page.goto(listUrl, { waitUntil: 'domcontentloaded', timeout: 45000 });
        await page.waitForTimeout(jitter(3000, 3000));
        const body = await getBody(page);
        if (/doesn't exist|not found|This List is empty/i.test(body) && body.length < 500) {
          source.status = 'empty-or-not-found';
        } else {
          const posts = await scrollAndCollect(page, {
            seenTweetIds: seenIds,
            maxScrolls: MAX_SCROLLS,
            postsPerScroll: 20,
          });
          for (const post of posts) {
            post.collection_method = 'list';
            post.collection_source = label;
            if (post.tweet_id) seenIds.add(post.tweet_id);
          }
          source.posts = posts;
          source.post_count = posts.length;
          source.status = 'ok';
        }
      } catch (error) {
        source.status = 'error';
        source.error = String(error?.message || error);
      }
      result.sources.push(source);
      result.total_posts += source.post_count || 0;
      if (i < LIST_URLS.length - 1) await page.waitForTimeout(jitter(3000, 4000));
    }

    if (COLLECT_FEED) {
      const source = { label: 'following-feed', url: 'https://x.com/home', status: 'unknown', posts: [] };
      try {
        await page.goto('https://x.com/home', { waitUntil: 'domcontentloaded', timeout: 45000 });
        await page.waitForTimeout(jitter(2000, 2000));
        const followingTab = page.getByRole('tab', { name: 'Following' }).first();
        if (await followingTab.isVisible().catch(() => false)) {
          await followingTab.click();
          await page.waitForTimeout(jitter(3000, 2000));
        }
        const posts = await scrollAndCollect(page, {
          seenTweetIds: seenIds,
          maxScrolls: MAX_SCROLLS,
          postsPerScroll: 20,
        });
        for (const post of posts) {
          post.collection_method = 'feed';
          post.collection_source = 'following-feed';
          if (post.tweet_id) seenIds.add(post.tweet_id);
        }
        source.posts = posts;
        source.post_count = posts.length;
        source.status = 'ok';
      } catch (error) {
        source.status = 'error';
        source.error = String(error?.message || error);
      }
      result.sources.push(source);
      result.total_posts += source.post_count || 0;
    }

    saveSeenIds(seenIds);
    result.status = 'ok';
    result.evidence = {
      screenshot_path: path.join(OUT_DIR, 'collect_feeds_final.png'),
      visited_urls: result.sources.map((source) => source.url),
    };
    await page.screenshot({ path: result.evidence.screenshot_path, fullPage: true });
  } catch (error) {
    result.status = 'error';
    result.error = String(error?.message || error);
  }

  writeJson('collect_feeds.json', result);
  console.log(JSON.stringify(result));
  if (context) await context.close();
  if (browser) await browser.close();
})();
