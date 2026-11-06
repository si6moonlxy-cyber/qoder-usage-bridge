# qoder-usage-bridge

简体中文 | [English](README_EN.md)

[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)]()
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)]()

把 [Qoder IDE](https://qoder.com) 的 token 用量接进 [CC Switch](https://ccswitch.io) 的「用量统计」面板。

Feed Qoder IDE token usage into CC Switch's usage dashboard.

> 非官方工具，与 Qoder、CC Switch 均无关联。直接写第三方应用的数据库存在风险，请先备份、自行评估。

---

## 为什么需要它

CC Switch 的用量统计只覆盖它内置支持的应用（Claude Code、Codex、Gemini CLI、OpenCode、Kimi Code、DeepSeek Harness、Antigravity 等），Qoder 不在名单里——它没有外部数据源接口，也不认识 Qoder 的会话日志。

但 Qoder 自己会把每条助手消息的 token 用量**明文**存在本地 SQLite 的 `chat_message.token_info` 里。这个脚本就干一件事：把这些数字按 CC Switch 的表结构搬过去，于是面板里出现了 Qoder 的统计。

## 它在面板里显示什么

| 面板列 | 来源 |
| --- | --- |
| 请求数 | `chat_message` 中带 token 用量的助手消息条数 |
| 新增输入 | `prompt_tokens - cached_tokens` |
| 缓存命中 | `cached_tokens` |
| 输出 | `completion_tokens` |
| 模型 | 解析成 `DeepSeek-Flash`、`Qwen3.8-Flash` 这样的真实名字，而不是 `custom_model` |
| 供应商 | 模型厂商：`DeepSeek` / `Alibaba` / `Moonshot` / `Zhipu` / `MiniMax`，识别不出记 `Qoder` |
| 成本 | 按 CC Switch 自己的 `model_pricing` 价目表计算（USD） |
| 速度 / 首字 | 空——Qoder 本地不存时延数据 |

## 工作原理

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

只增删 `data_source='qoder_session'` 的行，其它应用的数据完全不动。

## 环境要求

- Python 3.9+，**无第三方依赖**
- Qoder IDE（本地库为 SQLite）
- CC Switch 4.0.x（实测 4.0.26）
- Windows 已实测；macOS / Linux 的路径探测已实现但未实机验证

## 安装

```bash
git clone https://github.com/si6moonlxy-cyber/qoder-usage-bridge.git
cd qoder-usage-bridge
```

## 使用

```bash
# 1. 预演：只打印，不写库
python qoder_usage_bridge.py

# 2. 实际写入（首次自动备份 cc-switch.db）
python qoder_usage_bridge.py --apply

# CC Switch 开着也能写
python qoder_usage_bridge.py --apply --allow-running

# 3. 查看已导入的数据
python qoder_usage_bridge.py --list

# 4. 全部撤销
python qoder_usage_bridge.py --reset
```

写完回到 CC Switch 的「用量统计」，点一下刷新即可看到；应用筛选里没有 Qoder 图标，请在「全部」下查看。

### 命令行参数

| 参数 | 说明 |
| --- | --- |
| `--qoder-db` | Qoder `local.db` 路径，默认按平台自动探测 |
| `--ccswitch-db` | `cc-switch.db` 路径，默认 `~/.cc-switch/cc-switch.db` |
| `--apply` | 真正写入（缺省为预演） |
| `--allow-running` | CC Switch 运行中照样写入 |
| `--watch` | 常驻模式：心跳 + 数据库变更即同步 |
| `--interval` | 心跳间隔，默认 60 秒 |
| `--poll` | 变更轮询间隔，默认 5 秒 |
| `--debounce` | 检测到变更后等待时间，默认 3 秒 |
| `--log` | 日志文件路径 |
| `--quiet` | 不打印到 stdout，自动落日志 |
| `--list` | 打印已导入行 |
| `--reset` | 删除所有已导入的 Qoder 行 |
| `--version` | 版本号 |

## 自动同步

### 方案 A：Windows 计划任务（推荐）

CC Switch 没有给外部脚本留钩子，它的内置同步只认自己支持的那几个应用的会话日志。所以这里交给 Windows 计划任务每分钟拉一次：`schtasks` 注册，**不需要管理员权限**，也**不留常驻进程**。

```powershell
powershell -ExecutionPolicy Bypass -File .\install-task.ps1
# 改成每 2 分钟
powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -Interval 2
# 卸载
powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -Uninstall
```

- 任务名 `QoderUsageBridge`，可用 `schtasks /query /tn QoderUsageBridge` 查看
- 以 `pythonw.exe --apply --allow-running --quiet` 静默运行，不弹窗
- 脚本内部调用外部命令（如 `tasklist`）时加了 `CREATE_NO_WINDOW`，不闪黑框、不抢焦点
- 日志：`%LOCALAPPDATA%\qoder-usage-bridge\bridge.log`（超过 5MB 自动轮转）
- 备份最多每小时一次、只保留最近 5 份，不会被每分钟的任务塞满磁盘

再在 CC Switch 设置里把用量面板的刷新间隔调成 1 分钟（对应 `settings.json` 的 `usageDashboardRefreshIntervalMs`），面板就会自己滚动刷新，观感上和内置统计没有区别。

### 方案 B：常驻进程（秒级）

```bash
python qoder_usage_bridge.py --watch
```

- 每 60 秒心跳同步一次（`--interval` 可调）
- Qoder 数据库一有写入，几秒内立即补同步（`--debounce` 可调）
- 适合想第一时间看到数据、且不介意多一个后台进程的场景

## 模型与供应商是怎么来的

1. `chat_message.token_info` 给出 `prompt_tokens` / `cached_tokens` / `completion_tokens`
2. 当 `model_info.model_key` 是 `qfmodel` 这类系统模型时，用 `aicoding.modelConfigs.cache.assistant` 里的显示名
3. 当它是 `custom_model`（BYOK）时，用 `workspaceStorage/*/state.vscdb` 里 `chat.modelConfig.session.<会话ID>` 的 `custom:model_xxx`，再到 `globalStorage` 的 `aicoding.customModels` 取 `displayName` 与 `providerDisplayName`
4. 供应商取模型的提供方：BYOK 直接用 `providerDisplayName`；系统模型没有这个字段，按显示名前缀推断（DeepSeek / Qwen / Kimi / GLM / MiniMax），推断不出时记 `Qoder`
5. 价格优先匹配 CC Switch 的 `model_pricing`（去掉 `-pg`、`-cn`、`-lms` 等后缀后匹配），匹配不到就记 0，界面显示「未定价」

## 常见问题

**为什么供应商有时显示 `Alibaba` / `Moonshot`，而不是 `Qoder`？**
面板的「供应商」列语义是**模型厂商**，不是客户端。Qoder 内置模型本身不带厂商字段，所以按模型显示名前缀推断；推断不出的（如 `Auto`、`Ultimate` 这类路由模型）才回退成 `Qoder`。

**某个模型显示「未定价」怎么办？**
说明 CC Switch 的 `model_pricing` 里没有对应单价。在 CC Switch 里补上单价后重跑脚本，历史行的成本会自动回填（脚本按 `request_id` 做 upsert，不是简单插入）。

**应用筛选栏里为什么没有 Qoder？**
应用列表是 CC Switch 内置的，脚本无法注册新应用图标。数据本身是完整的，在「全部」下查看即可。

**会不会覆盖 CC Switch 自己的统计？**
不会。脚本只操作 `data_source='qoder_session'` 的行，其它应用的行既不读也不写，`--reset` 也只删这些行。

**CC Switch 升级后会不会坏？**
表结构变了的话脚本需要相应调整。因为数据是 upsert 而不是一次性导入，重跑脚本可以随时修正历史数据。

**计划任务会不会弹窗 / 闪黑框？**
不会。任务用 `pythonw.exe` 运行，子进程也带 `CREATE_NO_WINDOW`，全程无窗口。

## 已知限制

- Qoder 升级会重建本地库，历史最多只能追到重建当天
- 「速度」「首字」为空：Qoder 本地不存时延数据
- Qoder 内置模型的单价来自 CC Switch 价目表，是估算值，不等于 Qoder 实际扣的积分
- 应用筛选栏没有 Qoder 图标（应用列表是 CC Switch 内置的）
- CC Switch 的库结构若在后续版本改变，脚本需要相应调整
- 数据最差晚 1 分钟（计划任务周期），不是实时

## 安全与回滚

- Qoder 的库始终以 `mode=ro` 只读打开
- 写入前备份 `~/.cc-switch/cc-switch.db` 到 `~/.cc-switch/backups/qoder-import-<时间戳>/`，最多每小时一次、保留最近 5 份
- 只写 `data_source='qoder_session'` 的行，`--reset` 也只删这些行
- 出问题可直接用备份覆盖回去

## 目录结构

```
qoder-usage-bridge/
├── qoder_usage_bridge.py   # 主脚本，单文件，无依赖
├── install-task.ps1        # Windows 计划任务安装 / 卸载
├── README.md               # 简体中文（本文件）
├── README_EN.md            # English
├── LICENSE                 # MIT
├── .gitignore
└── .gitattributes          # 统一换行符，避免幽灵 diff
```

## License

[MIT](LICENSE)