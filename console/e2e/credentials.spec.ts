import { expect, test } from "@playwright/test";

import { AUTH, login } from "./support";

// Ticket #52: the credentials page completes a Bilibili QR login and accepts a
// YouTube cookie file. The smoke server seeds an expired Bilibili credential
// and scripts the fake login (pending -> scanned -> confirmed), so every state
// the console must show is reachable without a network or a real account.
//
// The sentinels below are the credential values. None of them may appear in
// the rendered page, the API responses the page reads, or the browser console.

const EXPIRED_SESSDATA = "smoke-expired-sessdata-secret-52";
const FAKE_LOGIN_SECRETS = [
  "smoke-login-sessdata-secret-52",
  "smoke-login-bili-jct-secret-52",
  "smoke-login-ac-time-secret-52",
];
const YOUTUBE_COOKIE_SECRET = "SUPER_SECRET_CONSOLE_COOKIE_52";
const YOUTUBE_COOKIE =
  "# Netscape HTTP Cookie File\n" +
  `.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\t${YOUTUBE_COOKIE_SECRET}\n`;

test.describe("控制台凭证页", () => {
  test("B 站登录失效有明显提示，扫码确认后与健康检查一致", async ({ page, request }) => {
    const consoleLines: string[] = [];
    page.on("console", (message) => consoleLines.push(message.text()));

    await page.goto("/console/");
    await login(page);
    await page.getByTestId("nav-credentials").click();

    await expect(page.getByTestId("credentials-page")).toBeVisible();

    // The seeded credential is stale: the page and /v1/health agree, and the
    // warning is prominent.
    await expect(page.getByTestId("credentials-login-status")).toHaveText("已失效");
    await expect(page.getByTestId("credentials-login-warning")).toContainText("已失效");
    const before = await request.get("/v1/health");
    expect(((await before.json()) as { bilibili_login: string }).bilibili_login).toBe(
      "expired",
    );

    // Starting shows the QR code; cancelling leaves the stored credential alone.
    await page.getByTestId("credentials-qr-start").click();
    await expect(page.getByTestId("credentials-qr-image")).toBeVisible();
    await expect(page.getByTestId("credentials-qr-image")).toHaveAttribute(
      "src",
      /^data:image\/png;base64,/,
    );
    await expect(page.getByTestId("credentials-session-status")).toHaveText(
      /等待扫码|已扫码，等待确认/,
    );
    await page.getByTestId("credentials-qr-cancel").click();
    await expect(page.getByTestId("credentials-session-status")).toHaveText("已取消");
    await expect(page.getByTestId("credentials-qr-image")).toHaveCount(0);
    const afterCancel = await request.get("/v1/health");
    expect(((await afterCancel.json()) as { bilibili_login: string }).bilibili_login).toBe(
      "expired",
    );

    // Refresh the QR code and complete the login. The page walks through
    // scanned -> confirmed and then reports the same state as health.
    await page.getByTestId("credentials-qr-start").click();
    await expect(page.getByTestId("credentials-qr-image")).toBeVisible();
    await expect(page.getByTestId("credentials-session-status")).toHaveText(
      "已扫码，等待确认",
      { timeout: 15_000 },
    );
    await expect(page.getByTestId("credentials-session-status")).toHaveText("登录成功", {
      timeout: 15_000,
    });
    await expect(page.getByTestId("credentials-login-status")).toHaveText("有效", {
      timeout: 15_000,
    });
    await expect(page.getByTestId("credentials-login-warning")).toHaveCount(0);

    const after = await request.get("/v1/health");
    expect(((await after.json()) as { bilibili_login: string }).bilibili_login).toBe("valid");
    const confirmed = await request.get("/v1/bilibili/login", { headers: AUTH });
    expect((await confirmed.json()) as object).toMatchObject({ status: "confirmed" });

    // No credential content anywhere the operator can see or copy.
    const rendered = await page.locator("body").innerText();
    for (const secret of [EXPIRED_SESSDATA, ...FAKE_LOGIN_SECRETS]) {
      expect(rendered).not.toContain(secret);
    }
    expect(consoleLines.join("\n")).not.toContain("secret-52");
  });

  test("上传 YouTube cookie 后显示配置状态，页面不回显内容", async ({ page, request }) => {
    const consoleLines: string[] = [];
    page.on("console", (message) => consoleLines.push(message.text()));

    await page.goto("/console/");
    await login(page);
    await page.getByTestId("nav-credentials").click();
    await expect(page.getByTestId("credentials-page")).toBeVisible();

    // YouTube needs no login by default; no jar is configured at startup.
    await expect(page.getByTestId("credentials-youtube-status")).toHaveText("未配置");
    await expect(page.getByTestId("credentials-youtube-size")).toHaveText("—");
    await expect(page.getByTestId("credentials-youtube-count")).toHaveText("—");
    await expect(page.getByTestId("credentials-youtube-updated")).toHaveText("—");

    await page.getByTestId("credentials-youtube-file").setInputFiles({
      name: "youtube-cookies.txt",
      mimeType: "text/plain",
      buffer: Buffer.from(YOUTUBE_COOKIE, "utf-8"),
    });
    await expect(page.getByTestId("credentials-youtube-filename")).toHaveText(
      "youtube-cookies.txt",
    );
    await page.getByTestId("credentials-youtube-upload").click();

    await expect(page.getByTestId("credentials-youtube-message")).toContainText("已保存");
    await expect(page.getByTestId("credentials-youtube-status")).toHaveText("已配置");
    await expect(page.getByTestId("credentials-youtube-size")).not.toHaveText("—");
    await expect(page.getByTestId("credentials-youtube-count")).toHaveText("1 条");
    await expect(page.getByTestId("credentials-youtube-updated")).not.toHaveText("—");

    // The API metadata matches what the page shows, and carries no content.
    const status = await request.get("/v1/admin/youtube-cookie", { headers: AUTH });
    const statusBody = (await status.json()) as {
      configured: boolean;
      size_bytes: number;
      cookie_count: number;
    };
    expect(statusBody).toMatchObject({ configured: true, cookie_count: 1 });
    expect(statusBody.size_bytes).toBe(Buffer.byteLength(YOUTUBE_COOKIE, "utf-8"));
    expect(await status.text()).not.toContain(YOUTUBE_COOKIE_SECRET);

    // An invalid file is explained in Chinese and never echoed.
    await page.getByTestId("credentials-youtube-file").setInputFiles({
      name: "not-a-cookie.txt",
      mimeType: "text/plain",
      buffer: Buffer.from(`not a cookie jar ${YOUTUBE_COOKIE_SECRET}`, "utf-8"),
    });
    await page.getByTestId("credentials-youtube-upload").click();
    await expect(page.getByTestId("credentials-youtube-error")).toContainText("Netscape");

    const rendered = await page.locator("body").innerText();
    expect(rendered).not.toContain(YOUTUBE_COOKIE_SECRET);
    expect(consoleLines.join("\n")).not.toContain(YOUTUBE_COOKIE_SECRET);
  });
});
