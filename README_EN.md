# qoder-usage-bridge

English | [简体中文](README.md)

[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)]()
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)]()

Feed [Qoder IDE](https://qoder.com) token usage into [CC Switch](https://ccswitch.io)'s usage dashboard.

> Unofficial tool, not affiliated with Qoder or CC Switch. Writing into a third-party application's database carries risk — back it up first and evaluate at your own discretion.

---

## Why this exists

CC Switch's usage dashboard only covers the apps it ships support for (Claude Code, Codex, Gemini CLI, OpenCode, Kimi Code, DeepSeek Harness, Antigravity, ...). Qoder is not on that list: CC Switch offers no external data-source hook, and it does not understand Qoder's session logs.

Qoder itself, however, stores the per-message token usage of every assistant reply **in plaintext** inside `chat_message.token_info` in its local SQLite database. This script does one thing: move those numbers into CC Switch's table layout, so Qoder shows up in the dashboard.

## What it shows in the dashboard

| Column | Source |
| --- | --- |
| Requests | Number of assistant messages that carry token usage in `chat_message` |
| New input | `prompt_tokens - cached_tokens` |
| Cache hits | `cached_tokens` |
| Output | `completion_tokens` |
| Model | Resolved to a real name such as `DeepSeek-Flash` or `Qwen3.8-Flash`, not `custom_model` |
| Provider | Model vendor: `DeepSeek` / `Alibaba` / `Moonshot` / `Zhipu` / `MiniMax`; falls back to `Qoder` when unknown |
| Cost | Priced from CC Switch's own `model_pricing` table (USD) |
| Speed / first token | Empty — Qoder does not store latency data locally |

## How it works

```
Qoder local.db (chat_message.token_info, read-only)
        │  prompt / cached / completion
        ▼
Model resolution: chat.modelConfig.session.* -> aicoding.customModels / system model table
        │  display name + provider
        ▼
Pricing: cc-switch model_pricing
        │  USD / 1M tokens
        ▼
cc-switch.db (proxy_request_logs, data_source='qoder_session')
        ▼
CC Switch "Usage" -> All
```

Only rows tagged `data_source='qoder_session'` are added or removed; other applications' data is never touched.

## Requirements

- Python 3.9+, **no third-party dependencies**
- Qoder IDE (its local database is SQLite)
- CC Switch 4.0.x (tested on 4.0.26)
- Verified on Windows; the macOS / Linux path detection is implemented but has not been verified on real hardware

## Install

```bash
git clone https://github.com/si6moonlxy-cyber/qoder-usage-bridge.git
cd qoder-usage-bridge
```

## Usage

```bash
# 1. Dry run: print only, write nothing
python qoder_usage_bridge.py

# 2. Actually write (backs up cc-switch.db on first run)
python qoder_usage_bridge.py --apply

# Writing while CC Switch is running is fine
python qoder_usage_bridge.py --apply --allow-running

# 3. Inspect what has been imported
python qoder_usage_bridge.py --list

# 4. Remove everything this tool imported
python qoder_usage_bridge.py --reset
```

Then go back to CC Switch's usage dashboard and hit refresh. There is no Qoder icon in the app filter, so look under "All".

### Command-line options

| Option | Description |
| --- | --- |
| `--qoder-db` | Path to Qoder's `local.db`; auto-detected per platform by default |
| `--ccswitch-db` | Path to `cc-switch.db`; defaults to `~/.cc-switch/cc-switch.db` |
| `--apply` | Actually write (dry run otherwise) |
| `--allow-running` | Write even while CC Switch is running |
| `--watch` | Run as a daemon: heartbeat plus sync on database changes |
| `--interval` | Heartbeat interval, 60 seconds by default |
| `--poll` | Change polling interval, 5 seconds by default |
| `--debounce` | Delay after detecting a change before syncing, 3 seconds by default |
| `--log` | Log file path |
| `--quiet` | Do not print to stdout; log to file instead |
| `--list` | Print imported rows |
| `--reset` | Delete every imported Qoder row |
| `--version` | Print the version |

## Automatic sync

### Option A: Windows scheduled task (recommended)

CC Switch leaves no hook for external scripts, and its built-in sync only understands the session logs of the apps it supports. So the work is handed to a Windows scheduled task that runs once a minute: registered with `schtasks`, **no administrator rights needed**, and **no resident process**.

```powershell
powershell -ExecutionPolicy Bypass -File .\install-task.ps1
# Every 2 minutes instead
powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -Interval 2
# Uninstall
powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -Uninstall
```

- The task is named `QoderUsageBridge`; inspect it with `schtasks /query /tn QoderUsageBridge`
- It runs silently as `pythonw.exe --apply --allow-running --quiet`, so no window appears
- Child processes spawned by the script (such as `tasklist`) use `CREATE_NO_WINDOW`, so nothing flashes and focus is never stolen
- Log file: `%LOCALAPPDATA%\qoder-usage-bridge\bridge.log` (rotated past 5 MB)
- Backups happen at most once per hour, keeping the five newest, so a per-minute task cannot flood the disk

Then set the usage dashboard refresh interval to 1 minute in CC Switch (the `usageDashboardRefreshIntervalMs` key in `settings.json`); the panel refreshes itself and looks indistinguishable from the built-in statistics.

### Option B: Resident process (second-level latency)

```bash
python qoder_usage_bridge.py --watch
```

- Heartbeat sync every 60 seconds (`--interval` configurable)
- As soon as Qoder writes to its database, the change is picked up within seconds (`--debounce` configurable)
- Best when you want the freshest data and do not mind one extra background process

## How models and vendors are resolved

1. `chat_message.token_info` provides `prompt_tokens` / `cached_tokens` / `completion_tokens`
2. When `model_info.model_key` is a system model such as `qfmodel`, the display name comes from `aicoding.modelConfigs.cache.assistant`
3. When it is `custom_model` (BYOK), the script reads `custom:model_xxx` from `chat.modelConfig.session.<sessionId>` in `workspaceStorage/*/state.vscdb`, then looks up `displayName` and `providerDisplayName` in `aicoding.customModels` from `globalStorage`
4. The vendor is the model's provider: BYOK models use `providerDisplayName` directly; system models have no such field, so the vendor is inferred from the display-name prefix (DeepSeek / Qwen / Kimi / GLM / MiniMax), falling back to `Qoder`
5. Prices are matched against CC Switch's `model_pricing` after stripping suffixes such as `-pg`, `-cn` and `-lms`. No match means a cost of 0 and "unpriced" in the UI

## FAQ

**Why does the provider sometimes read `Alibaba` or `Moonshot` instead of `Qoder`?**
The provider column means the **model vendor**, not the client. Qoder's built-in models carry no vendor field, so the vendor is inferred from the model's display name; only models that cannot be inferred (routing models such as `Auto` or `Ultimate`) fall back to `Qoder`.

**What if a model shows as unpriced?**
CC Switch's `model_pricing` has no unit price for it. Add the price in CC Switch and run the script again: historical rows are backfilled automatically, because the script upserts by `request_id` rather than inserting blindly.

**Why is there no Qoder icon in the app filter?**
The app list is built into CC Switch; a script cannot register a new icon. The data itself is complete — view it under "All".

**Will it overwrite CC Switch's own statistics?**
No. The script only touches rows with `data_source='qoder_session'`; rows from other apps are neither read nor written, and `--reset` removes only those rows.

**Will it break after a CC Switch update?**
If the table layout changes, the script will need a matching update. Because rows are upserted rather than imported once, re-running the script can always correct historical data.

**Does the scheduled task pop up windows or flash a black box?**
No. The task runs under `pythonw.exe` and its child processes use `CREATE_NO_WINDOW`, so there is no window at any point.

## Known limitations

- Upgrading Qoder recreates its local database, so history only reaches back to the day of the last recreation
- "Speed" and "first token" stay empty: Qoder does not store latency data locally
- Prices for Qoder's bundled models come from CC Switch's price table; they are estimates, not the credits Qoder actually deducts
- The app filter has no Qoder icon (the app list is built into CC Switch)
- If CC Switch's database layout changes in a future release, the script needs a matching adjustment
- Data can be up to 1 minute stale (the scheduled-task interval); it is not real time

## Safety and rollback

- Qoder's database is always opened read-only (`mode=ro`)
- Before writing, `~/.cc-switch/cc-switch.db` is backed up to `~/.cc-switch/backups/qoder-import-<timestamp>/`, at most once per hour, keeping the five newest
- Only rows with `data_source='qoder_session'` are written, and `--reset` deletes only those rows
- If anything goes wrong, restore the backup over the database

## Repository layout

```
qoder-usage-bridge/
├── qoder_usage_bridge.py   # main script, single file, no dependencies
├── install-task.ps1        # install / uninstall the Windows scheduled task
├── README.md               # Simplified Chinese
├── README_EN.md            # English (this file)
├── LICENSE                 # MIT
├── .gitignore
└── .gitattributes          # normalize line endings, no phantom diffs
```

## License

[MIT](LICENSE)
