const { launchContext, writeJson, getBody, looksLoggedIn, jitter } = require('./lib');

const HANDLES = JSON.parse(process.env.X_AVATAR_HANDLES_JSON || '[]');

(async () => {
  const result = {
    status: 'unknown',
    task_type: 'pull_avatars',
    profiles: [],
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
      writeJson('pull_avatars.json', result);
      console.log(JSON.stringify(result));
      return;
    }

    for (const handle of HANDLES) {
      try {
        await page.goto(`https://x.com/${handle}`, { waitUntil: 'domcontentloaded', timeout: 45000 });
        await page.waitForTimeout(jitter(2000, 1000));
        const profile = await page.evaluate((profileHandle) => {
          const normalizeAvatarUrl = (url) => url ? url.replace(/_normal\./, '_400x400.') : null;
          const main = document.querySelector('main');
          const selectors = [
            `a[href="/${profileHandle}/photo"] img[src*="profile_images"]`,
            `a[href$="/${profileHandle}/photo"] img[src*="profile_images"]`,
            `[data-testid="primaryColumn"] a[href*="/${profileHandle}/photo"] img[src*="profile_images"]`,
            `[data-testid="primaryColumn"] img[src*="profile_images"]`,
          ];
          let img = null;
          for (const selector of selectors) {
            img = (main || document).querySelector(selector);
            if (img) break;
          }
          const avatarUrl = normalizeAvatarUrl(img ? img.src : null);
          const displayName = document.querySelector('[data-testid="UserName"] span')?.textContent?.trim() || null;

          // Extract follower/following counts
          const parseCount = (text) => {
            if (!text) return null;
            text = text.replace(/,/g, '').trim();
            const match = text.match(/([\d.]+)\s*(K|M|B)?/i);
            if (!match) return null;
            let num = parseFloat(match[1]);
            const suffix = (match[2] || '').toUpperCase();
            if (suffix === 'K') num *= 1000;
            else if (suffix === 'M') num *= 1000000;
            else if (suffix === 'B') num *= 1000000000;
            return Math.round(num);
          };

          let followersCount = null;
          let followingCount = null;
          const followersLink = document.querySelector(`a[href="/${profileHandle}/verified_followers"], a[href="/${profileHandle}/followers"]`);
          const followingLink = document.querySelector(`a[href="/${profileHandle}/following"]`);
          if (followersLink) {
            const span = followersLink.querySelector('span span');
            if (span) followersCount = parseCount(span.textContent);
          }
          if (followingLink) {
            const span = followingLink.querySelector('span span');
            if (span) followingCount = parseCount(span.textContent);
          }

          return { handle: profileHandle, display_name: displayName, avatar_url: avatarUrl, followers_count: followersCount, following_count: followingCount };
        }, handle);
        result.profiles.push({ status: 'ok', ...profile });
      } catch (error) {
        result.profiles.push({ handle, status: 'error', error: String(error?.message || error) });
      }
    }
    result.status = 'ok';
  } catch (error) {
    result.status = 'error';
    result.error = String(error?.message || error);
  }

  writeJson('pull_avatars.json', result);
  console.log(JSON.stringify(result));
  if (context) await context.close();
  if (browser) await browser.close();
})();
