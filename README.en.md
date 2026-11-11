<div align="center">

# BeeBox

**A local-first home speech AI service**

Runs on a Windows GPU machine at home and turns audio, video, and Bilibili / YouTube links into transcripts with timestamps and speaker labels.<br>
It exposes its capabilities only through the `/v1` HTTP API; models and data stay on the machine, with no dependency on externally hosted services.

<p><a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0-blue?style=flat-square" alt="license"></a> <img src="https://img.shields.io/badge/stack-FastAPI_·_PyTorch-0b84fe?style=flat-square" alt="stack"> <img src="https://img.shields.io/badge/version-1.0.1-blue?style=flat-square" alt="version"></p>

<p><a href="README.md">中文</a> · <a href="CHANGELOG.md">Changelog</a> · <a href="docs/adr/">Architecture decision records</a> · <a href="GLOSSARY.md">Glossary</a></p>

</div>

---

## Contents

1. [What it is and what it is not](#1-what-it-is-and-what-it-is-not)
2. [Technology choices and why](#2-technology-choices-and-why)
3. [Architecture and why](#3-architecture-and-why)
4. [Current limitations](#4-current-limitations)
5. [Future directions](#5-future-directions)
6. [Getting started](#6-getting-started)
7. [Configuration, API, and development](#7-configuration-api-and-development)

---

## 1. What it is and what it is not

BeeBox is a **single-user backend service for personal use**. Its clients are Beer's home service (home), scripts, agents, and the console that ships with this repository.

| Does | Does not |
| --- | --- |
| Speech transcription: audio / video files and single Bilibili or YouTube video links | Subtitle editing, version history, or transcript revision (revisions happen in Obsidian) |
| Character-level timestamps and optional speaker diarization | Speaker identification (deciding who a speaker really is) |
| Sentence-level segments plus paragraphs; TXT / Markdown / SRT exports | LLM processing such as proofreading, translation, summaries, or minutes (that belongs to clients) |
| Download, load, unload, and delete local models | Online ASR, multi-tenancy, or exposure to the public internet |

In one sentence: **BeeBox only turns speech into reliably timed text; everything else is left to its clients.** This boundary comes from [ADR-0008](docs/adr/0008-beebox-voice-service.md) and underlies every choice below.

---

## 2. Technology choices and why

### 2.1 Product shape: an API-only backend, not a workbench

The project's predecessor, Verbatim / asrbox, had a full web workbench with subtitle editing, AI proofreading, translation, chat, batch jobs, MCP, and more. Version 1.0 removed all of it and kept only the `/v1` API.

**Why**
- Beer, the only heavy client, uses none of the workbench features; keeping them only grows the maintenance surface and misleads the agents that help develop the project.
- LLM processing duplicated home's existing LLM pipeline, and keeping it in two places would make both harder to maintain.
- The console became "just another client" that uses only the public API. This forces the API to be sufficient on its own, with no back-door endpoints.

**Options considered**: keep extending the workbench (complete but unused), or remove only the UI and keep every backend capability (the API surface would still be huge).

### 2.2 ASR baseline: Qwen3-ASR-1.7B + ForcedAligner + pyannote

| Stage | Choice | Why | Alternatives / why not |
| --- | --- | --- | --- |
| Speech recognition | **Qwen3-ASR-1.7B** (Apache-2.0, fp16) | Most material is Chinese meetings and lectures, where Qwen3-ASR is clearly more accurate | Whisper large-v3: mature ecosystem but weaker on Chinese; FireRedASR2: strong on Chinese but had thin tooling at the time |
| Timestamps | **Qwen3-ForcedAligner-0.6B** | Qwen3-ASR does not produce precise timestamps itself; the aligner provides character-level ones for sentence splitting and subtitles | — |
| Speaker diarization | **pyannote community-1** (ModelScope mirror) | Works on Chinese meetings in testing and needs no Hugging Face token | sherpa-onnx: lighter but untested, kept as a fallback; MOSS-Transcribe-Diarize: joint diarization, but it would replace the ASR baseline |

Recognition, alignment, and diarization run **as separate stages** rather than as one end-to-end model. Each stage can be replaced, can fail, and can be measured independently; the cost is that the stages must be stitched together (see [limitations](#4-current-limitations)).

The choice is backed by measurement: the [Gate 1 report](docs/spikes/0001-gate1.md) ran the full chain on a real 82.7-minute recording, peaking at about 8.6 GB of VRAM for ASR + alignment and about 1.6 GB for pyannote, which fits a 16 GB card. See [ADR-0001](docs/adr/0001-qwen3-asr-base.md) and [ADR-0005](docs/adr/0005-pyannote-diarization.md).

### 2.3 A multi-model catalog: optional models beyond the baseline

Besides the baseline, the service registers a set of local models for different languages and speed needs:

- **Whisper family**: five sizes each on transformers and faster-whisper (CTranslate2), including Distil-Whisper (English only)
- **Chinese-first**: SenseVoice Small, Paraformer-large, Fun-ASR-Nano, FireRedASR2-AED 1.1B
- **Multilingual speech-LMs**: Qwen3-ASR 0.6B/1.7B, Granite Speech 4.1, Cohere Transcribe, ARK-ASR, Voxtral Mini
- **Joint diarization**: MOSS-Transcribe-Diarize 0.9B
- **NeMo (optional runtime)**: Parakeet TDT 0.6B v3, Canary 1B Flash

**Why not a single model**: no model is best for every language, duration, and speed. In measurements, Paraformer was the fastest model in the catalog (RTF ≈ 0.1), while Distil-Whisper only suits English and "hallucinates" English on Chinese audio. Every catalog entry **states honestly** its language coverage, timestamp / speaker capabilities, and supported devices, so clients can choose.

**Why weights are not in the repository or the installer**: weights run to several GB each and carry different licenses (Apache-2.0, MIT, CC-BY-4.0, vendor model licenses). They are downloaded on demand from ModelScope / Hugging Face, with attribution maintained in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). See the [model guide](docs/models.md) and the [model roadmap](docs/asr-models-roadmap.md).

### 2.4 Deployment: a native Windows service, not Docker

| Choice | Why |
| --- | --- |
| Native Windows 11 + a **uv**-locked venv | The GPU machine runs Windows anyway; GPU containers on Windows go through WSL, and maintaining an unused container setup would mean verifying every change in two environments |
| Hosted as a system service by **WinSW** | Starts at boot and restarts 5 seconds after a crash; the previous "scheduled task + PowerShell wrapper" did not recover from crashes |
| Deployed by **git tag** | Production code cannot silently drift from main, and rolling back means switching tags |
| Console build output `console/dist` committed to the repository | The production machine needs no Node; there is a single port, and CI checks the build output matches the source |

Development happens on the same machine, so **the dev and production instances are strictly isolated**: separate directories, ports (17495 vs 17494), data, and tokens, with the model directory shared read-only. See [ADR-0010](docs/adr/0010-windows-native-deployment.md).

### 2.5 Stack

| Layer | Choice | Why |
| --- | --- | --- |
| Web framework | FastAPI + Uvicorn | Generates OpenAPI natively, so the contract can become a snapshot test; Pydantic handles validation uniformly |
| Storage | SQLite (SQLAlchemy) + a local data directory | Single user, single machine; no separate database process needed, and backup is copying a directory |
| Inference | PyTorch / transformers / CTranslate2 / FunASR | Covers the official inference paths of every model in the catalog |
| Media | ffmpeg / ffprobe | The de facto standard with the broadest format coverage |
| Video links | yt-dlp + bgutil (YouTube PO tokens) + bilibili-api (QR login) | Gets platform subtitles and high-quality audio tracks; versions pinned |
| Dependency management | uv + `uv.lock` | Reproducible and fast; production and development share one lock |
| Console | React + Vite + TypeScript, Playwright end-to-end tests | A lightweight SPA that reads only the public API |

### 2.6 Security and data: a LAN token, media deleted after use

- BeeBox listens on a LAN / Tailscale address for home, so **it refuses to start on a non-loopback address without a Bearer token**; tokens in URL query parameters are not accepted.
- Browsers or players that cannot send headers download through 60-second, single-use, path-bound tickets.
- **Source media and temporary audio are deleted as soon as a job succeeds**; results are kept for 30 days by default. home already stores originals on the NAS and results in Obsidian, so BeeBox does not provide long-term storage.
- Download credentials (Bilibili and YouTube cookies) are **write-only** for clients: the API never echoes them, logs never record them, and they never enter the database.

See [ADR-0011](docs/adr/0011-lan-token-and-retention.md) and [Privacy and local data](docs/privacy.md).

---

## 3. Architecture and why

### 3.1 Layered overview

```mermaid
flowchart TB
    subgraph Clients[Clients]
        home[Beer · home service]
        script[Scripts / agents]
        console[Console /console]
    end

    subgraph API["/v1 API layer (backend/v1)"]
        files["/v1/files"]
        jobs["/v1/jobs"]
        models["/v1/models"]
        queue["/v1/queue · /v1/health"]
    end

    subgraph Services[Service layer (backend/services)]
        tasks[Job lifecycle: tasks]
        video[Video links: video_jobs]
        gpuq[GPU queue: gpu_queue<br/>FIFO · concurrency 1]
        pipeline[Media → chunks → merge → postprocess<br/>→ segments → paragraphs → exports]
        mgr[Model management: models]
        ret[Retention cleanup: retention]
    end

    subgraph Engine[SpeechEngine boundary]
        local[local: real models]
        fake[fake: test double]
    end

    worker[Inference subprocess<br/>local_task_worker<br/>heartbeat + unload]
    backends[ASR backends · aligner · pyannote<br/>backend/backends]
    store[(SQLite + data directory<br/>uploads / derived audio / exports)]

    Clients -->|HTTP + Bearer token| API
    API --> Services
    tasks --> gpuq --> pipeline
    video -->|platform subtitles: no GPU| pipeline
    video -->|no subtitles: audio only| gpuq
    pipeline --> Engine
    local --> worker --> backends
    Services --> store
```

### 3.2 The path of one transcription job

```text
POST /v1/files (streaming upload)
  └─ POST /v1/jobs  → status queued, enters the GPU queue
       └─ preprocessing   ffprobe preflight + ffmpeg conversion to a uniform WAV
       └─ waiting_model   download / load the model when needed
       └─ transcribing    long audio is cut into chunks (120 s window, 2 s overlap),
                          inferred chunk by chunk, with progress stored
                          → forced alignment yields character-level timestamps
                          → cross-chunk merge and de-duplication
       └─ diarize         (optional) pyannote diarization, then speakers assigned to text
       └─ postprocess     sentence-level segments (sentence-final punctuation > pause > length cap)
                          → same-speaker segments grouped into paragraphs
       └─ exporting       TXT / Markdown by paragraph, SRT by segment
  └─ succeeded: source media and temporary audio deleted, result kept for 30 days
```

A video-link job decides its path before queueing: if the platform has a usable manual or automatic subtitle track, it is converted directly into the same segments and paragraphs and **never touches the GPU**; only when no usable subtitle exists, or the client sends `prefer=asr`, is the audio track downloaded and run through the ASR flow above.

### 3.3 Key design decisions and why

**① All capabilities share one job resource and one GPU queue** ([ADR-0009](docs/adr/0009-unified-job-api.md))

- The machine has a single 16 GB GPU. Two processes loading models on their own would OOM each other, so **one process must queue all work and own all model loading and unloading**.
- The queue is strictly first-in-first-out with a concurrency of one and can be paused: the current job finishes, new jobs keep queueing, and resuming continues where it left off. Deployments and real-model tests pause the production queue first.
- Externally there is only `/v1/jobs` (capabilities distinguished by `kind`) and `/v1/files`. Adding TTS later is just another `kind`; jobs, progress, files, auth, and model management are reused, and clients write one polling loop.
- There are only five public states: `queued / running / succeeded / failed / cancelled`. Details live in `stage`, `progress`, `error_code`, and `retryable`. Errors share the shape `{detail, error_code, stage}`. The client's state machine stays simple while internal stages are free to evolve.

**② SpeechEngine: the only boundary between the pipeline and the models**

The pipeline never imports a model runtime directly; it builds a `SpeechRequest` and consumes a `TranscriptionResult`. Behind the boundary are two implementations:

- `local`: the real ASR backends, aligner, and pyannote;
- `fake`: a scripted stand-in that returns preset segments, speakers, or failures.

**Why**: the genuinely complex logic (chunking, merging, postprocessing, paragraphs, exports) can be tested with real code on CI **without a GPU, models, or network**. That is how the default test suite runs; real-model tests run separately.

**③ Inference runs in a separate subprocess with a liveness heartbeat**

- Model crashes, VRAM leaks, and CUDA deadlocks cannot take down the API process; the process exits when the job ends, so VRAM is always fully released.
- A live process is not a working process: a deadlocked inference keeps running. The worker reports its accumulated CPU time as a heartbeat, and is judged stalled and terminated only when the heartbeat freezes or a hard limit passes with no output. Slow chunks are not killed by mistake, and wedged ones do not hold the queue forever.

**④ Chunking with per-chunk persistence enables resume**

Long recordings are cut into 120-second chunks with 2 seconds of overlap, and each chunk's result and state are written to the database. After a restart or crash, a job continues from the last finished chunk, and queued jobs are restored. For an 80-minute recording, that means re-running at most two minutes instead of starting over.

**⑤ Two levels of text structure: segments and paragraphs** ([ADR-0007](docs/adr/0007-paragraph-contract.md))

- A **segment** is close to one sentence, belongs to one speaker, and is the unit of subtitles and timing (one SRT cue per segment).
- A **paragraph** groups several segments from the same speaker, targets 10–120 seconds, and is the unit of reading and export (TXT / Markdown render by paragraph).

**Why two levels**: subtitles need to be short and reading needs to be long. With only one level, either a subtitle cue spans several lines or the reading transcript breaks into one sentence per line.

**⑥ The contract is a snapshot**

`openapi.json` at the repository root is a committed snapshot of the API, and `backend/tests/test_openapi_snapshot.py` fails whenever the implementation and snapshot differ. Compatibility is guarded by a test, not by documentation discipline. Changing a route or model means explicitly regenerating and committing the snapshot, so the diff is obvious in review.

**⑦ Polling only, no webhooks**

Clients poll `GET /v1/jobs/{id}` as the standard method, with SSE as an option. Polling is enough for a single user, and webhooks would bring callback addresses, retries, and auth, which is not worth the complexity.

### 3.4 Repository layout

```text
backend/
  v1/                  /v1 routes, request/response schemas, error codes
  services/            job lifecycle, GPU queue, media and chunking, merging, postprocessing,
                       segments/paragraphs, exports, model management, video platforms,
                       Bilibili login, retention cleanup
  backends/            model registry (registry) and local ASR engine dispatch (local_asr)
  database/            SQLite models and migrations
  vendor/fireredasr2/  minimal FireRedASR2 inference code (upstream Apache-2.0)
  tests/               default tests (fake engine, fake platforms, no GPU or network)
  real_tests/          real-model / real-platform tests and recorded results
console/               console source (React) and committed build output dist/
deploy/windows/        install, deploy, and dev-instance scripts plus the WinSW template
docs/                  ADRs, model guide, privacy, CI, roadmap, spike reports
openapi.json           API contract snapshot
```

---

## 4. Current limitations

### Runtime and deployment
- **Only one supported shape: Windows 11 + an NVIDIA GPU.** macOS / Linux work for development and some model verification but are not supported production environments.
- **One GPU, one queue, concurrency of one.** There are no priorities, so a long job blocks every GPU job behind it. Only subtitle-based video jobs bypass the queue.
- **One user, one token.** No multiple users, permission levels, or quotas; not suitable for public exposure.
- **Polling only**: no webhooks, and SSE is optional.

### Recognition quality
- **Diarization stitching issues remain:** pyannote tends to over-split short interjections and hosts (one person split into several labels), speaker changes can cut a sentence in two, and overlapping speech may be attributed incorrectly.
- **General forced alignment is not done yet:** models without native timestamps (such as Fun-ASR-Nano, Cohere Transcribe, and base Granite Speech 4.1) only get segment-level timing or none at all, and cannot be uniformly upgraded to character level.
- **No real-time / streaming transcription**; only offline file processing.
- **Accuracy and speed findings hold only for the verified machines and dependency versions**; other hardware or upstream upgrades may behave differently.
- No speaker identification; speakers are anonymous numbers only.

### Models
- **NeMo models (Parakeet, Canary) have passed only unit and contract tests and have not been verified on real CUDA hardware**, and `nemo_toolkit` is not in the default dependencies.
- FireRedASR2 accepts at most 60 seconds per input and relies on VAD splitting; Distil-Whisper supports English only.
- Some model sources (such as gated Hugging Face models) need extra authorization or a mirror fallback.

### Video platforms
- **Bilibili login needs a fresh QR scan roughly every 180 days.** Credentials from the TV channel cannot be renewed through the web API, so BeeBox only detects expiry and home sends an email reminder ([ADR-0012](docs/adr/0012-bilibili-login-refresh.md)).
- Bilibili and YouTube integrations rely on undocumented APIs and can break when the platforms change; age-restricted YouTube videos need an uploaded cookie.

### Data and results
- **Transcripts cannot be changed once produced**; the server has no revision API.
- **Source media is deleted after success**: if a failed job's source media has already been cleaned up, it must be resubmitted.
- Exports are limited to TXT / Markdown / SRT; there is no DOCX or VTT.

### Licensing and engineering
- `bilibili-api-python` and bgutil are **GPL-3.0**, so the whole project is released under GPL-3.0 to stay compatible with them.
- `services/tasks.py` (about 2,500 lines) and `backends/local_asr.py` (about 1,700 lines) are large and are the main refactoring targets.

---

## 5. Future directions

Roughly in priority order. Plans in progress are tracked in GitHub Issues; model expansion is covered in the [model roadmap](docs/asr-models-roadmap.md).

### Near term: close quality gaps
- **General forced-alignment postprocessing**: after any model without timestamps finishes, optionally run alignment to upgrade the result to segments with a character-level timeline. Alignment failure does not affect the success of the base transcription. One investment that benefits every model.
- **Diarization postprocessing**: merge short turns, stabilize speaker labels, mark overlapping speech, and smooth sentence boundaries at speaker changes.
- **Real CUDA verification of the NeMo models**, to decide whether Parakeet / Canary join the official catalog.

### Mid term: new capabilities on the same skeleton
- **Text-to-speech (TTS)**: added as a new `kind` under `/v1/jobs`, sharing files, jobs, the GPU queue, and model management with transcription (the original intent of ADR-0009). A separate service is considered only if dependencies truly cannot coexist.
- **A thin MCP layer**: when needed, a thin MCP adapter on top of `/v1`, without reimplementing capabilities inside the service.
- **More candidate models**: Granite Speech 5 (CTC), granite-speech-4.1-2b-nar (high throughput), Canary-1B-v2, Voxtral Small 24B, and others, each evaluated on honest capability claims, license compliance, and manageable size.

### Long term: only if needed
- Queue priorities or preemption (for example, letting short jobs jump ahead), as long as the single-GPU VRAM constraint still holds.
- Streaming / near-real-time transcription.
- Splitting `tasks.py` and `local_asr.py` so each pipeline stage is easier to test and replace on its own.

**Will not do**: workbench-style subtitle editing, LLM proofreading / translation / summaries, or speaker identification. These belong to clients; see [ADR-0008](docs/adr/0008-beebox-voice-service.md).

---

## 6. Getting started

**Requirements**: Windows 11 x64, an NVIDIA GPU (CUDA-compatible), Python 3.12 (64-bit) with [uv](https://docs.astral.sh/uv/), and `ffmpeg` / `ffprobe` (on `PATH` or in the service tools directory).

**Production install** (WinSW service, starts at boot, restarts after crashes); see [deploy/windows/README.md](deploy/windows/README.md):

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1 -ModelRoot <existing model storage root>
```

**Development or ad-hoc run**:

```powershell
uv sync --frozen
$env:BEEBOX_DATA_DIR = "D:\beebox-data"
uv run python -m backend.server --host 127.0.0.1 --port 17494
curl.exe http://127.0.0.1:17494/v1/health
```

For a dev instance, use `deploy\windows\dev-instance.ps1` (port 17495, isolated from production); see [AGENTS.md](AGENTS.md).

---

## 7. Configuration, API, and development

### Environment variables

| Variable | Description |
| --- | --- |
| `BEEBOX_DATA_DIR` | Data directory: database, uploads, derived audio, exports, and caches |
| `BEEBOX_API_TOKEN` | Bearer token; required when listening on a non-loopback address, and once set every `/v1` endpoint except health requires it |
| `BEEBOX_MODEL_STORAGE_ROOT` | Storage root for models and framework caches |
| `BEEBOX_RETENTION_DAYS` | Days to keep results, 30 by default |
| `BEEBOX_DOWNLOAD_PROXY` | HTTP(S) proxy for video metadata, subtitle, and audio downloads; direct connection if unset, and the proxy address is never written to results or logs |

See `.env.example` for an example. The service does **not** read `.env` automatically; export the variables into the environment before starting.

### Common endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/health` | Liveness, readiness, version, GPU, queue, and Bilibili login status |
| `POST /v1/files` | Streaming upload; returns the file id and metadata |
| `POST /v1/jobs` | Create a job from a file or a video link |
| `GET /v1/jobs` · `GET /v1/jobs/{id}` | Job list / state, stage, and progress |
| `GET /v1/jobs/{id}/result` | Structured result (full text, segments, paragraphs, speakers, source) |
| `GET /v1/jobs/{id}/exports/{fmt}` | Export as `txt` / `md` / `srt` |
| `GET /v1/models` | Local model status, download, unload, and delete |
| `GET /v1/queue` | GPU queue status; `/pause` and `/resume` control the queue |

The full contract is in [`openapi.json`](openapi.json). After changing routes or models, regenerate and commit it:

```powershell
uv run python scripts/export_openapi.py
```

### Tests

```powershell
uv run pytest backend/tests -q          # default tests: fake engine and platforms, no GPU, models, or network
cd console; npm ci; npm run build       # after changing console source, rebuild and commit console/dist
```

Real-model tests require your own lawfully obtained media and pre-downloaded models, and are not part of the default suite.

### Further reading

- [AGENTS.md](AGENTS.md): development conventions and workflow (the single entry point)
- [GLOSSARY.md](GLOSSARY.md): domain terms
- [docs/adr/](docs/adr/): architecture decision records (currently active: 0007–0013)
- [docs/models.md](docs/models.md): model guide and verification records
- [docs/privacy.md](docs/privacy.md): privacy and local data

## License

The source code is licensed under [GPL-3.0](LICENSE) (GPL-3.0-only), copyright Feng. The project started from the MIT-licensed asrbox 0.4.0 and has since been largely rewritten; upstream attribution is in [NOTICE.md](NOTICE.md). FFmpeg, models, and other third-party components are governed by their own licenses; model weights are not distributed with the repository.
