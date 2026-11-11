# 在 Windows 上原生部署，不用 Docker

BeeBox 只支持一种部署形态：Windows 11 GPU 主机上的原生服务（uv 锁定的 venv，WinSW 托管，开机自启、崩溃自动重启），由仓库里的部署脚本按 git tag 更新。实际运行环境本来就是原生的，维护一套没人用的 Docker 只会让每次改动都要双环境验证。开发也在同一台机器上，所以开发实例与线上实例必须隔离（目录、端口、数据、token），模型目录只读共用；真实模型测试与部署都先暂停线上队列，等当前任务跑完。

**Considered Options**

- docker-compose（原 ADR-0002）：可复现，但与实际运行方式不符，而且 Windows 上 GPU 容器要经过 WSL。
- 保持计划任务加 PowerShell 包装：已在跑，但崩溃不自愈、依赖不可复现、线上代码与 main 脱节。

**Consequences**

- 取代 ADR-0002（docker-compose）；ADR-0006 的访问部分由 ADR-0011 取代。
- 删除 Dockerfile、compose、nginx 与桌面打包；CI 用 `windows-latest`。
- 控制台作为静态文件挂在服务自己的 `/console` 下，只有一个端口。
- 外部工具（ffmpeg、Deno）放在固定的工具目录，不依赖 winget 带版本号的路径。
