---
status: superseded by ADR-0011
---

# 仅本机访问，模型与数据使用宿主目录 volume

v1 只服务本机：Nginx 仅发布到 `127.0.0.1`，后端只在 compose 内部网络暴露，不提供对外 API，也不做登录鉴权。模型权重放宿主 `./models`，任务数据放 `./data`，两者都以 bind mount 挂载；数据不自动清理，由用户手动删除；派生 wav 在转写成功后清理。

**Considered Options**

- 仅回环地址：满足单用户自用，攻击面最小；代价是不能从局域网或手机访问。
- 绑定 `0.0.0.0` 加鉴权：可远程访问，但需要额外的认证、TLS 与滥用防护，超出 v1。
- Docker named volume：管理简单，但模型与数据不便直接检查、备份和手动清理。

**Consequences**

- 局域网与远程访问不在 v1；若以后需要，先补鉴权与 TLS，再改绑定地址。
- 备份与迁移靠复制 `./models` 和 `./data`；删除任务即手动删除 `./data` 下对应目录。
- 模型下载走 asrbox 模型管理器（ModelScope / Hugging Face）；pyannote 权重走 ModelScope 镜像，语音识别权重按 ADR-0001 处理。
- 派生 wav 只作中间产物，转写成功后清理，避免 `./data` 持续膨胀。
- 不做自动保留期与清理策略；磁盘管理由用户负责。
