import { expect, test } from "@playwright/test";

import { AUTH, login, wavBuffer } from "./support";

// Ticket #50: the console submits jobs and operates on them. Every test drives
// the real BeeBox process from playwright.config.ts (fake speech engine, fake
// video platform, throwaway data directory) through public /v1 only.

test.describe("控制台提交与处理任务", () => {
  test("粘贴视频链接提交任务并跳转到详情", async ({ page }) => {
    await page.goto("/console/");
    await login(page);
    await page.getByTestId("nav-submit").click();

    await page.getByTestId("submit-mode-url").click();
    // A YouTube sample: since ticket #66 every Bilibili link needs a usable
    // stored login, and the smoke server keeps its Bilibili credential expired
    // on purpose (the credentials spec checks that warning).
    await page.getByTestId("submit-url").fill("https://www.youtube.com/watch?v=ManualZh44");
    await page.getByTestId("submit-subtitle-language").fill("zh-Hans");
    await page.getByTestId("submit-button").click();

    await expect(page).toHaveURL(/\/console\/jobs\/[0-9a-f-]{36}$/);
    await expect(page.getByTestId("job-source-type")).toHaveText("视频链接");
    await expect(page.getByTestId("job-source-title")).toHaveText("示例视频：字幕路径");
    await expect(page.getByTestId("job-source-subtitle-language")).toHaveText("zh-Hans");
    await expect(page.getByTestId("job-source-text-source")).toHaveText("平台人工字幕");
    await expect(page.getByTestId("job-detail-status")).toHaveText("已完成", {
      timeout: 30_000,
    });
  });

  test("排队中或处理中的任务可以取消", async ({ page }) => {
    await page.goto("/console/");
    await login(page);
    await page.getByTestId("nav-submit").click();

    await page.getByTestId("submit-file-input").setInputFiles({
      name: "取消候选.wav",
      mimeType: "audio/wav",
      buffer: wavBuffer(2),
    });
    await page.getByTestId("submit-button").click();
    await expect(page).toHaveURL(/\/console\/jobs\/[0-9a-f-]{36}$/);

    // The fake engine keeps the job active for several seconds, so the
    // queued/running action set is observable before it completes.
    await expect(page.getByTestId("job-action-cancel")).toBeEnabled();
    await expect(page.getByTestId("job-action-retry")).toBeDisabled();
    await expect(page.getByTestId("job-action-delete")).toBeDisabled();

    await page.getByTestId("job-action-cancel").click();
    await expect(page.getByTestId("job-confirm-cancel")).toBeVisible();
    await page.getByTestId("job-confirm-accept").click();

    await expect(page.getByTestId("job-action-message")).toContainText("已取消任务");
    await expect(page.getByTestId("job-detail-status")).toHaveText("已取消", {
      timeout: 15_000,
    });

    await expect(page.getByTestId("job-action-cancel")).toBeDisabled();
    await expect(page.getByTestId("job-action-retry")).toBeDisabled();
    await expect(page.getByTestId("job-action-delete")).toBeEnabled();
  });

  test("失败任务显示中文错误码含义，可重试并确认删除", async ({ page, request }) => {
    await page.goto("/console/");
    await login(page);
    await page.getByTestId("nav-submit").click();

    // The fake platform registers this video as login-required: the job fails
    // with VIDEO_LOGIN_REQUIRED, which /v1 marks retryable.
    await page.getByTestId("submit-mode-url").click();
    await page.getByTestId("submit-url").fill("https://www.bilibili.com/video/BV1LoginOnly");
    await page.getByTestId("submit-prefer").check();
    await page.getByTestId("submit-button").click();
    await expect(page).toHaveURL(/\/console\/jobs\/[0-9a-f-]{36}$/);
    const jobId = page.url().split("/").pop() as string;

    await expect(page.getByTestId("job-detail-status")).toHaveText("失败", { timeout: 30_000 });
    await expect(page.getByTestId("job-error-message")).toContainText("扫码登录");
    await expect(page.getByTestId("job-error-code")).toHaveText("VIDEO_LOGIN_REQUIRED");
    await expect(page.getByTestId("job-option-prefer")).toHaveText("强制语音识别");

    await expect(page.getByTestId("job-action-cancel")).toBeDisabled();
    await expect(page.getByTestId("job-action-retry")).toBeEnabled();
    await expect(page.getByTestId("job-action-delete")).toBeEnabled();

    const before = await request.get(`/v1/jobs/${jobId}`, { headers: AUTH });
    expect(before.ok()).toBeTruthy();
    const beforeBody = (await before.json()) as { updated_at: string };

    await page.getByTestId("job-action-retry").click();
    await expect(page.getByTestId("job-action-message")).toContainText("已重新提交");

    // The retry really re-ran the job: the row was updated and it settled back
    // to the same permanent failure.
    await expect
      .poll(
        async () => {
          const response = await request.get(`/v1/jobs/${jobId}`, { headers: AUTH });
          const body = (await response.json()) as { updated_at: string; status: string };
          return { updatedAt: body.updated_at, status: body.status };
        },
        { timeout: 20_000 },
      )
      .toMatchObject({ status: "failed" });
    const after = await request.get(`/v1/jobs/${jobId}`, { headers: AUTH });
    const afterBody = (await after.json()) as { updated_at: string };
    expect(afterBody.updated_at).not.toBe(beforeBody.updated_at);
    await expect(page.getByTestId("job-detail-status")).toHaveText("失败");

    await page.getByTestId("job-action-delete").click();
    await expect(page.getByTestId("job-confirm-delete")).toBeVisible();
    await page.getByTestId("job-confirm-accept").click();

    await expect(page).toHaveURL(/\/console\/jobs$/);
    await expect
      .poll(async () => (await request.get(`/v1/jobs/${jobId}`, { headers: AUTH })).status())
      .toBe(404);
  });

  test("提交失败时用中文说明错误码，不创建任务", async ({ page, request }) => {
    await page.goto("/console/");
    await login(page);
    await page.getByTestId("nav-submit").click();

    await page.getByTestId("submit-file-input").setInputFiles({
      name: "模型不存在.wav",
      mimeType: "audio/wav",
      buffer: wavBuffer(1),
    });
    await page.getByTestId("submit-model").fill("no-such-model");
    await page.getByTestId("submit-button").click();

    await expect(page.getByTestId("submit-error")).toHaveText(
      "所选模型不在模型注册表中。（MODEL_NOT_FOUND）",
    );
    await expect(page).toHaveURL(/\/console\/submit$/);

    const jobs = await request.get("/v1/jobs?limit=100", { headers: AUTH });
    const body = (await jobs.json()) as { items: { source: { filename: string | null } }[] };
    expect(body.items.filter((job) => job.source.filename === "模型不存在.wav")).toHaveLength(0);
  });

  test("上传文件提交任务并跳转到详情", async ({ page }) => {
    await page.goto("/console/");
    await login(page);

    await page.getByTestId("nav-submit").click();
    await expect(page.getByTestId("submit-page")).toBeVisible();
    await expect(page).toHaveURL(/\/console\/submit$/);

    await page.getByTestId("submit-file-input").setInputFiles({
      name: "键盘上传.wav",
      mimeType: "audio/wav",
      buffer: wavBuffer(2),
    });
    await expect(page.getByTestId("submit-file-name")).toHaveText("键盘上传.wav");

    await page.getByTestId("submit-language").fill("zh");
    await page.getByTestId("submit-diarization").selectOption("off");
    await page.getByTestId("submit-num-speakers").fill("2");
    await page.getByTestId("submit-button").click();

    await expect(page).toHaveURL(/\/console\/jobs\/[0-9a-f-]{36}$/);
    await expect(page.getByTestId("job-source-type")).toHaveText("上传文件");
    await expect(page.getByTestId("job-source-title")).toHaveText("键盘上传.wav");
    await expect(page.getByTestId("job-source-language")).toHaveText("zh");
    await expect(page.getByTestId("job-option-diarization")).toHaveText("关闭");
    await expect(page.getByTestId("job-option-num-speakers")).toHaveText("2");
    await expect(page.getByTestId("job-detail-status")).toHaveText("已完成", {
      timeout: 30_000,
    });
  });
});
