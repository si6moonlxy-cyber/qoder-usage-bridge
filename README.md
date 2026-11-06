# qoder-usage-bridge

把 [Qoder IDE](https://qoder.com) 的 token 用量接进 [CC Switch](https://ccswitch.io) 的「用量统计」面板。

Feed Qoder IDE token usage into CC Switch's usage dashboard.

## 中文说明

### 它解决什么问题

CC Switch 的用量面板只统计它内置支持的工具（Claude Code、Codex、Gemini CLI、OpenCode、Kimi Code、DeepSeek Harness、Antigravity 等），Qoder 不在其中。

Qoder 会把每条助手消息的 token 用量明文存在本地 SQLite 里，这个脚本把它按 CC Switch 的 `proxy_request_logs` 结构写进去，于是面板里能看到 Qoder 的：

- 请求数、新增输入、输出、缓存命中
- 模型（解析成 `DeepSeek-Flash`、`Qwen3.8-Flash` 这样的真实名字）
- 供应商（模型提供方：DeepSeek / Alibaba / Moonshot / Zhipu / MiniMax，识别不出时记为 Qoder）
- 成本（按 CC Switch 的价目表计算，可以在 CC Switch 里自己补单价）

### 工作方式

```
Qoder local.db (chat_message.token_info, 只读)
        │  prompt / cached / completion
        ▼
模型解析：chat.modelConfig.session.* -> aicoding.customModels / 系统模型表
        │  显示名 + provider
        ▼
计价：cc-switch model_pricing
        │  USD / 1M tokens
        ▼
cc-switch.db (proxy_request_logs, data_source='qoder_session')
        ▼
CC Switch「用量统计」-> 全部
```

只增删 `data_source='qoder_session'` 的行，其它应用的数据不受影响。

### 环境要求

- Windows / macOS / Linux（Windows 已实测）
- Python 3.9+，无第三方依赖
- Qoder IDE（本地库为 SQLite）
- CC Switch 4.0.x（已实测 4.0.26）

### 快速开始

```bash
# 1. 先预演，只打印，不写库
python qoder_usage_bridge.py

# 2. 实际写入（首次会自动备份 cc-switch.db）
python qoder_usage_bridge.py --apply

# CC Switch 开着也能写
python qoder_usage_bridge.py --apply --allow-running

# 3. 查看已导入的数据
python qoder_usage_bridge.py --list

# 4. 全部撤销
python qoder_usage_bridge.py --reset
```

写完回到 CC Switch 的「用量统计」，点一下刷新即可看到；应用筛选里没有 Qoder 图标，请在「全部」下查看。

### 自动同步（推荐）

CC Switch 没有给外部脚本留下钩子，它的内置同步只认自己支持的那几个 app 的 session 日志，Qoder 不在其中。所以这里交给 Windows 计划任务每分钟拉一次：`schtasks` 注册，不需要管理员权限，也不留常驻进程。

```powershell
powershell -ExecutionPolicy Bypass -File .\install-task.ps1
# 改成每 2 分钟
powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -Interval 2
# 卸载
powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -Uninstall
```

- 任务名 `QoderUsageBridge`，可用 `schtasks /query /tn QoderUsageBridge` 查看
- 以 `pythonw.exe --apply --allow-running --quiet` 静默运行，不会弹窗
- 脚本内部调用外部命令（如 `tasklist`）时加了 `CREATE_NO_WINDOW`，不会闪黑框、不抢焦点
- 日志：`%LOCALAPPDATA%\qoder-usage-bridge\bridge.log`（单文件，超 5MB 自动轮转）
- 备份最多每小时一次，只保留最近 5 份，不会被每分钟的任务塞满磁盘

再在 CC Switch 设置里把用量面板的刷新间隔调成 1 分钟（对应 `settings.json` 的 `usageDashboardRefreshIntervalMs`），面板就会自己滚动刷新，看起来跟内置统计没区别。

如果希望 Qoder 一写库就同步（秒级），改用常驻模式：

```bash
python qoder_usage_bridge.py --watch
```

- 每 60 秒心跳同步一次（`--interval` 可调）
- Qoder 数据库一有写入，几秒内立即补同步（`--debounce` 可调）

### 模型与价格是怎么来的

1. `chat_message.token_info` 给出 `prompt_tokens` / `cached_tokens` / `completion_tokens`
2. 当 `model_info.model_key` 是 `qfmodel` 这类系统模型时，用 `aicoding.modelConfigs.cache.assistant` 里的显示名
3. 当它是 `custom_model`（BYOK）时，用 `workspaceStorage/*/state.vscdb` 里 `chat.modelConfig.session.<会话ID>` 的 `custom:model_xxx`，再到 `globalStorage` 的 `aicoding.customModels` 取 `displayName` 与 `providerDisplayName`
4. 供应商取模型的提供方：BYOK 直接用 `providerDisplayName`；系统模型没有这个字段，按显示名前缀推断（DeepSeek / Qwen / Kimi / GLM / MiniMax），推断不出时记 `Qoder`
5. 价格优先匹配 CC Switch 的 `model_pricing`（去掉 `-pg` 等后缀后匹配），匹配不到就记 0，界面显示「未定价」；补好单价后重跑脚本会自动回填

### 已知限制

- Qoder 升级会重建本地库，历史最多只能追到重建当天
- 「速度」「首字」为空：Qoder 本地不存时延数据
- Qoder 内置模型的单价来自 CC Switch 价目表，是估算值，不等于 Qoder 实际扣的积分
- 应用筛选栏没有 Qoder 图标（应用列表是 CC Switch 内置的）
- CC Switch 的库结构若在后续版本改变，脚本需要相应调整

### 安全与回滚

- 数据最差晚 1 分钟（计划任务周期），不是实时
- Qoder 的库始终以 `mode=ro` 只读打开
- 写入前备份 `~/.cc-switch/cc-switch.db` 到 `~/.cc-switch/backups/qoder-import-<时间戳>/`，最多每小时一次、保留最近 5 份
- 只写 `data_source='qoder_session'` 的行，`--reset` 也只删这些行
- 出问题可直接用备份覆盖回去

### 免责声明

非官方工具，与 Qoder、CC Switch 均无关联。直接写第三方应用的数据库存在风险，请先备份、自行评估，后果自负。

## English

CC Switch's usage dashboard only tracks its built-in apps. Qoder is not one of them. This script bridges Qoder's local token usage into CC Switch:

- reads `chat_message.token_info` from Qoder's SQLite database (read-only)
- resolves the real model name and provider from Qoder's VS Code-style state databases
- prices each request with CC Switch's `model_pricing` table
- upserts rows into `proxy_request_logs` with `data_source='qoder_session'`

```bash
python qoder_usage_bridge.py            # dry run
python qoder_usage_bridge.py --apply    # write (auto backup)
python qoder_usage_bridge.py --watch    # keep in sync every 60 seconds
python qoder_usage_bridge.py --list     # inspect imported rows
python qoder_usage_bridge.py --reset    # remove imported rows
```

CC Switch offers no hook for external scripts, so the recommended setup is a per-user Windows scheduled task that runs the bridge every minute (no admin rights, no resident process):

```powershell
powershell -ExecutionPolicy Bypass -File .\install-task.ps1
powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -Uninstall
```

Only rows tagged `data_source='qoder_session'` are touched. Qoder's database is opened read-only. Backups go to `~/.cc-switch/backups/qoder-import-<timestamp>/`, at most once per hour, keeping the five newest.

Known limits: no latency data (Qoder does not store it), historical usage is limited to the current Qoder database, and prices for Qoder's bundled models are estimates from CC Switch's price table.

Unofficial project, not affiliated with Qoder or CC Switch. Use at your own risk.

## License

MIT
