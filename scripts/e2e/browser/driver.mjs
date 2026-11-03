// Generic browser driver for the layer-2 harness.
//
// Reads one JSON job from argv, walks its steps against a real Chromium, and
// prints the final page. Assertions stay in Python so the scenarios read like
// the production incidents they cover.
//
// Environment:
//   PLAYWRIGHT_CORE  absolute path to playwright-core's index.mjs
//   CHROMIUM_PATH    absolute path to the Chromium executable to launch

const job = JSON.parse(process.argv[2]);
const { chromium } = await import(process.env.PLAYWRIGHT_CORE || "playwright-core");

const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH,
  args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-crash-reporter", "--disable-breakpad"],
});
const context = await browser.newContext();
const timeout = job.timeoutMs ?? 8000;

for (const cookie of job.cookies ?? []) {
  await context.addCookies([cookie]);
}

const page = await context.newPage();
for (const step of job.steps) {
  if (step.goto) {
    await page.goto(step.goto, { waitUntil: "load", timeout });
  }
  if (step.setCookie) {
    await context.addCookies([step.setCookie]);
  }
  if (step.click) {
    await Promise.all([
      page.waitForNavigation({ waitUntil: "load", timeout }).catch(() => null),
      page.click(step.click, { timeout }),
    ]).catch(async (error) => {
      // Some clicks legitimately do not navigate (validation re-render).
      if (step.optional) return;
      throw error;
    });
  }
}

const result = await page.evaluate(() => ({
  url: window.location.href,
  text: document.body ? document.body.innerText : "",
  html: document.body ? document.body.innerHTML : "",
}));
console.log(JSON.stringify(result));
await browser.close();
