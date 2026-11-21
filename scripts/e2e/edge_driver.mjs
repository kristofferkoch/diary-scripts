// Enroll a virtual passkey through Caddy, open the console, and optionally decide.
const { chromium } = await import(process.env.E2E_PLAYWRIGHT_CORE);

const [origin, host, transaction, repo] = process.argv.slice(2);
const reviewUrl = origin + "/lock/merge/" + (repo === "diary" ? "?repo=diary" : "");

const browser = await chromium.launch({
  executablePath: process.env.E2E_CHROMIUM,
  args: [
    "--no-sandbox", "--disable-crash-reporter", "--disable-breakpad",
    `--host-resolver-rules=MAP ${host} 127.0.0.1`,
  ],
});
const context = await browser.newContext({ ignoreHTTPSErrors: true });
if (transaction && transaction !== "-") {
  await context.addCookies([{
    name: "diary_lock_fresh_unlock", value: `${transaction}.${transaction}`,
    domain: host, path: "/lock/merge", secure: true, httpOnly: true, sameSite: "Strict",
  }]);
}
const page = await context.newPage();
const errors = [];
page.on("pageerror", (error) => errors.push(String(error)));
const cdp = await context.newCDPSession(page);
await cdp.send("WebAuthn.enable", { enableUI: false });
await cdp.send("WebAuthn.addVirtualAuthenticator", {
  options: {
    protocol: "ctap2", transport: "internal",
    hasResidentKey: true, hasUserVerification: true,
    isUserVerified: true, automaticPresenceSimulation: true,
  },
});

const result = { steps: [], errors };
await page.goto(origin + "/lock/enroll/", { waitUntil: "domcontentloaded" });
await page.getByLabel("One-time enrollment code").fill(process.env.E2E_ENROLLMENT_CODE);
await page.getByRole("button", { name: "Create passkey" }).click();
await page.waitForURL((url) => url.pathname === "/lock/", { timeout: 20000 });
result.steps.push({ step: "registered", url: page.url() });

await page.goto(reviewUrl, { waitUntil: "domcontentloaded" });
await page.waitForTimeout(500);
result.steps.push({
  step: "console", url: page.url(),
  text: (await page.locator("body").innerText()).slice(0, 300),
});

if (transaction && transaction !== "-") {
  const merge = page.locator('button[value="merge"]');
  if (await merge.count()) {
    await merge.first().click();
    await page.waitForTimeout(1000);
    result.steps.push({
      step: "decided", url: page.url(),
      text: (await page.locator("body").innerText()).slice(0, 400),
    });
  }
}

console.log(JSON.stringify(result, null, 2));
await browser.close();
