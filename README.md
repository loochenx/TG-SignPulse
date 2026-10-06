# TG-SignPulse

> Telegram 多账号自动签到、消息动作编排与关键词监听面板。

[English README](README_EN.md) · [健康检查](#健康检查) · [更新日志](#更新日志)

TG-SignPulse 是一个 Telegram 自动化管理面板。你可以在网页里管理多个账号，配置自动签到任务，并让任务按固定规则每天自动执行。

> AI 驱动：项目已集成 AI 能力（识图、计算题），可直接用于自动任务流程。

## 这个项目是做什么的？

- 统一管理多个 Telegram 账号（手机号验证码登录或二维码扫码登录）
- 自动签到、定时发消息、点击按钮，支持固定时间和随机时间段两种调度模式
- 8 种动作类型，含 AI 识图、AI 计算题、关键词监听
- 支持指定群组话题（Thread/Topic）执行签到
- 实时 WebSocket 日志流，可直接在网页查看执行过程和机器人最后回复
- 支持任务剪贴板批量导入导出、全局代理、失败通知和关键词监听
- 适合 VPS 长期运行

## 项目亮点

- **多账号管理**：手机号 / 二维码两种方式登录，账号支持独立代理
- **8 种动作类型**：发送文本、发送骰子、点击按钮、AI 识图后点按钮、AI 识图后发文本、AI 计算后发文本、AI 计算后点按钮、关键词监听通知
- **两种调度模式**：固定 CRON 时间 或 时间窗口内随机执行
- **话题签到**：支持 Telegram Forum 群组指定 Thread/Topic 内执行
- **通知推送**：Telegram Bot 推送任务失败/账号失效/登录通知；关键词命中支持 Telegram Bot、Bark、自定义 URL 三种渠道
- **实时日志**：WebSocket 实时推送执行日志，历史记录自动保留 3 天
- **任务迁移**：全部任务导出到剪贴板，粘贴导入自动跳过重复任务
- **面板安全**：JWT 认证 + TOTP 两步验证，支持单独关闭每个任务的失败通知
- **容器化部署**：Docker / Docker Compose 开箱即用，自动适配挂载目录的 UID/GID

## 功能概览

| 模块 | 能力 |
| --- | --- |
| 账号管理 | 多账号登录（手机号/二维码）、独立代理、状态检测、重新登录、TOTP 2FA |
| 任务编排 | 固定 CRON / 时间窗口随机执行，8 种动作类型，动作间隔与自动删消息 |
| 话题支持 | 群组 `Thread ID` 级别的发送与回复过滤 |
| 关键词监听 | 包含/正则两种匹配，命中后推送通知或继续执行后续动作序列 |
| 推送通知 | 全局：Telegram Bot（任务失败/账号失效/登录）；关键词命中：Telegram Bot / Bark / 自定义 URL |
| 运维能力 | Docker 部署、持久化数据目录、健康检查、配置版本自动迁移、导入导出 |

## 小白 3 步部署（推荐）

1. 安装 Docker（服务器和本机都可）
2. 执行下面命令启动容器
3. 浏览器打开 `http://服务器IP:8080`，用默认账号登录

默认凭据：
- 账号：`admin`
- 密码：`admin123`（首次登录会跳转到设置页并提示修改，新密码不能再用 `admin123`）

### 一条命令启动

```bash
docker run -d \
  --name tg-signpulse \
  --restart unless-stopped \
  -p 8080:8080 \
  -v $(pwd)/data:/data \
  -e TZ=Asia/Shanghai \
  -e APP_SECRET_KEY=your_secret_key \
  luochend/tg-signpulse:latest
```

如果你走反代（如 Nginx），可改成仅本机监听：

```bash
-p 127.0.0.1:8080:8080
```

### Docker Compose（可选）

```yaml
services:
  app:
    image: luochend/tg-signpulse:latest
    container_name: tg-signpulse
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./data:/data
    environment:
      - TZ=Asia/Shanghai
      - APP_SECRET_KEY=your_secret_key
```

## 数据目录与权限说明

- 默认数据目录：`/data`
- 当 `/data` 不可写时，会自动降级到 `/tmp/tg-signpulse`（非持久化）
- 新镜像已支持根据 `/data` 挂载目录属主 UID/GID 自动适配运行身份，通常无需 `chmod 777`

容器内排查命令：

```bash
id
ls -ld /data
touch /data/.probe && rm /data/.probe
```

## 常用环境变量（简版）

- `APP_SECRET_KEY`: 面板登录密钥，建议设置为 16 位以上随机字符串；未设置时自动生成并保存在数据目录的 `.secret_key` 文件中（删除该文件会使所有登录失效）
- `APP_CORS_ORIGINS`: 允许跨域访问的来源，逗号分隔；默认不开放跨域（面板与 API 同源，无需设置）
- `ADMIN_PASSWORD`: 初次安装时 admin 账户的默认密码（安全起见强烈建议设置，未设置则默认 admin123）
- `APP_HOST`: FastAPI 容器监听 IP，防暴露默认 `127.0.0.1`（如需用公网直连或宿主机反代端口请设为 `0.0.0.0`）
- `APP_DATA_DIR`: 自定义数据目录（优先级高于面板配置）
- `TG_PROXY`: Telegram 连接代理；也可在面板设置全局代理
- `TG_SESSION_MODE`: `file`（默认）或 `string`（arm64 推荐）
- `TG_LOGIN_GET_ME_TIMEOUT`: 签到登录身份确认的最长等待秒数，默认 `12`；超时后任务会按有限次数重试
- `TG_SESSION_NO_UPDATES`: `1` 启用 `no_updates`（仅 `string` 模式）
- `TG_GLOBAL_CONCURRENCY`: 全局并发（默认 `1`）
- `APP_TOTP_VALID_WINDOW`: 面板 2FA 容错窗口
- `APP_UPDATE_CHECK`: 设为 `0` 关闭检查更新（不再访问 GitHub 和镜像仓库）
- `APP_SELF_UPDATE`: 设为 `0` 关闭一键更新（即使挂载了 docker.sock）
- `APP_UPDATE_REPO`: 检查新版本用的 GitHub 仓库，默认 `loochenx/TG-SignPulse`
- `APP_UPDATE_CONTAINER`: 面板所在容器的名称或 ID，自动识别失败时再填
- `APP_UPDATE_CLEANUP`: 设为 `0` 更新后保留旧镜像（默认删除）
- `DOCKER_HOST`: Docker 地址，默认 `unix:///var/run/docker.sock`

## 自定义数据目录

你可以通过两种方式设置数据目录：

1. 面板设置：`系统设置 -> 全局签到设置 -> 数据目录`
2. 环境变量：`APP_DATA_DIR=/your/path`

说明：
- 修改后建议重启后端服务生效
- 该目录请务必可写，并挂载持久化卷

## 本地开发

- 推荐使用 Python 3.12；项目支持 Python `>=3.10,<3.14`
- 不建议使用 Python 3.14 及以上版本，本项目依赖的 Telegram/Pydantic 运行时组件暂未完全兼容
- 前端使用 Node.js 20，进入 `frontend/` 后执行 `npm ci`

## 常用面板设置

在 `系统设置 -> 全局签到设置` 中可以配置：

- 全局代理：账号未单独配置代理时，登录、刷新会话和执行任务会默认使用该代理
- Telegram机器人通知：填写 Bot Token 和通知 Chat ID 后，任务失败、账号登录失效或关键词命中会自动发送通知
- 数据目录：用于保存 sessions、logs、数据库和任务数据

在账号任务页可以：

- 为目标群组填写 `话题 / Thread ID`，让签到只在指定话题内执行
- 在有序动作序列中添加 `关键词监听`，并在 `推送方式` 下拉框中选择 Telegram机器人、转发、Bark 或自定义 URL
- 仅当选择 `转发`、`Bark` 或 `自定义推送 URL` 时，页面才显示对应参数输入框，减少无关配置干扰
- 点击右上角导出图标，将当前账号全部任务复制到剪贴板
- 点击右上角"粘贴导入任务"，从剪贴板批量导入任务并跳过已存在的重复任务

## 页面内更新

在 `系统设置 -> 软件更新` 可以查看当前版本并检查更新（结果缓存 6 小时，可手动重新检查）。有新版本时，设置入口会出现一个提示点。

### 一键更新（可选）

把宿主机的 Docker socket 挂进容器，面板就能自己下载新镜像并重建容器：

```yaml
    volumes:
      - ./data:/data
      - /var/run/docker.sock:/var/run/docker.sock
```

使用 `docker run` 时加上 `-v /var/run/docker.sock:/var/run/docker.sock`。没有挂载时，面板只检查更新，并给出在服务器上手动更新的命令。

点击「立即更新」后：

1. 面板拉取同一标签的新镜像（如 `:latest`），镜像没变就提示已是最新
2. 启动临时助手容器 `<容器名>-updater`，停止旧容器，按原配置创建新容器（端口、数据卷、环境变量、网络、重启策略等保持不变）
3. 新容器正常运行后删除旧容器和旧镜像；新容器起不来时自动恢复旧容器
4. 页面自动刷新到新版本，整个过程约 1 分钟无法访问

注意事项：

- **挂载 docker.sock 等于让面板拥有管理本机所有容器的权限**。开启前请修改默认密码并开启两步验证，不要把面板直接暴露在公网
- 有任务正在运行时不能更新，请等任务结束再操作
- 镜像仓库里找不到的镜像（如本地 `docker compose up -d --build` 构建的 `tg-signpulse:latest`）和需要登录的私有镜像无法一键更新，面板会给出手动更新命令
- 本地构建的镜像如果与仓库中的镜像同名，一键更新会换成仓库里的版本
- 用 `user:`（或 `docker run --user`）指定了运行用户时，还需要加上 socket 的属组：`group_add: ["<GID>"]`，GID 可用 `stat -c %g /var/run/docker.sock` 查看
- 不需要这个功能时，不挂载 socket 或设置 `APP_SELF_UPDATE=0` 即可

## 健康检查

- `GET /healthz`：快速健康检查
- `GET /readyz`：服务就绪检查

## 项目结构

```text
backend/      FastAPI 后端与调度器
tg_signer/    Telegram 自动化核心
frontend/     Next.js 管理面板
```

## 更新日志

### 2026-09-17（页面内更新）

- **页面内检查更新**：`系统设置 -> 软件更新` 显示当前版本、最新版本和更新内容链接，有新版本时设置入口出现提示点；可通过 `APP_UPDATE_CHECK=0` 关闭。
- **一键更新（可选）**：挂载 `/var/run/docker.sock` 后，面板可拉取新镜像并由助手容器按原配置重建自身，新容器起不来时自动回滚；未挂载时显示手动更新命令。详见「页面内更新」。
- **前端改版为移动端优先**：列表式设置页、底部导航与底部弹层，适配手机单手操作；登录页换成品牌脉冲线装饰。
- 镜像入口脚本在挂载 docker.sock 时，会把 socket 的属组加给运行用户。

### v0.9.0（2026-09-16）

本版本汇总 v0.8.8 之后的全部修复与优化（含下方 2026-09-14 ~ 09-15 各条目），并包含：

- **登录流程更稳**：手机号登录放弃后按 `PHONE_LOGIN_TTL_SECONDS`（默认 600 秒）自动释放账号锁，不再阻塞该账号的签到任务；重复提交验证码会直接提示，不再并发验证。
- **登录失效自动退出**：实时日志连接在 token 失效时以 4401 关闭，前端随即回到登录页。
- **关键词监控看门狗**：重启监控时不再误取消看门狗自身。
- **配置读写更安全**：全局设置与账号信息改为原子写入并按修改时间缓存，多线程并发写不再丢数据。
- **修改调度时间立即生效**：时间窗口模式下，修改窗口后会清除旧窗口里已排好的执行。
- **账号页面重构**：Dashboard 拆分为独立组件，减少不必要的重渲染；扫码登录轮询更稳定。

安全修复（升级前请留意）：

- **移除免验证码重置两步验证的接口**：`/api/auth/reset-totp`（仅凭密码即可关闭 2FA）与 `/api/user/totp/reset` 已删除。丢失验证器时，在服务器上执行：
  `docker exec tg-signpulse sh -c 'gosu "$(stat -c %u:%g /data)" python -m backend.cli.reset_totp admin'`
- **不再使用内置固定密钥**：未设置 `APP_SECRET_KEY` 时改为自动生成随机密钥。之前没有设置该变量的部署，升级后需要重新登录一次。
- **默认关闭跨域**：如需从其他域名调用 API，请设置 `APP_CORS_ORIGINS`。
- **默认密码提醒**：仍使用 `admin123` 登录时会跳转到设置页并提示修改。
- 通用任务异常时也会记录结束时间；通用任务实时日志在登录失效时同样以 4401 关闭。
- `pyproject.toml` 的 bcrypt 版本约束改为 `>=3.2,<4.1`，与 Docker 镜像中的 4.0.1 一致。

### 2026-09-15（性能与稳定性优化）

- **修复「立即签到」弹窗秒显示结束**：运行接口改为后台启动后立即返回，前端随后再建立实时日志连接，不再因任务尚未开始而被误判为已结束。
- **修复实时日志卡住/串台**：日志按累计序号与运行编号增量推送，超过 1000 行不再停止推送，60 秒内重复运行也不会先推上一次的旧日志；任务结束时推送成功/失败结果。
- **运行请求不再被反向代理超时打断**：长任务不再占住 HTTP 请求。
- **前端请求超时**：读取类请求 30 秒、写入类请求 180 秒超时并提示，避免页面无限转圈。
- **实时日志连接管理**：关闭弹窗、离开页面或重复运行时主动断开旧连接；WebSocket 鉴权后立即释放数据库连接；前端日志最多保留 1000 行。
- **移除未使用的 `/api/events/logs` 接口**：该接口在事件循环中同步轮询数据库，会阻塞整个服务。
- **静态托管加固**：前端文件解析限制在 `/web` 目录内；`/_next` 构建产物启用长期缓存，HTML 入口不缓存；未知 `/api/*` 路径返回 404。

### 2026-09-15（第五轮代码审查修复）

- **修复并发日志隔离漏洞**：使用 `contextvars.ContextVar` 替代 ERROR 级别放行方案。asyncio 每个 Task 持有独立的 Context 副本，设置 `_current_task_account` 后 filter 可捕获该协程所有级别日志，不再通过账号前缀匹配，彻底消除多账号并发时的日志串台问题。

### 2026-09-15（第四轮代码审查修复）

- **修复任务崩溃时日志丢失**：`_AccountTaskLogFilter` 现在无条件放行 ERROR 级别及带异常堆栈的日志，之前 `core.py` 内直接调用 `logger.exception` 的报错因不含账号前缀而被静默过滤，用户面板看到的日志为空。
- **修复任务 key 可能永久锁死**：`_active_tasks[task_key] = True` 与日志处理器挂载移入 `try` 块内，确保任何初始化异常都会由 `finally` 正确清理，不再需要重启服务才能恢复。
- **修复 `_fetch_chats` 缩进错误**：`async with` 块体从 12 格错误缩进修正为标准 4 格，消除作用域误读隐患。

### 2026-09-15（逻辑优化）

- **执行日志截断生效**：`SIGN_TASK_HISTORY_MAX_FLOW_LINES`（默认 5000 行）与 `SIGN_TASK_HISTORY_MAX_LINE_CHARS`（默认 2000 字符）此前只读取未生效，现已真正截断（保留尾部日志），防止历史文件无限膨胀。
- **巡检跳过已失效账号**：已标记为需重新登录的账号不再重复发起 Telegram 连接，减少无效请求。
- **通知逻辑合并**：成功 / 失败 / 账号失效三类 Bot 通知共用一个发送方法，消除约 60 行重复代码。
- **Chat 缓存原子写入**：`chats_cache.json` 及 legacy 历史文件改为原子替换写入，崩溃不再损坏文件。
- **账号锁统一**：移除服务内冗余的锁字典，统一使用全局 `get_account_lock()`。
- **日志规范**：任务异常堆栈改走 `logger.exception`，不再直接打印到 stderr；清理残留乱码注释。
- **仓库地址更新**：登录页与设置页的 GitHub 图标链接改为 [loochenx/TG-SignPulse](https://github.com/loochenx/TG-SignPulse)。

### 2026-09-15（账号健康巡检）

- **新增定时账号健康巡检**：每天 09:00 自动检测所有账号 session 是否有效，发现失效立即通过 Telegram Bot 推送通知，无需等到签到任务失败才知道。
- **新增手动触发巡检**：账号页面顶部导航栏新增「巡检」按钮（💓图标），点击立即对所有账号执行一次检测并刷新状态。
- **通知去重**：同一账号已推送失效通知后，再次巡检不重复通知；账号重新登录恢复正常后，下次失效时会重新发送通知。

### 2026-09-14（第三轮代码审查修复）

- **修复 `_load_history_entries` 死代码条件**：`account_name and X or not account_name and X` 恒等于 `X`，`account_name` 判断完全无效，已简化为 `if legacy_file.exists()`，消除维护误解隐患。
- **修复 `in_memory_run` 绕过生命周期管理**：从直接调 `await self.app.start()` 改为 `async with self.app:`，确保 `_CLIENT_REFS` 引用计数正确维护，且 session 失效时统一抛出 `ConnectionError("Session invalid: ...")`，行为与其他连接路径一致。
- **历史文件写入改为原子替换**：`_save_run_info` 中 `history_file` 写入现在也使用 `tempfile + os.replace`，进程崩溃不再截断历史记录。
- **提取 `_atomic_write_json` 辅助方法**：`create_task`、`update_task`、`set_task_enabled`、`clear_account_history_logs` 中所有 `config.json` 写入统一走原子替换，消除非原子写入的数据损坏风险，代码也更简洁。
- **修复 `_active_logs.setdefault` 类型错误**：fallback 值从 `list` 改为 `deque(maxlen=1000)`，与其他日志缓冲区类型一致。
- **修复乱码 docstring**：`clear_account_history_logs` 函数注释修正为正确的简体中文。

### 2026-09-14（第二轮代码审查修复）

- **修复日志过滤器子串误判**：`_AccountTaskLogFilter` 改为匹配完整账号前缀 `账户「{name}」`，防止账号名是另一账号名子串时日志仍然交叉污染。
- **修复 `check_account_status` 误判 session 失效**：`except ConnectionError` 分支新增 "session invalid" 检测，session 失效时正确返回 `needs_relogin=True`，不再被当成临时网络错误放行。
- **移除 `refresh_account_chats` 重复 `get_me()` 调用**：`__aenter__` 已完成 session 校验，删除多余的显式调用。
- **修复 `__aenter__` 双重 `get_me()`**：去掉 `start()` 前的预检 `get_me()`，改为从 `start()` 直接捕获 auth 错误（`Unauthorized`/`AuthKeyInvalid` 等），消除每次首次连接多一次 API 请求的问题。
- **`_save_run_info` 写 config.json 改为原子替换**：先写临时文件再 `os.replace`，防止进程崩溃导致配置损坏、`last_run` 错乱。
- **区分 range-run 与 catchup 的 job_id**：`_schedule_range_random_run` 改用 `-range-run` 后缀，避免任务编辑时 catchup 覆盖今天已计算好的随机执行时间。
- **`list_accounts` 缓存不再保存实时 status 字段**：缓存只存结构字段，每次返回时实时合并 status，消除缓存中 stale status 被其他代码读到的隐患。
- **`normal_run()` 支持 chat_ids 热更新**：CLI 长期运行时每轮迭代重新加载配置，若 chat_ids 变化则自动重新注册 handler，无需重启。

### 2026-09-14（代码质量优化）

- **优化并发日志隔离**：为 `TaskLogHandler` 新增 `_AccountTaskLogFilter`，多账号并发执行时日志不再交叉污染。
- **修复任务状态内存泄漏**：`_active_tasks` 任务结束后改用 `.pop()` 而非 `= False`，避免长期运行后 dict 无限增长。
- **优化日志缓冲**：`_active_logs` 改用 `deque(maxlen=1000)`，截断旧日志由 O(n) 降为 O(1)。
- **修复 `close_client_by_name` 竞争窗口**：在持锁期间弹出 client 实例，消除锁外 stop 导致正在启动的连接被断开的问题。
- **修复 `check_account_status` 引用计数绕过**：改用 `async with client:` 上下文管理器，正确维护 `_CLIENT_REFS`。
- **清理调度器死代码**：`sync_jobs` 中 `reschedule_job` 立刻被 `add_job(replace_existing=True)` 覆盖，已移除冗余调用。
- **修复调度器 job 持有过期 dict 引用**：range 模式向 APScheduler 传入只含 `range_start/range_end` 的轻量副本，任务更新后不再引用旧配置。
- **其他**：预编译 `_BOT_ERROR_PATTERN` 正则、`_save_run_info` 中去掉多余的 `get_task()` 磁盘读取、`list_accounts` 异常改为 `logger.exception` 记录。

### 2026-09-14（Bug 修复）

- **修复任务长期运行后无法执行**：消息处理器在异常退出时未从共享 Client 移除，导致每次失败都累积处理器；改用 `try/finally` 保证始终清理。
- **修复 session 失效误判**：`check_account_status` 中的宽泛字符串匹配 (`"SESSION" and "INVALID"`) 会将 Pyrogram 内部异常误判为 session 失效，导致账号被永久写入 `needs_relogin=True`；改为精确匹配 Telegram API 错误码。
- **修复检查后遗留连接**：`check_account_status` 调用 `client.connect()` 后不断开，导致 client 长期处于半连接状态；现在检查完毕后在 `finally` 中释放连接。
- **修复匿名消息 AttributeError**：`on_message` / `on_edited_message` 中 `from_user` 为 None（频道或匿名消息）时访问 `.username` 崩溃，已改为安全访问。
- **代码质量**：修复全部 ruff lint 错误（BLE001、SIM117、TRY401 等约 990 处），31 个测试全部通过。

### 2026-05-21

- **任务失败自动重试**：签到任务因网络超时或其他错误失败时，10 分钟后自动重试一次；账号 session 失效时不重试（避免无效请求）；重试任务本身失败后不再产生二次重试。
- **range 模式延迟改用 DateTrigger**：随机延迟由 `asyncio.sleep` 改为注册 APScheduler `DateTrigger`，进程重启后仍可在窗口内完成执行。
- **成功通知精简**：移除通知中的日志尾部，仅保留账号、任务名及签到回复内容。

### 2026-05-12

- **修复任务执行 500 错误**：`run_task_with_logs` 中 `except` 块的局部 `logger` 赋值导致整个函数内 `logger` 变为未绑定局部变量，触发 `UnboundLocalError`，已移除该多余赋值。
- **编辑/新建任务后自动补执行**：创建、编辑或启用 range 模式任务时，若当前时间已在执行窗口内且今日未执行，会立即安排一次性补执行，不再等到第二天。

## 致谢

本项目 fork 自 [akasls/TG-SignPulse](https://github.com/akasls/TG-SignPulse)，其上游为 [amchii/tg-signer](https://github.com/amchii/tg-signer)，感谢两位作者的开源工作。

技术栈：FastAPI、Uvicorn、APScheduler、Pyrogram/Kurigram、Next.js、Tailwind CSS、OpenAI SDK。
