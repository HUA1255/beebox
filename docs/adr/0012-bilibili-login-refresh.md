# B 站登录用 bilibili-api 扫码；TV 凭证只做失效检测

为了取 B 站 AI 字幕与高音质音频，BeeBox 用 `bilibili-api-python` 17.4.2（锁定版本）做扫码登录，并把凭证写成 yt-dlp 读取的 cookie 文件。扫码走 TV 通道（`/x/passport-tv-login/qrcode/...`）：网页通道已不再返回 SESSDATA/bili_jct/DedeUserID，TV 通道则返回这组 web cookie 和一个 app 作用域的 `refresh_token`（库把它存为 `Credential.ac_time_value`）。

**TV 凭证不能通过 web 接口续期。** 库的 `Credential.refresh()` 只实现了网页 cookie 刷新：它先取 `refresh_csrf`，再向 `/x/passport-login/web/cookie/refresh` 提交 `source=main_web`、网页 `refresh_token` 与 `bili_jct`；TV 登录返回的 `refresh_token` 是 app 侧的 OAuth token，两者不匹配。库已归档、没有 TV 续期实现，BeeBox 也不自行实现未公开的签名流程（issue #68）。因此契约是「失效检测 + 重新扫码提醒」，而不是「每天自动续期」。

每天的检查按凭证来源分两种：

- **TV 来源（默认；扫码登录目前只有这一条通道）**：只调用 `Credential.check_valid`（`/x/web-interface/nav`，cookie 有效即 `isLogin`）做失效检测，不调用 `check_refresh`/`refresh`，也不改写 cookie 文件。平台仍接受时 `/v1/health` 保持 `valid`，cookie 文件内容与时间戳都不变；平台拒绝时写入失效标记，`/v1/health` 报 `expired`，控制台提示重新扫码，需要登录的视频报 `VIDEO_LOGIN_REQUIRED`。
- **web 来源**：保留原有的网页 cookie 刷新路径；平台要求续期时原子改写 cookie 文件，刷新失败同样落到 `expired`。

来源记录在 secrets 目录的 `bilibili-login-source` 标记文件里，内容只有 `tv` 或 `web`，不含任何凭证。标记缺失按 `tv` 处理：引入来源记录之前保存的凭证都来自 TV 通道，跳过 web 刷新是安全的一侧。凭证永远只写在 secrets 目录，不回显、不入库、不写日志（ADR-0011）。

提醒路径是 `/v1/health` 的 `bilibili_login` 状态：调用方 home（Beer 的家里服务）轮询该状态，在 `expired` 时发邮件提醒，维护者到控制台凭证页重新扫码。BeeBox 自身不发邮件；真实账号的一次失效检测结果记录在 issue #68。

**Considered Options**

- 只做扫码页加失效检测、不自动刷新：依赖最少，但每隔几个月要手动扫码。**（已采用：TV 凭证无法通过 web 接口续期，见上）**
- 手动替换 cookie 文件：最简单，但要到 Windows 上操作。
- `--cookies-from-browser`：无头服务上没有浏览器配置。
- 自行实现 TV OAuth 刷新：TV `refresh_token` 的刷新端点与签名流程没有公开文档，锁定库里也没有实现；逆向引入不可验证的维护负担，收益只是延长扫码间隔。

**Consequences**

- 扫码获得的登录只能用到平台认可的有效期；到期后 `/v1/health` 报 `expired`，home 据此发邮件，维护者到控制台重新扫码。不再承诺“每天自动续期”。
- 本地 cookie 时间戳固定为写入时 +180 天：即使平台仍接受，到期后 `/v1/health` 也会报 `expired` 并提示重新扫码，因此最长约 180 天需要重新扫码一次。
- web 刷新代码保留给 web 来源的凭证；确认没有 web 凭证后可以删除。
- B 站改接口或 RSA 公钥后 web 刷新会失效，只剩扫码兜底；TV 失效检测依赖 `check_valid`，该接口变化时同样退化为重新扫码。
- 私用不分发不触发 GPL 义务；若开源发布需重新评估（YouTube 侧的 bgutil 同为 GPL-3.0）。
- 后续（2026-11，开源发布）：重新评估后，整个项目改用 GPL-3.0-only，与这两个依赖兼容，功能保留不变。
- YouTube 默认不登录，用 bgutil 生成 PO token；只有年龄限制视频才用上传的 cookie。
