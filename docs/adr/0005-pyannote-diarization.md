# 说话人分离使用 pyannote community-1

说话人分离采用 pyannote community-1，权重通过 ModelScope 镜像获取，不依赖 Hugging Face token；sherpa-onnx 作为 Docker 集成失败时的备选。Gate 1 已在完整 82.7 分钟录音上跑通：6 个说话人、385 个 turn，独立进程峰值显存 1.59GB。

**Considered Options**

- pyannote community-1：中文会议可用，ModelScope 可下载，无需 HF token；本次已实测。
- sherpa-onnx：CPU 可跑、依赖轻，但本次未实测，仅作备选。
- 3D-Speaker：未验证，暂不引入。
- MOSS-Transcribe-Diarize：联合分离会替换 Qwen3-ASR 基座，与 ADR-0001 冲突。

**Consequences**

- 分离与语音识别分阶段执行；Gate 2 需确认同容器驻留时的显存占用。
- Gate 1 观察到短插话/主持人过分割、说话人切换点切断句子、重叠语音可能分不到字；Gate 2 需要短 turn 合并、说话人标签稳定化与重叠标记等后处理。
- 模型权重放入 `./models`，由模型管理器下载与校验。
- 若 pyannote 在 Docker/Linux 内无法稳定运行，再启用 sherpa-onnx 备选，不替换 ASR 基座。
