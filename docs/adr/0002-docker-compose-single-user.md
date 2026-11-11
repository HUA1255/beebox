---
status: superseded by ADR-0010
---

# 以 docker-compose 单用户自用方式部署

服务只给作者本人使用，因此采用 docker-compose 部署，固定为前后端两个容器：FastAPI 后端（GPU）与 Nginx 前端容器。不做桌面端，不做多用户、登录与对外 API；只绑定本机回环地址。允许复用现成的多容器项目，不强行压成单容器。

**Considered Options**

- 单容器：部署简单，但会把前端静态资源、反向代理与 GPU 后端塞进同一镜像，也排除 OpenTranscribe 这类功能完整的多服务项目。
- 桌面应用：安装最简单，但无法作为 Docker 服务部署；用户明确不需要桌面端。
- 两个以上容器：OpenTranscribe 最少需要 8 个服务，超出本机自用的运维预算。

**Consequences**

- 前端与后端分别构建、分别升级；Nginx 负责静态资源与内部 API 反向代理。
- 两个容器共享 `./models` 与 `./data` 的 volume 约定，详见 ADR-0006。
- 两容器的资源占用与运维复杂度高于单容器，但低于 OpenTranscribe 的多服务栈。
- 个人自用不触发 AGPL/GPL 的分发义务；若以后对外提供服务，需要重新评估。
