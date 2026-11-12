# Changelog / 更新日志

本文件记录 qoder-usage-bridge 的所有重要变更。版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)，日期为发布日。

All notable changes to this project are documented here. Versions follow [SemVer](https://semver.org/).

---

## v1.1.0 — 2026-11-12

中文 | [English](#v110--2026-11-12-english)

### 背景：Qoder 会重建数据库，并把旧会话物理删掉

Qoder 升级时会重建 `%APPDATA%\Qoder\SharedClientCache\cache\db\local.db`。重建之后：

- 旧会话被**物理删除**，库里只剩重建之后的记录（实测某次重建后剩 493 条、全为当月）；
- **空闲页也扫不出残留**——对 `local.db` 与 `-wal` 做原始字节扫描，去重后得到的仍然是现存行数，没有旧记录可打捞；
- Qoder 的日志里不计费 token（只有 `usedTokens` 这类上下文占用），本地没有任何可回推的来源。

在这之前，bridge 只能导入「Qoder 当前库还在的记录」，所以 CC Switch 面板里会出现某个月整段消失、且无法找回。v1.1.0 起，每次同步都会把记录抄进 bridge 自己的账本。

### 新增：两层本地备份

```
Qoder local.db（只读）
      │
      ├─→ 账本 %LOCALAPPDATA%\qoder-usage-bridge\qoder-ledger.db   持续追加，按 message id 去重
      │        └─→ 每日快照 snapshots\qoder-ledger-YYYYMMDD.db       每天一份，默认留 14 份
      │
      └─→ cc-switch.db（app_type=qoder, data_source=qoder_session）
```

- **账本**：一个持续追加的 SQLite 文件（不是每天新开），存**原始** `prompt_tokens` / `cached_tokens` / `completion_tokens`，加上已解析的 `model` / `model_id` / `provider` 和毫秒时间戳。按 Qoder 的 message id 主键去重，只随新消息增长。
- **每日快照**：每天一个带日期的文件，复制的是账本（用 SQLite backup API，非文件硬拷），默认保留 14 份、滚动删除；同一天重复运行不会重写。
- Qoder 的库始终以 `mode=ro` 只读打开，**从不**被写入；`--reset` 只删 CC Switch 里的行，账本不动。

体积与耗时（实测）：

| 形态 | 单日磁盘 | 写入耗时 |
|---|---|---|
| 账本（追加写入同一文件） | 493 行 = **120 KB**，只随新消息缓慢增长 | **< 10 ms** |
| 每日快照（每天一个新文件） | **120 KB/天**，14 份约 2 MB | 复制即完成 |

### 新增（可选）：`--snapshot-full` 整库留底

默认**关闭**。需要连 Qoder 完整会话内容一起留底时开启：

```bash
python qoder_usage_bridge.py --apply --allow-running --snapshot-full
```

每天额外把 `local.db`（40.0 MB）+ `-wal`（5.4 MB）拷进 `snapshots\qoder-raw-YYYYMMDD\`，约 **45 MB/天**，保留 14 份约 **640 MB**。多数场景用不到：那 40 MB 里绝大部分是 Qoder 的记忆与向量表，与用量无关；「重建窗口内丢数据」这一风险由每分钟一次的计划任务加账本已经覆盖。

### 新增命令

```bash
# 按账本查月度用量（Qoder 重建之后照样能查），--month 可选
python qoder_usage_bridge.py --report
python qoder_usage_bridge.py --report --month 2026-11

# 把账本回放进 CC Switch（Qoder 丢了历史、或 CC Switch 那边被裁掉之后）
python qoder_usage_bridge.py --restore-from-ledger --allow-running
python qoder_usage_bridge.py --restore-from-ledger --allow-running --month 2026-11
```

- `--report` 按账本输出：每月请求数、`prompt` / `cached` / `completion` / `fresh` / `real`，并附按模型的分组。
- `--restore-from-ledger` 按 `request_id` 做 upsert，重复执行不会翻倍。

### 其他新增参数

`--ledger`、`--no-ledger`、`--snapshot-dir`、`--no-snapshot`、`--snapshot-keep`、`--month`。

### 验证

- 副本库上清空 493 行 qoder 记录 → `--restore-from-ledger` → 行数与 token 汇总（fresh 777,219 / cached 53,710,055 / completion 383,308）与原值**完全一致**，其它应用的 3192 行一行未动；
- 连续两次 `--apply`：第二次账本 `added=0`，当日快照不重写；
- 真实环境：计划任务在升级后的下一轮即自动建出账本与当日快照，`--report` 正常输出。

### 升级

- **不需要重装计划任务**：既有的 `QoderUsageBridge`（`--apply --allow-running --quiet`）会自动开始写账本与快照；
- 账本从这一版首次运行起开始积累，**之前已被 Qoder 重建掉的月份仍然无法找回**（只能查 Qoder 账户服务端的用量页）；
- 向后兼容：`--list` / `--reset` / `--watch` 行为不变。

### v1.1.0 — 2026-11-12 (English)

**Local backup for Qoder usage history.** Qoder recreates `local.db` on upgrade and physically deletes old conversations (measured: only post-rebuild rows remain, and free pages hold no leftovers), so the dashboard could only ever show what the current database still held.

Every sync now appends the raw token counts to a durable ledger keyed by Qoder's message id (`%LOCALAPPDATA%\qoder-usage-bridge\qoder-ledger.db`, one appended file, 493 rows ≈ 120 KB, < 10 ms to write), writes one dated compact copy per day (≈120 KB/day, 14 kept), and can replay everything into CC Switch.

- `--report [--month YYYY-MM]`: monthly totals straight from the ledger, with a per-model breakdown.
- `--restore-from-ledger [--month]`: upsert by `request_id`, so repeating it never doubles anything.
- `--snapshot-full` (opt-in, off by default): also copies Qoder's whole `local.db` daily (≈45 MB/day, ≈640 MB for 14 copies) for full-conversation restores. Most of that database is Qoder's memory and embedding tables, unrelated to usage.
- Qoder's database stays read-only; the ledger and snapshots live under `%LOCALAPPDATA%` and never touch Qoder's tree.

Verified by wiping all 493 rows in a copy of `cc-switch.db` and replaying: row count and token totals matched exactly, and other apps' rows were untouched. No reinstall is needed — the existing scheduled task picks the change up on its next run. Months erased before this version cannot be recovered locally; use the Qoder account's server-side usage page for those.

---

## v1.0.0 — 2026-11-06

首个公开版本：把 Qoder IDE 的 token 用量接进 CC Switch 的「用量统计」面板。

- 从 Qoder 本地 SQLite（`chat_message.token_info`）读取每条助手消息的 `prompt_tokens` / `cached_tokens` / `completion_tokens`
- 解析模型显示名与供应商（系统模型走 `aicoding.modelConfigs.cache.assistant`，BYOK 走 `aicoding.customModels`）
- 按 CC Switch 的 `model_pricing` 计价
- 只写 `data_source='qoder_session'` 的行；Qoder 库以 `mode=ro` 只读打开；写前备份 `cc-switch.db`
- 三种运行方式：预演、一次性 `--apply`、常驻 `--watch`；另有 `install-task.ps1` 注册每分钟的 Windows 计划任务
- 中英文 README 与 MIT 许可

### v1.0.0 — 2026-11-06 (English)

Initial public release: feeds Qoder IDE token usage into CC Switch's usage dashboard.

- Reads per-message `prompt_tokens` / `cached_tokens` / `completion_tokens` from Qoder's local SQLite (`chat_message.token_info`)
- Resolves the model display name and vendor (system models via `aicoding.modelConfigs.cache.assistant`, BYOK via `aicoding.customModels`)
- Prices rows against CC Switch's `model_pricing`
- Writes only `data_source='qoder_session'` rows; opens Qoder's database read-only; backs up `cc-switch.db` before writing
- Dry run, one-shot `--apply`, resident `--watch`, plus `install-task.ps1` for a per-minute Windows scheduled task
- Bilingual READMEs under the MIT license
