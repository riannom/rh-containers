const path = require('path');
const {
  launchContext,
  writeJson,
  getBody,
  looksLoggedIn,
  collectVisiblePosts,
  jitter,
  OUT_DIR,
} = require('./lib');

const QUERY = process.env.X_QUERY || '';
const LIMIT = Number(process.env.X_LIMIT || '10');

(async () => {
  const result = { status: 'unknown', query: QUERY, limit: LIMIT };
  let browser;
  let context;
  try {
    ({ browser, context } = await launchContext());
    const page = context.pages()[0] || await context.newPage();
    await page.goto('https://x.com/home', { waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.waitForTimeout(jitter(4000, 3000));
    const homeBody = await getBody(page);
    if (!looksLoggedIn(homeBody)) {
      result.status = 'fail';
      result.reason = 'session-not-authenticated';
    } else if (!QUERY) {
      result.status = 'fail';
      result.reason = 'missing-query';
    } else {
      const searchUrl = `https://x.com/search?q=${encodeURIComponent(QUERY)}&src=typed_query&f=live`;
      await page.goto(searchUrl, { waitUntil: 'domcontentloaded', timeout: 45000 });
      await page.waitForTimeout(jitter(6000, 5000));
      const body = await getBody(page);
      const posts = await collectVisiblePosts(page, LIMIT);
      result.status = 'pass';
      result.reason = 'search-collected';
      result.url = page.url();
      result.title = await page.title();
      result.excerpt = body.slice(0, 1600);
      result.posts = posts;
      await page.screenshot({ path: path.join(OUT_DIR, 'search_research.png'), fullPage: true });
    }
  } catch (error) {
    result.status = 'error';
    result.reason = String(error);
  }
  writeJson('search_research.json', result);
  console.log(JSON.stringify(result, null, 2));
  if (browser) await browser.close().catch(() => {});
})();
