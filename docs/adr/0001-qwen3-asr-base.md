# 使用 Qwen3-ASR-1.7B 作为语音识别基座

本版本以 Qwen3-ASR-1.7B（Apache-2.0）为 ASR 基座，优先保证中文会议/讲座的识别准确率；时间戳由 Qwen3-ForcedAligner-0.6B 生成，说话人分离由独立阶段完成。

作者对比过 Whisper large-v3 等方案后选择 Qwen3-ASR：中文识别更准确，代价是工具链更薄、没有原生说话人分离，且 RTX 2080 Ti 只能使用 fp16。

**Considered Options**

- Whisper large-v3：生态成熟、集成多，但中文准确率不及 Qwen3-ASR。
- FireRedASR2：中文较强，但工具链和社区支持更薄。
- MOSS-Transcribe-Diarize：联合分离，但会替换掉 Qwen3-ASR 基座，不符合本版本选择。

**Consequences**

- 必须额外集成 ForcedAligner 才能得到字/词级时间戳。
- 说话人分离必须由独立阶段（pyannote、3D-Speaker、sherpa-onnx 等）完成。
- 模型推理需固定 fp16，不能依赖 bf16。
