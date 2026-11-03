// Drive the real Authelia login and WebAuthn ceremony through Caddy, then
// optionally click the console's Merge button and report what happened.
const { chromium } = await import(process.env.E2E_PLAYWRIGHT_CORE);
const { readFileSync } = await import("node:fs");

const [origin, host, transaction] = process.argv.slice(2);
const notifierFile = process.env.E2E_NOTIFIER_FILE;

const readCode = () => {
  try {
    const lines = readFileSync(notifierFile, "utf8").split("\n").map((l) => l.trim());
    return lines.filter((l) => /^[A-Z0-9]{6,10}$/.test(l)).pop() ?? "";
  } catch {
    return "";
  }
};
const waitForCode = async (previous, timeoutMs = 15000) => {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const code = readCode();
    if (code && code !== previous) return code;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  return "";
};

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

await page.goto(origin + "/lock/merge/", { waitUntil: "domcontentloaded" });
await page.waitForSelector("#username-textfield", { timeout: 20000 });
await page.fill("#username-textfield", "owner");
await page.fill("#password-textfield", "owner-password");
await page.click("button[type=submit]");
await page.waitForTimeout(2500);
result.steps.push({ step: "second-factor", url: page.url() });

// First login for this user: register the virtual authenticator as device one.
if (page.url().includes("/2fa/webauthn")) {
  const previous = readCode();
  await page.getByText("Register device", { exact: false }).first().click();
  await page.waitForTimeout(2000);
  if ((await page.locator("#one-time-code").count()) === 0) {
    const add = page.getByRole("button", { name: /add/i });
    if (await add.count()) {
      await add.first().click();
    }
  }
  await page.waitForSelector("#one-time-code", { timeout: 15000 });
  await page.fill("#one-time-code", await waitForCode(previous));
  await page.getByRole("button", { name: /verify/i }).first().click();
  await page.waitForSelector("#webauthn-credential-description", { timeout: 15000 });
  await page.fill("#webauthn-credential-description", "virtual key");
  await page.getByRole("button", { name: /next/i }).first().click();
  await page.waitForTimeout(4000);
  result.steps.push({ step: "registered", url: page.url() });
}

await page.goto(origin + "/lock/merge/", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(1500);
result.steps.push({ step: "console", url: page.url(), text: (await page.locator("body").innerText()).slice(0, 300) });

if (transaction && transaction !== "-") {
  const merge = page.locator('button[value="merge"]');
  if (await merge.count()) {
    await merge.first().click();
    await page.waitForTimeout(4000);
    result.steps.push({ step: "decided", url: page.url(), text: (await page.locator("body").innerText()).slice(0, 400) });
  }
}

console.log(JSON.stringify(result, null, 2));
await browser.close();
