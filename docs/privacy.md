# Privacy and Local Data

BeeBox is a local-first voice service. It runs on your own Windows machine and
processes media with local models; it does not send media or transcripts to an
external ASR or LLM service.

## Local Processing

Model inference, transcript generation, and export rendering run locally.
Model downloads still contact Hugging Face or ModelScope. Dependency
installation and links opened by the user have their own network behavior.

The service does not edit, proofread, or translate transcripts. A transcript is
produced once by the transcription pipeline and is only read or exported
afterwards.

## Data Root

The service uses `BEEBOX_DATA_DIR` as its data root (a development backend
defaults to the repository's `data/` directory).

| Location | Contents |
| --- | --- |
| `beebox.db` | Tasks, transcript segments, settings, and diagnostics metadata |
| `models/<model-name>/` | Downloaded model files and markers |
| `uploads/` | Media managed by transcription tasks (location configurable) |
| `derived-audio/` | Normalized audio and chunk files owned by individual tasks (location configurable) |
| `audio/` | Extracted or normalized audio |
| `cache/` | Intermediate task data |
| `exports/` | Backend exports and generated files |

Media ingestion defaults to referencing imported media at its original location
instead of copying it, so those tasks point at files outside the data root.
BeeBox never writes into a referenced file's folder and never deletes, moves, or
renames a referenced file; normalized audio and chunks for such tasks stay under
the configurable `derived-audio/` location. Copy mode stores media under
`uploads/`. Uploads made through HTTP are always copied into managed storage.

Model weights and framework caches can instead use a separately configured
model storage root. The configured path remains recorded if that storage
disconnects; BeeBox does not silently recreate it on the system disk.

## Local API Boundary

The service binds to `127.0.0.1` by default. Setting `BEEBOX_API_TOKEN` makes
every `/v1` endpoint except `/v1/health` require a Bearer token, and a
non-loopback bind is refused at startup unless the token is configured. The
token reduces access from unrelated local software; it is not user
authentication, disk encryption, or protection from software running as the
same Windows user.

API, export, and event-stream requests send the token in the
`Authorization` header. The `/v1` surface rejects tokens in the query string,
and credential parameters are removed from the request scope before normal
access logging.

Downloads use a short-lived ticket from `POST /v1/resource-tickets` when a
client cannot attach an `Authorization` header. It is scoped to one path and
method, expires after 60 seconds and is accepted exactly once.

A manually started development backend is unauthenticated unless
`BEEBOX_API_TOKEN` is set.

## Diagnostics

The service records task diagnostics and stage logs in the data directory for
troubleshooting. They can contain local paths, model ids, and sanitized error
excerpts, but never media bytes or transcript payloads. Diagnostics are not
exposed through `/v1`; read them from the local database or log files.

Inspect logs before sharing them. Remove private filenames, transcript
content, media, tokens, and internal endpoints. Use a private security report
for sensitive material.

## Deleting Tasks and Models

Deleting a `/v1` job removes the job's result, exports, and now-unreferenced
source file. A file another job still references is kept. Deleting a task that
references an original file never deletes that original; only its managed
derived audio, chunks, and database records are removed. Exported files saved
outside the data root are not automatically removed.

Deleting a model removes its directory under `models/<model-name>/`. Cancelling
a download does not delete reusable partial files; retry can reuse them.

Confirm no task or model download is active before deleting data.

## Complete Uninstall

Removing the source directory alone does not remove models, tasks, or exports.

1. Export any transcripts you want to keep.
2. Stop the BeeBox service and confirm its backend is no longer running.
3. Remove the application and virtual-environment directories, and the Windows
   service registration if one exists.
4. Remove the data root (`BEEBOX_DATA_DIR`) if you want to delete tasks,
   settings, and models.
5. Remove the configured model storage root and any exported files you no longer
   need.

These deletions are irreversible.

## Reporting Privacy or Security Issues

Do not place credentials, private media, full diagnostic archives, or sensitive
transcripts in a public issue. Report security-sensitive material privately to
the maintainer instead.
