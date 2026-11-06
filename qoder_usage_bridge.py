#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qoder-usage-bridge: feed Qoder IDE token usage into CC Switch's usage dashboard.

Qoder stores per-message token usage in its local SQLite database (plain JSON in
``chat_message.token_info``). CC Switch builds its usage dashboard from the
``proxy_request_logs`` table. This script bridges the two:

    Qoder local.db  ->  resolve model + price  ->  cc-switch.db proxy_request_logs
                        (app_type = qoder, data_source = qoder_session)

Only rows tagged ``data_source='qoder_session'`` are ever written or removed;
every other application's data is left untouched. Qoder's database is always
opened read-only. ``cc-switch.db`` is backed up before the first write.

Usage:
    python qoder_usage_bridge.py                     # dry run, prints what would be written
    python qoder_usage_bridge.py --apply             # one-shot import (backs up first)
    python qoder_usage_bridge.py --apply --allow-running
    python qoder_usage_bridge.py --watch             # keep in sync every 60s (daemon)
    python qoder_usage_bridge.py --list              # show what has been imported
    python qoder_usage_bridge.py --reset             # delete imported Qoder rows

Author: qoder-usage-bridge contributors
License: MIT
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict

__version__ = "1.0.0"

APP_TYPE = "qoder"
DATA_SOURCE = "qoder_session"
FALLBACK_PROVIDER = "Qoder"
ID_PREFIX = "qoder:"
LOG_MAX_BYTES = 5 * 1024 * 1024
BACKUP_KEEP = 5
BACKUP_REUSE_SECONDS = 3600
CREATE_NO_WINDOW = 0x08000000

# Qoder's built-in model table only carries a display name, so infer the model
# vendor from it. Order matters: the first matching prefix wins.
VENDOR_HINTS = (
    ("deepseek", "DeepSeek"),
    ("qwen", "Alibaba"),
    ("kimi", "Moonshot"),
    ("glm", "Zhipu"),
    ("minimax", "MiniMax"),
)


# --------------------------------------------------------------------------- paths


def qoder_user_dir() -> str:
    """Qoder's VS Code-style user directory on the current platform."""
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA", ""), "Qoder", "User")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Qoder/User")
    return os.path.expanduser("~/.config/Qoder/User")


def default_qoder_db() -> str:
    if os.name == "nt":
        return os.path.join(
            os.environ.get("APPDATA", ""), "Qoder", "SharedClientCache", "cache", "db", "local.db"
        )
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/Qoder/SharedClientCache/cache/db/local.db"
        )
    return os.path.expanduser("~/.config/Qoder/SharedClientCache/cache/db/local.db")


def default_ccs_db() -> str:
    return os.path.join(os.path.expanduser("~"), ".cc-switch", "cc-switch.db")


def default_log_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    return os.path.join(base, "qoder-usage-bridge", "bridge.log")


# --------------------------------------------------------------------------- logging


class Logger:
    def __init__(self, path: str | None = None, quiet: bool = False, with_time: bool = False) -> None:
        self.path = path
        self.quiet = quiet
        self.with_time = with_time
        if path:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
            except OSError:
                self.path = None

    def __call__(self, msg: str) -> None:
        prefix = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} " if self.with_time else ""
        line = prefix + msg
        if not self.quiet:
            try:
                print(line, flush=True)
            except (OSError, AttributeError):
                pass
        if not self.path:
            return
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > LOG_MAX_BYTES:
                os.replace(self.path, self.path + ".1")
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass


LOGGER = Logger()


def log(msg: str) -> None:
    LOGGER(msg)


# --------------------------------------------------------------------------- qoder side


def _item_value(con: sqlite3.Connection, key: str) -> str | None:
    try:
        row = con.execute("select value from ItemTable where key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row else None


def load_model_context() -> dict:
    """Read session -> model bindings, BYOK model details and system model names."""
    ctx: dict = {"session_model": {}, "custom": {}, "system": {}, "default": None}
    user_dir = qoder_user_dir()
    global_db = os.path.join(user_dir, "globalStorage", "state.vscdb")

    if os.path.exists(global_db):
        con = sqlite3.connect(f"file:{global_db}?mode=ro", uri=True)
        try:
            raw = _item_value(con, "aicoding.customModels")
            if raw:
                for item in json.loads(raw):
                    if isinstance(item, dict) and item.get("id"):
                        ctx["custom"][item["id"]] = item

            raw = _item_value(con, "aicoding.modelConfigs.cache.assistant")
            if raw:
                for item in json.loads(raw):
                    if isinstance(item, dict) and item.get("name"):
                        ctx["system"][item["name"]] = item.get("displayName") or item["name"]

            ctx["default"] = _item_value(con, "chat.modelConfig.assistant")
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            pass
        finally:
            con.close()

    pattern = os.path.join(user_dir, "workspaceStorage", "*", "state.vscdb")
    for path in glob.glob(pattern):
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            rows = con.execute(
                "select key, value from ItemTable where key like 'chat.modelConfig.session.%'"
            ).fetchall()
            con.close()
        except sqlite3.Error:
            continue
        for key, value in rows:
            ctx["session_model"][key.rsplit(".", 1)[-1]] = str(value)

    return ctx


def vendor_of(model_name: str) -> str:
    """Guess the model vendor from its display name."""
    lowered = (model_name or "").lower()
    for prefix, vendor in VENDOR_HINTS:
        if lowered.startswith(prefix):
            return vendor
    return FALLBACK_PROVIDER


def resolve_model(session_id: str, model_key: str, ctx: dict) -> dict:
    """Turn a Qoder model key into a readable model.

    The per-message ``model_info`` is the most accurate signal (e.g. ``qfmodel``);
    when it is ``custom_model`` or empty, fall back to the model bound to the
    session, which points at the BYOK model definition.
    """
    if model_key and model_key in ctx["system"]:
        display = ctx["system"][model_key]
        return {"display": display, "model_id": model_key, "provider": vendor_of(display)}

    raw = ctx["session_model"].get(session_id) or ctx["default"] or ""
    if raw.startswith("custom:"):
        info = ctx["custom"].get(raw.split(":", 1)[1])
        if info:
            provider = info.get("providerDisplayName") or info.get("provider") or FALLBACK_PROVIDER
            return {
                "display": info.get("displayName") or info.get("model") or raw,
                "model_id": info.get("model") or "",
                "provider": provider,
            }
    if raw and raw in ctx["system"]:
        display = ctx["system"][raw]
        return {"display": display, "model_id": raw, "provider": vendor_of(display)}

    fallback = model_key or raw or "unknown"
    return {"display": fallback, "model_id": fallback, "provider": vendor_of(fallback)}


def read_qoder_records(db_path: str, ctx: dict) -> list[dict]:
    """Read every assistant message that carries token usage."""
    if not os.path.exists(db_path):
        raise SystemExit(f"Qoder database not found: {db_path}")

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            """
            select id, session_id, token_info, model_info, gmt_create
            from chat_message
            where role = 'assistant' and token_info is not null and token_info <> ''
            order by gmt_create
            """
        ).fetchall()
    finally:
        con.close()

    records = []
    for msg_id, session_id, token_info, model_info, gmt_create in rows:
        try:
            tok = json.loads(token_info)
        except (TypeError, ValueError):
            continue

        prompt = max(int(tok.get("prompt_tokens") or 0), 0)
        output = max(int(tok.get("completion_tokens") or 0), 0)
        cached = max(int(tok.get("cached_tokens") or 0), 0)
        if prompt == 0 and output == 0:
            continue
        cached = min(cached, prompt)

        try:
            model_key = (json.loads(model_info) or {}).get("model_key") or ""
        except (TypeError, ValueError):
            model_key = ""

        info = resolve_model(session_id, model_key, ctx)
        records.append(
            {
                "request_id": ID_PREFIX + str(msg_id),
                "session_id": session_id,
                "model": info["display"],
                "model_id": info["model_id"],
                "provider": info["provider"],
                "input_tokens": prompt - cached,
                "cache_read_tokens": cached,
                "output_tokens": output,
                "created_at": int(int(gmt_create) // 1000),
            }
        )
    return records


# --------------------------------------------------------------------------- cc-switch side


def load_prices(con: sqlite3.Connection) -> dict[str, tuple[float, float, float]]:
    """cc-switch custom pricing: model_id -> (input, output, cache_read) USD per 1M tokens."""
    try:
        rows = con.execute(
            "select model_id, input_cost_per_million, output_cost_per_million, cache_read_cost_per_million "
            "from model_pricing"
        ).fetchall()
    except sqlite3.Error:
        return {}
    prices = {}
    for model_id, inp, out, cache in rows:
        try:
            prices[str(model_id)] = (float(inp), float(out), float(cache))
        except (TypeError, ValueError):
            continue
    return prices


def price_candidates(rec: dict) -> list[str]:
    names: list[str] = []

    def add(value: str | None) -> None:
        if value and value not in names:
            names.append(value)

    model_id = rec.get("model_id") or ""
    add(model_id)
    for suffix in ("-pg", "-cn", "-intl", "-lms"):
        if model_id.endswith(suffix):
            add(model_id[: -len(suffix)])
    add((rec.get("model") or "").strip().lower().replace(" ", "-").replace("_", "-"))
    add(rec.get("model"))
    return names


def cost_of(rec: dict, prices: dict[str, tuple[float, float, float]]) -> tuple[str, str, str, str, str | None]:
    matched = next((name for name in price_candidates(rec) if name in prices), None)
    if not matched:
        return "0", "0", "0", "0", None
    inp, out, cache = prices[matched]
    input_cost = rec["input_tokens"] / 1_000_000 * inp
    cache_cost = rec["cache_read_tokens"] / 1_000_000 * cache
    output_cost = rec["output_tokens"] / 1_000_000 * out
    total = input_cost + cache_cost + output_cost
    fmt = lambda value: f"{value:.6f}"
    return fmt(input_cost), fmt(output_cost), fmt(cache_cost), fmt(total), matched


def app_running() -> bool:
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq cc-switch.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            # The scheduled task runs under pythonw.exe, which has no console.
            # Without this flag Windows gives the console-subsystem child a new
            # console window, so a black box flashes on screen on every run.
            creationflags=CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
        ).stdout
        return "cc-switch.exe" in out.lower()
    except Exception:
        return False


def backup_db(ccs_db: str, reuse_within: float = BACKUP_REUSE_SECONDS) -> str:
    """Copy the CC Switch database, at most once per ``reuse_within`` seconds.

    A scheduled run every minute must not copy a multi-megabyte database on
    every tick, so the newest backup is reused until it is old enough.
    """
    root = os.path.join(os.path.dirname(ccs_db), "backups")
    os.makedirs(root, exist_ok=True)
    existing = sorted(
        (
            os.path.join(root, name)
            for name in os.listdir(root)
            if name.startswith("qoder-import-")
        ),
        key=os.path.getmtime,
    )
    if existing and time.time() - os.path.getmtime(existing[-1]) < reuse_within:
        return existing[-1]

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target_dir = os.path.join(root, f"qoder-import-{stamp}")
    os.makedirs(target_dir, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        src = ccs_db + suffix
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(target_dir, os.path.basename(src)))

    cut = len(existing) + 1 - BACKUP_KEEP
    for stale in existing[: max(0, cut)]:
        shutil.rmtree(stale, ignore_errors=True)
    return target_dir


def open_ccs(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise SystemExit(f"CC Switch database not found: {db_path}")
    con = sqlite3.connect(db_path, timeout=15)
    con.execute("pragma busy_timeout = 15000")
    return con


def existing_ids(con: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            "select request_id from proxy_request_logs where data_source = ?", (DATA_SOURCE,)
        )
    }


UPSERT_SQL = """
    insert into proxy_request_logs (
        request_id, provider_id, app_type, model, request_model,
        input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
        input_cost_usd, output_cost_usd, cache_read_cost_usd, cache_creation_cost_usd,
        total_cost_usd, latency_ms, first_token_ms, duration_ms, status_code,
        error_message, session_id, provider_type, is_streaming, cost_multiplier,
        created_at, data_source, pricing_model, input_token_semantics
    ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    on conflict(request_id) do update set
        provider_id = excluded.provider_id,
        model = excluded.model,
        request_model = excluded.request_model,
        input_tokens = excluded.input_tokens,
        output_tokens = excluded.output_tokens,
        cache_read_tokens = excluded.cache_read_tokens,
        input_cost_usd = excluded.input_cost_usd,
        output_cost_usd = excluded.output_cost_usd,
        cache_read_cost_usd = excluded.cache_read_cost_usd,
        total_cost_usd = excluded.total_cost_usd,
        pricing_model = excluded.pricing_model
"""


def upsert_records(con: sqlite3.Connection, records: list[dict]) -> tuple[int, int]:
    """Insert or refresh rows; returns (rows handled, rows matched to a price)."""
    prices = load_prices(con)
    handled = priced = 0
    for rec in records:
        input_cost, output_cost, cache_cost, total, pricing_model = cost_of(rec, prices)
        con.execute(
            UPSERT_SQL,
            (
                rec["request_id"], rec["provider"], APP_TYPE, rec["model"], rec["model_id"] or rec["model"],
                rec["input_tokens"], rec["output_tokens"], rec["cache_read_tokens"], 0,
                input_cost, output_cost, cache_cost, "0",
                total, 0, None, None, 200,
                None, rec["session_id"], DATA_SOURCE, 1, "1.0",
                rec["created_at"], DATA_SOURCE, pricing_model, 0,
            ),
        )
        handled += 1
        if pricing_model:
            priced += 1
    con.commit()
    return handled, priced


# --------------------------------------------------------------------------- commands


def summarize(records: list[dict], imported: set[str]) -> None:
    new = [rec for rec in records if rec["request_id"] not in imported]
    by_day: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for rec in records:
        day = dt.datetime.fromtimestamp(rec["created_at"]).strftime("%Y-%m-%d")
        slot = by_day[day]
        slot[0] += 1
        slot[1] += rec["input_tokens"]
        slot[2] += rec["cache_read_tokens"]
        slot[3] += rec["output_tokens"]

    log(f"Qoder records: {len(records)} total, {len(new)} new, {len(records) - len(new)} already imported")
    log("Per day (real = uncached input + output):")
    for day in sorted(by_day):
        count, inp, cache, out = by_day[day]
        log(f"  {day}  requests {count:4d}  input {inp:>10,}  cached {cache:>10,}  output {out:>7,}  real {inp + out:>9,}")

    models: dict[str, int] = defaultdict(int)
    for rec in records:
        models[rec["model"]] += 1
    log("Models: " + ", ".join(f"{name}={count}" for name, count in sorted(models.items(), key=lambda kv: -kv[1])))


def import_once(qoder_db: str, ccs_db: str) -> tuple[int, int]:
    ctx = load_model_context()
    records = read_qoder_records(qoder_db, ctx)
    con = open_ccs(ccs_db)
    try:
        return upsert_records(con, records)
    finally:
        con.close()


def cmd_list(ccs_db: str) -> None:
    con = open_ccs(ccs_db)
    try:
        rows = con.execute(
            """
            select date(created_at, 'unixepoch', 'localtime') d, model,
                   count(*), sum(input_tokens), sum(cache_read_tokens), sum(output_tokens), sum(total_cost_usd)
            from proxy_request_logs where data_source = ?
            group by d, model order by d, model
            """,
            (DATA_SOURCE,),
        ).fetchall()
    finally:
        con.close()

    if not rows:
        log("No Qoder rows imported yet.")
        return
    log("Imported Qoder rows:")
    for day, model, count, inp, cache, out, cost in rows:
        log(
            f"  {day}  {str(model)[:20]:20}  requests {count:4d}  input {inp or 0:>9,}  "
            f"cached {cache or 0:>9,}  output {out or 0:>7,}  cost ${float(cost or 0):.4f}"
        )


def cmd_reset(ccs_db: str, allow_running: bool) -> int:
    if not allow_running and app_running():
        log("CC Switch is running. Close it first, or pass --allow-running.")
        return 2
    log(f"Backup: {backup_db(ccs_db)}")
    con = open_ccs(ccs_db)
    try:
        cur = con.execute("delete from proxy_request_logs where data_source = ?", (DATA_SOURCE,))
        con.commit()
        log(f"Deleted {cur.rowcount} imported Qoder rows.")
    finally:
        con.close()
    return 0


def cmd_watch(qoder_db: str, ccs_db: str, interval: float, poll: float, debounce: float) -> int:
    """Keep Qoder in sync: heartbeat every ``interval`` seconds, plus near-real-time
    pickup when Qoder writes to its database."""
    log(f"[watch] started (version {__version__}, interval {interval:.0f}s, poll {poll:.0f}s)")
    log(f"[watch] qoder : {qoder_db}")
    log(f"[watch] ccswitch: {ccs_db}")

    backup_done = False
    last_import = 0.0
    last_mtime: float | None = None
    changed_at: float | None = None

    while True:
        try:
            now = time.time()
            try:
                mtime: float | None = os.path.getmtime(qoder_db)
            except OSError:
                mtime = last_mtime

            if mtime != last_mtime:
                last_mtime = mtime
                changed_at = now

            due_change = changed_at is not None and now - changed_at >= debounce
            due_heartbeat = now - last_import >= interval

            if due_change or due_heartbeat:
                if not backup_done:
                    log(f"[watch] backup: {backup_db(ccs_db)}")
                    backup_done = True
                handled, priced = import_once(qoder_db, ccs_db)
                last_import = time.time()
                changed_at = None
                reason = "change" if due_change else "heartbeat"
                log(f"[watch] synced ({reason}): {handled} rows, {priced} priced")
        except KeyboardInterrupt:
            log("[watch] stopped")
            return 0
        except Exception as exc:  # keep the daemon alive on transient locks
            log(f"[watch] error: {type(exc).__name__}: {exc}")
        time.sleep(poll)


def main(argv: list[str] | None = None) -> int:
    # pythonw.exe, which the scheduled task uses, has no console at all.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        prog="qoder-usage-bridge",
        description="Feed Qoder IDE token usage into CC Switch's usage dashboard.",
    )
    parser.add_argument("--qoder-db", default=default_qoder_db(), help="path to Qoder local.db")
    parser.add_argument("--ccswitch-db", default=default_ccs_db(), help="path to cc-switch.db")
    parser.add_argument("--apply", action="store_true", help="write to cc-switch.db (default: dry run)")
    parser.add_argument("--allow-running", action="store_true", help="write even while CC Switch is running")
    parser.add_argument("--watch", action="store_true", help="run as a daemon and keep syncing")
    parser.add_argument("--interval", type=float, default=60, help="heartbeat sync interval in seconds")
    parser.add_argument("--poll", type=float, default=5, help="watch poll interval in seconds")
    parser.add_argument("--debounce", type=float, default=3, help="wait after a database change before syncing")
    parser.add_argument("--log", default=None, help="log file path (default: %%LOCALAPPDATA%% log in watch mode)")
    parser.add_argument("--quiet", action="store_true", help="do not print to stdout (watch mode)")
    parser.add_argument("--list", action="store_true", help="show imported rows")
    parser.add_argument("--reset", action="store_true", help="delete imported Qoder rows")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    global LOGGER
    if args.watch or args.quiet:
        LOGGER = Logger(args.log or default_log_path(), quiet=args.quiet, with_time=True)
    else:
        LOGGER = Logger(args.log, quiet=False, with_time=bool(args.log))

    if args.list:
        cmd_list(args.ccswitch_db)
        return 0
    if args.reset:
        return cmd_reset(args.ccswitch_db, args.allow_running)

    if args.watch:
        try:
            return cmd_watch(args.qoder_db, args.ccswitch_db, args.interval, args.poll, args.debounce)
        except KeyboardInterrupt:
            log("[watch] stopped by user")
            return 0

    ctx = load_model_context()
    records = read_qoder_records(args.qoder_db, ctx)
    con = open_ccs(args.ccswitch_db)
    try:
        summarize(records, existing_ids(con))
        if not args.apply:
            log("\nDry run, nothing written. Re-run with --apply to write.")
            return 0
        if not args.allow_running and app_running():
            log("\nCC Switch is running. Close it first, or pass --allow-running.")
            return 2

        log(f"\nBackup: {backup_db(args.ccswitch_db)}")
        handled, priced = upsert_records(con, records)
        log(f"Done: {handled} Qoder rows written, {priced} matched a price.")
        log("Refresh the usage dashboard in CC Switch to see them (use the 'All' filter).")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
