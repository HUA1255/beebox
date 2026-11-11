import { expect, test } from "@playwright/test";

import { AUTH, login, waitForJob, wavBuffer } from "./support";

// Every test drives the real BeeBox process started by playwright.config.ts:
// fake speech engine, fake video platform, throwaway data directory.

test.describe("BeeBox 控制台", () => {
  test("未登录只显示登录页，token 错误有提示", async ({ page }) => {
    await page.goto("/console/");

    await expect(page.getByTestId("login-page")).toBeVisible();
    await expect(page.getByTestId("jobs-table")).toHaveCount(0);

    await page.getByTestId("login-token").fill("definitely-wrong-token");
    await page.getByTestId("login-submit").click();

    await expect(page.getByTestId("login-error")).toContainText("token");
    await expect(page.getByTestId("login-page")).toBeVisible();

    await page.getByTestId("login-token").fill(
      process.env.BEEBOX_CONSOLE_SMOKE_TOKEN ?? "console-smoke-token",
    );
    await page.getByTestId("login-submit").click();
    await expect(page.getByTestId("jobs-page")).toBeVisible();
    await expect(page.getByTestId("logout-button")).toBeVisible();
  });

  test("任务列表与详情随进度更新，并显示来源和转写结果", async ({ page, request }) => {
    await page.goto("/console/");
    await login(page);

    const uploadResponse = await request.post("/v1/files", {
      headers: AUTH,
      multipart: {
        file: {
          name: "console-smoke.wav",
          mimeType: "audio/wav",
          buffer: wavBuffer(2),
        },
      },
    });
    expect(uploadResponse.status()).toBe(201);
    const uploaded = (await uploadResponse.json()) as { id: string };

    const createResponse = await request.post("/v1/jobs", {
      headers: AUTH,
      data: {
        kind: "transcription",
        source: { type: "file", file_id: uploaded.id },
        options: { language: "zh", diarization: false },
      },
    });
    expect(createResponse.status()).toBe(201);
    const created = (await createResponse.json()) as { id: string };

    // The fake engine sleeps for a few seconds, so the list's polling picks
    // the job up while it is still running. This is the live-update check:
    // neither page is reloaded between the running and succeeded assertions.
    const row = page.locator(`tr[data-job-id="${created.id}"]`);
    await expect(row).toBeVisible();
    await expect(row.getByTestId("job-status")).toHaveText(/处理中|排队中/);

    await row.getByTestId("job-link").click();
    await expect(page).toHaveURL(new RegExp(`/console/jobs/${created.id}$`));

    await expect(page.getByTestId("job-detail-status")).toHaveText(/处理中|排队中/);
    await expect(page.getByTestId("job-detail-status")).toHaveText("已完成", { timeout: 30_000 });
    await expect(page.getByTestId("job-detail-stage")).toHaveText("完成");
    await expect(page.getByTestId("job-detail-progress")).toHaveText("100%");
    await expect(page.getByTestId("job-source-type")).toHaveText("上传文件");
    await expect(page.getByTestId("job-source-title")).toHaveText("console-smoke.wav");
    await expect(page.getByTestId("job-source-text-source")).toHaveText("语音识别");
    await expect(page.getByTestId("job-source-language")).toHaveText("zh");
    await expect(page.getByTestId("job-source-duration")).toHaveText("0:02");

    await expect(page.getByTestId("job-result-preview")).toBeVisible();
    await expect(page.getByTestId("job-result-preview")).toContainText("冒烟测试转写结果");
    await expect(page.getByTestId("job-result-preview")).toContainText("第二段用于验证结果预览");
    await expect(page.getByTestId("result-paragraph").first()).toBeVisible();

    await page.getByRole("link", { name: "任务", exact: true }).first().click();
    await expect(row.getByTestId("job-status")).toHaveText("已完成");
    await expect(row.getByTestId("job-progress")).toHaveText("100%");
  });

  test("链接任务详情显示标题、作者、时长、链接与字幕来源", async ({ page, request }) => {
    // A YouTube sample: since ticket #66 every Bilibili link requires a usable
    // stored login, and this smoke server deliberately keeps its Bilibili
    // credential expired for the credentials page.
    const createResponse = await request.post("/v1/jobs", {
      headers: AUTH,
      data: {
        kind: "transcription",
        source: {
          type: "url",
          url: "https://www.youtube.com/watch?v=ManualZh44",
        },
      },
    });
    expect(createResponse.status()).toBe(201);
    const created = (await createResponse.json()) as { id: string };

    const settled = await waitForJob(request, created.id);
    expect(settled.status).toBe("succeeded");

    await page.goto(`/console/jobs/${created.id}`);
    // A client route loads only because BeeBox serves index.html for it.
    await expect(page.getByTestId("login-page")).toBeVisible();
    await login(page);
    await page.goto(`/console/jobs/${created.id}`);

    await expect(page.getByTestId("job-detail-status")).toHaveText("已完成");
    await expect(page.getByTestId("job-source-type")).toHaveText("视频链接");
    await expect(page.getByTestId("job-source-title")).toHaveText("示例视频：字幕路径");
    await expect(page.getByTestId("job-source-author")).toHaveText("BeeBox 测试频道");
    await expect(page.getByTestId("job-source-duration")).toHaveText("1:15");
    await expect(page.getByTestId("job-source-url")).toContainText(
      "https://www.youtube.com/watch?v=ManualZh44",
    );
    await expect(page.getByTestId("job-source-text-source")).toHaveText("平台人工字幕");
    await expect(page.getByTestId("job-source-subtitle-language")).toHaveText("zh-Hans");
    await expect(page.getByTestId("job-result-preview")).toBeVisible();
  });

  test("健康与队列页显示服务状态和 GPU 队列", async ({ page }) => {
    await page.goto("/console/");
    await login(page);

    await page.getByRole("link", { name: "健康与队列" }).click();
    await expect(page.getByTestId("health-page")).toBeVisible();

    await expect(page.getByTestId("health-online")).toHaveText("是");
    await expect(page.getByTestId("health-ready")).toHaveText("是");
    await expect(page.getByTestId("health-version")).toHaveText("1.0.1");
    await expect(page.getByTestId("queue-paused")).toHaveText(/运行中|已暂停/);
    await expect(page.getByTestId("queue-length")).toBeVisible();
    await expect(page.getByTestId("queue-current")).toBeVisible();
  });

  // The smoke server seeds BEEBOX_MODEL_SOURCE_DIR with whisper-base, so the
  // download is an exact byte copy with no network. A per-chunk delay keeps it
  // in flight long enough to observe progress and cancel it.
  test("模型页显示状态、校验结果与空间，下载可取消、重试与删除", async ({
    page,
    request,
  }) => {
    // Keep the test repeatable: a retry starts from a clean model directory.
    await request.delete("/v1/models/whisper-base", { headers: AUTH });

    await page.goto("/console/");
    await login(page);
    await page.getByRole("link", { name: "模型" }).click();

    await expect(page.getByTestId("models-page")).toBeVisible();
    await expect(page.getByTestId("models-table")).toBeVisible();
    const row = page.locator('tr[data-model-id="whisper-base"]');
    await expect(row).toBeVisible();
    await expect(row.getByTestId("model-downloaded")).toHaveText("未下载");
    await expect(row.getByTestId("model-verified")).toHaveText("未校验");
    await expect(row.getByTestId("model-size")).toHaveText("0 B");
    await expect(row.getByTestId("model-loaded")).toHaveText("未加载");

    // Download: the row keeps polling, so an intermediate percentage is seen
    // before the install completes.
    await row.getByTestId("model-download").click();
    await expect(row.getByTestId("model-download-state")).toHaveText("下载中");
    await expect(row.getByTestId("model-progress")).toHaveText(/^[1-9][0-9]?%$/, {
      timeout: 30_000,
    });
    await expect(row.getByTestId("model-downloaded")).toHaveText("已下载", {
      timeout: 30_000,
    });
    await expect(row.getByTestId("model-verified")).toHaveText("校验通过");
    await expect(row.getByTestId("model-size")).not.toHaveText("0 B");
    await expect(row.getByTestId("model-progress")).toHaveText("100%");
    await expect(row.getByTestId("model-download-state")).toHaveText("已完成");

    // A job through the fake engine makes the model "loaded"; unload releases
    // it back to the not-loaded state.
    const uploadResponse = await request.post("/v1/files", {
      headers: AUTH,
      multipart: {
        file: {
          name: "models-smoke.wav",
          mimeType: "audio/wav",
          buffer: wavBuffer(1),
        },
      },
    });
    expect(uploadResponse.status()).toBe(201);
    const uploaded = (await uploadResponse.json()) as { id: string };
    const jobResponse = await request.post("/v1/jobs", {
      headers: AUTH,
      data: {
        kind: "transcription",
        source: { type: "file", file_id: uploaded.id },
        options: { model: "whisper-base" },
      },
    });
    expect(jobResponse.status()).toBe(201);
    const job = (await jobResponse.json()) as { id: string };
    expect((await waitForJob(request, job.id)).status).toBe("succeeded");

    await expect(row.getByTestId("model-loaded")).toHaveText("已加载", {
      timeout: 30_000,
    });
    await row.getByTestId("model-unload").click();
    await expect(row.getByTestId("model-loaded")).toHaveText("未加载");

    // Delete removes the install and its disk usage.
    await row.getByTestId("model-delete").click();
    await expect(row.getByTestId("model-downloaded")).toHaveText("未下载");
    await expect(row.getByTestId("model-verified")).toHaveText("未校验");
    await expect(row.getByTestId("model-size")).toHaveText("0 B");

    // Cancel an active download, then retry it to completion.
    await row.getByTestId("model-download").click();
    await expect(row.getByTestId("model-download-state")).toHaveText("下载中");
    await row.getByTestId("model-cancel").click();
    await expect(row.getByTestId("model-download-state")).toHaveText("已取消", {
      timeout: 30_000,
    });
    await expect(row.getByTestId("model-downloaded")).toHaveText("未下载");
    await row.getByTestId("model-retry").click();
    await expect(row.getByTestId("model-verified")).toHaveText("校验通过", {
      timeout: 30_000,
    });
    await expect(row.getByTestId("model-downloaded")).toHaveText("已下载");

    await row.getByTestId("model-delete").click();
    await expect(row.getByTestId("model-downloaded")).toHaveText("未下载");
  });

  test("队列暂停与恢复在模型页可操作，并反映到健康页", async ({ page, request }) => {
    await request.post("/v1/queue/resume", { headers: AUTH });

    await page.goto("/console/");
    await login(page);
    await page.getByRole("link", { name: "模型" }).click();
    await expect(page.getByTestId("models-page")).toBeVisible();

    await expect(page.getByTestId("queue-paused")).toHaveText("运行中");
    await page.getByTestId("queue-pause").click();
    await expect(page.getByTestId("queue-paused")).toHaveText("已暂停");

    const paused = await request.get("/v1/health", { headers: AUTH });
    expect(paused.ok()).toBeTruthy();
    expect((await paused.json()).queue.paused).toBe(true);

    await page.getByRole("link", { name: "健康与队列" }).click();
    await expect(page.getByTestId("health-page")).toBeVisible();
    await expect(page.getByTestId("queue-paused")).toHaveText("已暂停");

    await page.getByTestId("queue-resume").click();
    await expect(page.getByTestId("queue-paused")).toHaveText("运行中");

    const resumed = await request.get("/v1/health", { headers: AUTH });
    expect((await resumed.json()).queue.paused).toBe(false);
  });
});
