const path = require('path');
const { launchContext, writeJson, getBody, looksLoggedIn, OUT_DIR } = require('./lib');

(async () => {
  const result = { status: 'unknown', task_type: 'verify_session' };
  let browser;
  let context;
  try {
    ({ browser, context } = await launchContext());
    const page = context.pages()[0] || await context.newPage();
    await page.goto('https://x.com/home', { waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.waitForTimeout(6000);
    const body = await getBody(page);
    result.url = page.url();
    result.title = await page.title();
    result.logged_in = looksLoggedIn(body);
    result.excerpt = body.slice(0, 1600);
    result.status = 'ok';
    result.evidence = {
      screenshot_path: path.join(OUT_DIR, 'verify_session.png'),
      screenshot_taken: true,
    };
    await page.screenshot({ path: result.evidence.screenshot_path, fullPage: true });
  } catch (error) {
    result.status = 'error';
    result.error = String(error?.message || error);
  }
  writeJson('verify_session.json', result);
  console.log(JSON.stringify(result));
  if (context) await context.close();
  if (browser) await browser.close();
})();
