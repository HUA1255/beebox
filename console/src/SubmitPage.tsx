import { type DragEvent, type FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";

import { createJob, uploadFile } from "./api";
import { describeError } from "./errors";
import type { JobOptionsInput } from "./types";

type SubmitMode = "file" | "url";
type DiarizationChoice = "default" | "on" | "off";

// Ticket #50: submit a transcription job from the browser. Uploads go through
// POST /v1/files before POST /v1/jobs; links go straight to POST /v1/jobs.
// Every transcription option is optional: an omitted field means "use the
// server default", so the request body only carries what the operator set.
export function SubmitPage() {
  const navigate = useNavigate();

  const [mode, setMode] = useState<SubmitMode>("file");
  const [file, setFile] = useState<File | null>(null);
  const [url, setUrl] = useState("");
  const [dragActive, setDragActive] = useState(false);

  const [language, setLanguage] = useState("");
  const [diarization, setDiarization] = useState<DiarizationChoice>("default");
  const [numSpeakers, setNumSpeakers] = useState("");
  const [model, setModel] = useState("");
  const [subtitleLanguage, setSubtitleLanguage] = useState("");
  const [preferAsr, setPreferAsr] = useState(false);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function selectFile(candidate: File | null) {
    setFile(candidate);
    setError(null);
  }

  function handleDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setDragActive(false);
    const dropped = event.dataTransfer.files?.[0] ?? null;
    if (dropped) {
      selectFile(dropped);
    }
  }

  function buildOptions(): JobOptionsInput | undefined {
    const options: JobOptionsInput = {};
    if (language.trim()) {
      options.language = language.trim();
    }
    if (diarization !== "default") {
      options.diarization = diarization === "on";
    }
    if (numSpeakers.trim()) {
      options.num_speakers = Number(numSpeakers.trim());
    }
    if (model.trim()) {
      options.model = model.trim();
    }
    if (mode === "url") {
      if (subtitleLanguage.trim()) {
        options.subtitle_language = subtitleLanguage.trim();
      }
      if (preferAsr) {
        options.prefer = "asr";
      }
    }
    return Object.keys(options).length > 0 ? options : undefined;
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    if (mode === "file" && !file) {
      setError("请选择要上传的音视频文件。");
      return;
    }
    if (mode === "url" && !url.trim()) {
      setError("请粘贴视频链接。");
      return;
    }
    if (numSpeakers.trim()) {
      const value = Number(numSpeakers.trim());
      if (!Number.isInteger(value) || value < 1 || value > 10) {
        setError("说话人数需为 1 到 10 之间的整数。");
        return;
      }
    }

    setBusy(true);
    try {
      const options = buildOptions();
      if (mode === "file" && file) {
        const uploaded = await uploadFile(file);
        const job = await createJob({
          kind: "transcription",
          source: { type: "file", file_id: uploaded.id },
          options,
        });
        navigate(`/jobs/${job.id}`);
        return;
      }
      const job = await createJob({
        kind: "transcription",
        source: { type: "url", url: url.trim() },
        options,
      });
      navigate(`/jobs/${job.id}`);
    } catch (failure) {
      setError(describeError(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="page" data-testid="submit-page">
      <div className="page-heading">
        <div>
          <h1>提交任务</h1>
          <p className="muted">上传音视频文件，或粘贴 Bilibili / YouTube 视频链接。</p>
        </div>
      </div>

      <form className="submit-form" onSubmit={handleSubmit}>
        <article className="card">
          <h2>来源</h2>
          <div className="mode-switch" role="group" aria-label="任务来源">
            <button
              type="button"
              className={mode === "file" ? "button mode active" : "button mode"}
              data-testid="submit-mode-file"
              aria-pressed={mode === "file"}
              onClick={() => setMode("file")}
            >
              上传文件
            </button>
            <button
              type="button"
              className={mode === "url" ? "button mode active" : "button mode"}
              data-testid="submit-mode-url"
              aria-pressed={mode === "url"}
              onClick={() => setMode("url")}
            >
              视频链接
            </button>
          </div>

          {mode === "file" ? (
            <>
              <label
                className={dragActive ? "drop-zone drag-active" : "drop-zone"}
                data-testid="submit-drop-zone"
                onDragOver={(event) => {
                  event.preventDefault();
                  setDragActive(true);
                }}
                onDragLeave={() => setDragActive(false)}
                onDrop={handleDrop}
              >
                <input
                  className="file-input-hidden"
                  type="file"
                  accept="audio/*,video/*"
                  data-testid="submit-file-input"
                  onChange={(event) => selectFile(event.target.files?.[0] ?? null)}
                />
                <span className="drop-title">选择文件，或拖拽到这里</span>
                <span className="muted small">支持常见音频与视频格式。</span>
              </label>
              {file ? (
                <p className="muted" data-testid="submit-file-name">
                  {file.name}
                </p>
              ) : null}
            </>
          ) : (
            <label className="field">
              <span>视频链接</span>
              <input
                data-testid="submit-url"
                name="url"
                type="url"
                placeholder="https://www.bilibili.com/video/BV..."
                value={url}
                onChange={(event) => setUrl(event.target.value)}
              />
            </label>
          )}
        </article>

        <article className="card">
          <h2>转写参数（可选，留空使用服务端默认）</h2>
          <div className="option-grid">
            <label className="field">
              <span>语言</span>
              <input
                data-testid="submit-language"
                name="language"
                placeholder="如 zh、en；留空自动识别"
                value={language}
                onChange={(event) => setLanguage(event.target.value)}
              />
            </label>
            <label className="field">
              <span>说话人分离</span>
              <select
                data-testid="submit-diarization"
                name="diarization"
                value={diarization}
                onChange={(event) => setDiarization(event.target.value as DiarizationChoice)}
              >
                <option value="default">服务端默认</option>
                <option value="on">开启</option>
                <option value="off">关闭</option>
              </select>
            </label>
            <label className="field">
              <span>说话人数</span>
              <input
                data-testid="submit-num-speakers"
                name="num_speakers"
                type="number"
                min={1}
                max={10}
                placeholder="1–10；留空自动判断"
                value={numSpeakers}
                onChange={(event) => setNumSpeakers(event.target.value)}
              />
            </label>
            <label className="field">
              <span>模型</span>
              <input
                data-testid="submit-model"
                name="model"
                placeholder="留空使用服务端默认模型"
                value={model}
                onChange={(event) => setModel(event.target.value)}
              />
            </label>
            {mode === "url" ? (
              <>
                <label className="field">
                  <span>字幕语言</span>
                  <input
                    data-testid="submit-subtitle-language"
                    name="subtitle_language"
                    placeholder="如 zh-Hans；留空自动选择"
                    value={subtitleLanguage}
                    onChange={(event) => setSubtitleLanguage(event.target.value)}
                  />
                </label>
                <label className="checkbox-field">
                  <input
                    data-testid="submit-prefer"
                    name="prefer"
                    type="checkbox"
                    checked={preferAsr}
                    onChange={(event) => setPreferAsr(event.target.checked)}
                  />
                  <span>忽略平台字幕，强制语音识别</span>
                </label>
              </>
            ) : null}
          </div>
          <p className="muted small">
            字幕语言与强制语音识别只对视频链接任务生效；上传文件只使用语音识别。
          </p>
        </article>

        {error ? (
          <p className="alert" role="alert" data-testid="submit-error">
            {error}
          </p>
        ) : null}

        <div className="form-actions">
          <button
            type="submit"
            className="button primary"
            data-testid="submit-button"
            disabled={busy}
          >
            {busy ? "提交中…" : "提交任务"}
          </button>
        </div>
      </form>
    </section>
  );
}
