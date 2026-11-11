import { expect, type APIRequestContext, type Page } from "@playwright/test";

export const TOKEN = process.env.BEEBOX_CONSOLE_SMOKE_TOKEN ?? "console-smoke-token";
export const AUTH = { Authorization: `Bearer ${TOKEN}` };

/** A real 16 kHz mono PCM WAV; the backend decodes it with ffmpeg. */
export function wavBuffer(seconds = 2, sampleRate = 16_000): Buffer {
  const samples = Math.max(1, Math.round(sampleRate * seconds));
  const buffer = Buffer.alloc(44 + samples * 2);
  buffer.write("RIFF", 0, "ascii");
  buffer.writeUInt32LE(36 + samples * 2, 4);
  buffer.write("WAVE", 8, "ascii");
  buffer.write("fmt ", 12, "ascii");
  buffer.writeUInt32LE(16, 16);
  buffer.writeUInt16LE(1, 20);
  buffer.writeUInt16LE(1, 22);
  buffer.writeUInt32LE(sampleRate, 24);
  buffer.writeUInt32LE(sampleRate * 2, 28);
  buffer.writeUInt16LE(2, 32);
  buffer.writeUInt16LE(16, 34);
  buffer.write("data", 36, "ascii");
  buffer.writeUInt32LE(samples * 2, 40);
  for (let index = 0; index < samples; index += 1) {
    const sample = Math.round(1200 * Math.sin((2 * Math.PI * 440 * index) / sampleRate));
    buffer.writeInt16LE(sample, 44 + index * 2);
  }
  return buffer;
}

export async function login(page: Page): Promise<void> {
  await page.getByTestId("login-token").fill(TOKEN);
  await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("jobs-page")).toBeVisible();
}

interface JobLike {
  id: string;
  status: string;
  progress: number;
}

export async function waitForJob(
  request: APIRequestContext,
  jobId: string,
  timeoutMs = 60_000,
): Promise<JobLike> {
  const deadline = Date.now() + timeoutMs;
  let job: JobLike | null = null;
  while (Date.now() < deadline) {
    const response = await request.get(`/v1/jobs/${jobId}`, { headers: AUTH });
    expect(response.ok()).toBeTruthy();
    job = (await response.json()) as JobLike;
    if (["succeeded", "failed", "cancelled"].includes(job.status)) {
      return job;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`job ${jobId} did not settle: ${JSON.stringify(job)}`);
}
