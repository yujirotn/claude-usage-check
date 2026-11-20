#!/usr/bin/env python3
"""Claude Code と Codex の使用量を数えて、共有フォルダに集め、PDF レポートにする。

会話の中身は読み出さず、送らない。数えるのは件数・トークン数・モデル名・日付だけ。
外部への通信は一切しない（書き込むのは手元の設定ファイルと、利用者が指定した同期フォルダだけ）。
Python 3.9 以上・標準ライブラリだけで動く。

  python3 usage_check.py find-drives                 # 同期フォルダの候補を出す
  python3 usage_check.py setup --name 山田 --dest <格納先> [--projects]
  python3 usage_check.py retention [--days 90]        # ログの保存日数を延ばす
  python3 usage_check.py collect [--dry-run]          # 数えて格納先に書く
  python3 usage_check.py report --dest <格納先> [--from 2026-11-01 --to 2026-11-30]
"""
import argparse
import csv
import glob
import hashlib
import html
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime

TOOL_VERSION = "0.5.0"
SCHEMA = 1
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".claude-usage-check")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
SUBDIR = "claude-usage"  # 格納先の中に作るフォルダ
YEN = None  # --yen で 1ドルあたりの円を渡すと、金額を円で表示する


def fmt_money(usd):
    if YEN:
        return "¥{:,}".format(int(round((usd or 0) * YEN)))
    return "${:,.2f}".format(usd or 0)


# ---------------------------------------------------------------- 共通

def claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def codex_dir():
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, RecursionError):
        return default


def write_json_atomic(path, data, mode=0o644, sort_keys=True, indent=1):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent, sort_keys=sort_keys)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 共有フォルダの JSON の検証
# 共有フォルダは他のメンバーも書ける。読む JSON は必ずここを通し、形の合わないものは丸ごと捨てる。

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NAME_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,80}$")
INV_KEYS = ("skills_user", "agents_user", "skills_project", "agents_project", "skills_plugin",
            "agents_plugin", "skills_synced", "plugins", "projects_scanned")
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h", "responses")
CODEX_TOKENS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
CODEX_COUNTS = ("sessions", "prompts", "responses", *CODEX_TOKENS,
                "commands", "file_edits", "mcp", "subagents")
CODEX_REVIEW_COUNTS = ("review_turns", "review_tokens")
CODEX_FIELDS = (*CODEX_COUNTS, *CODEX_REVIEW_COUNTS)
HARNESS_KEYS = ("projects", "claude_md_files", "claude_md_lines", "agents_md_files", "agents_md_lines",
                "hooks", "mcp_servers", "codex_skills")
WEEKDAYS = ("月", "火", "水", "木", "金", "土", "日")
HOURS = tuple(str(i) for i in range(24))
CODEX_REVIEW = "codex-auto-review"


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10 ** 15


def _num(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 10 ** 9)


def clean_text(v, limit):
    if not isinstance(v, str):
        return None
    v = re.sub(r"[\x00-\x1f\x7f\ud800-\udfff<>]", "", v).strip()
    return v[:limit] or None


def valid_day(value):
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def checked_counts(src, keys):
    if not isinstance(src, dict) or not all(_int(src.get(key)) for key in keys):
        raise ValueError
    return {key: src[key] for key in keys}


def validate_work(src):
    work = checked_counts(src, ("prompts", "responses", "file_edits"))
    seconds = src.get("seconds")
    auto = src.get("auto_prompts")
    heatmap = src.get("heatmap")
    if not _num(seconds) or (auto is not None and (not _int(auto) or auto > work["prompts"])):
        raise ValueError
    if (not isinstance(heatmap, dict)
            or not all(h in HOURS and _int(n) for h, n in heatmap.items())
            or sum(heatmap.values()) > work["prompts"]):
        raise ValueError
    return dict(work, seconds=seconds, auto_prompts=auto, heatmap=heatmap)


def validate_codex(src):
    result = checked_counts(src, CODEX_COUNTS)
    if any(key in src for key in CODEX_REVIEW_COUNTS):
        result.update(checked_counts(src, CODEX_REVIEW_COUNTS))
    else:
        result.update(dict.fromkeys(CODEX_REVIEW_COUNTS, 0))
    models, mcp = src.get("models"), src.get("mcp_servers")
    if not isinstance(models, dict) or not isinstance(mcp, dict):
        raise ValueError
    result["models"] = {}
    for name, counts in models.items():
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise ValueError
        result["models"][name] = checked_counts(counts, ("responses", *CODEX_TOKENS))
    if not all(isinstance(name, str) and clean_text(name, 80) == name and _int(n)
               for name, n in mcp.items()):
        raise ValueError
    result["mcp_servers"] = mcp
    result["work"] = validate_work(src.get("work"))
    if (any(result["work"][key] > result[key] for key in ("prompts", "responses", "file_edits"))
            or sum(mcp.values()) > result["mcp"]):
        raise ValueError
    return result


def validate_member(d):
    """形が正しければ検証済みの dict を、そうでなければ (None, 理由) を返す。"""
    if not isinstance(d, dict) or not _int(d.get("schema")) or d["schema"] != SCHEMA:
        return None, "形式の版が違う"
    member = clean_text(d.get("member"), 40)
    if not member:
        return None, "名前がない"
    days_in = d.get("days")
    if not isinstance(days_in, dict):
        return None, "日別の数字がない"
    days = {}
    for day, r in days_in.items():
        if not valid_day(day) or not isinstance(r, dict):
            return None, "日付の形が違う"
        if not all(_int(r.get(k, 0)) for k in ("sessions", "prompts", "responses")) or not _num(r.get("cost_usd", 0)):
            return None, "数字の形が違う"
        models = {}
        model_data = r.get("models", {})
        if model_data is None:
            model_data = {}
        if not isinstance(model_data, dict):
            return None, "モデルの内訳の形が違う"
        for mn, t in model_data.items():
            if not (isinstance(mn, str) and NAME_RE.match(mn) and isinstance(t, dict)):
                return None, "モデル名の形が違う"
            if not all(_int(t.get(k, 0)) for k in TOKEN_KEYS):
                return None, "トークン数の形が違う"
            c = t.get("cost_usd")
            if c is not None and not _num(c):
                return None, "金額の形が違う"
            models[mn] = dict({k: t.get(k, 0) for k in TOKEN_KEYS}, cost_usd=c)
        item = {"sessions": r.get("sessions", 0), "prompts": r.get("prompts", 0),
                "responses": r.get("responses", 0), "cost_usd": r.get("cost_usd", 0), "models": models}
        tools_in = r.get("tools")
        if tools_in is not None:
            if not isinstance(tools_in, dict):
                return None, "道具の内訳の形が違う"
            tools = {}
            for kind, names in tools_in.items():
                if kind not in TOOL_KINDS or not isinstance(names, dict):
                    return None, "道具の内訳の形が違う"
                out = {}
                for k, v in names.items():
                    name = clean_text(k, 80)
                    if not name or not _int(v):
                        return None, "道具の内訳の形が違う"
                    out[name] = v
                tools[kind] = out
            item["tools"] = tools
        for key in ("entrypoints", "projects"):
            src = r.get(key)
            if src is None:
                continue
            if not isinstance(src, dict):
                return None, "内訳の形が違う"
            out = {}
            for k, v in src.items():
                name = clean_text(k, 80)
                if not name or not _int(v):
                    return None, "内訳の形が違う"
                out[name] = v
            item[key] = out
        if "codex" in r and not any(k in r for k in (*item, "work")):
            item = {}
        try:
            if "codex" in r:
                item["codex"] = validate_codex(r["codex"])
            if "work" in r:
                item["work"] = validate_work(r["work"])
        except ValueError:
            return None, "Codex または働き方の数字の形が違う"
        days[day] = item
    inv = None
    if d.get("inventory") is not None:
        src = d["inventory"]
        if not isinstance(src, dict):
            return None, "入っている数の形が違う"
        inv = {}
        for k in INV_KEYS:
            v = src.get(k, 0)
            if not _int(v):
                return None, "入っている数の形が違う"
            inv[k] = v
        a = src.get("as_of")
        inv["as_of"] = a if isinstance(a, str) and DATE_RE.match(a) else ""
    harness = None
    if "harness" in d:
        try:
            harness = checked_counts(d["harness"], HARNESS_KEYS)
            snapshot = d["harness"].get("as_of")
            if not valid_day(snapshot):
                raise ValueError
            harness["as_of"] = snapshot
        except ValueError:
            return None, "ハーネスの整備度の形が違う"
    as_of = d.get("prices_as_of")
    updated = d.get("updated_at")
    return {"schema": SCHEMA, "member": member, "days": days,
            "prices_as_of": as_of if isinstance(as_of, str) and DATE_RE.match(as_of) else "?",
            "updated_at": updated[:25] if isinstance(updated, str) and DATE_RE.match(updated[:10]) else "",
            "tool_version": clean_text(d.get("tool_version"), 20) or "",
            "inventory": inv, "harness": harness}, None


def safe_filename(name):
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", name.strip())
    return s[:60] or "member"


def die(msg):
    print("エラー: " + msg, file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- 単価

class Prices:
    def __init__(self):
        p = load_json(os.path.join(HERE, "prices.json"), {}) or {}
        self.models = p.get("models", {})
        self.d = p.get("defaults", {"cache_read_ratio": 0.1, "cache_write_5m_ratio": 1.25, "cache_write_1h_ratio": 2.0})
        self.as_of = p.get("as_of", "?")

    def cost(self, model, t):
        m = self.models.get(model)
        if m is None:  # 日付つきの別名などは前方一致で拾う
            for k, v in self.models.items():
                if model.startswith(k):
                    m = v
                    break
        if m is None:
            return None
        inp, out = m["input"], m["output"]
        cr = m.get("cache_read", inp * self.d["cache_read_ratio"])
        w5 = m.get("cache_write_5m", inp * self.d["cache_write_5m_ratio"])
        w1 = m.get("cache_write_1h", inp * self.d["cache_write_1h_ratio"])
        return (t["input"] * inp + t["output"] * out + t["cache_read"] * cr
                + t["cache_write_5m"] * w5 + t["cache_write_1h"] * w1) / 1e6


# ---------------------------------------------------------------- 数える

def empty_tokens():
    return {"input": 0, "output": 0, "cache_read": 0, "cache_write_5m": 0, "cache_write_1h": 0}


def local_time(ts):
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except (ValueError, OverflowError, OSError):
        return None


def as_dict(value):
    return value if isinstance(value, dict) else {}


def token_count(usage, key):
    value = usage.get(key)
    return value if _int(value) else 0


def jsonl_records(path, pattern=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if pattern and not pattern.search(line):
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def empty_work(auto=None):
    return {"prompts": 0, "responses": 0, "file_edits": 0, "seconds": 0,
            "auto_prompts": auto, "heatmap": defaultdict(int)}


def record_prompt(work, timestamp, automatic):
    work["prompts"] += 1
    if timestamp is not None:
        work["heatmap"][str(timestamp.hour)] += 1
    if automatic is not None:
        work["auto_prompts"] = (work["auto_prompts"] or 0) + int(automatic)


def add_work_time(timestamps, days, work_for_day):
    for (_, day), times in timestamps.items():
        times.sort()
        work_for_day(days[day])["seconds"] += sum(
            end - start for start, end in zip(times, times[1:]) if end - start <= 300
        )


CLAUDE_RECORD = re.compile(r'"type"\s*:\s*"(user|assistant)"')
CLAUDE_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def collect_days(include_projects):
    """~/.claude/projects の JSONL から日別の数字を作る。会話の本文は保持しない。"""
    root = os.path.join(claude_dir(), "projects")
    files = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    prices = Prices()
    days = defaultdict(lambda: {
        "sessions": set(), "prompts": 0, "responses": 0,
        "models": defaultdict(lambda: dict(empty_tokens(), responses=0)),
        "entrypoints": defaultdict(int), "projects": defaultdict(int),
        "tools": {k: defaultdict(int) for k in TOOL_KINDS},
        "work": empty_work(0),
    })
    seen = set()
    seen_tools = set()
    cwds = set()  # 入っている数を数えるための作業フォルダ（外には出さない）
    first_day = {}  # セッションは始まった日に1回だけ数える
    timestamps = defaultdict(list)
    for path in files:
        for d in jsonl_records(path, CLAUDE_RECORD):
            t = d.get("type")
            timestamp = local_time(d.get("timestamp"))
            if t not in ("user", "assistant") or timestamp is None:
                continue
            day = timestamp.date().isoformat()
            rec = days[day]
            side = bool(d.get("isSidechain"))
            sid = d.get("sessionId")
            sid = sid if isinstance(sid, str) else ""
            timestamps[(sid or path, day)].append(timestamp.timestamp())
            if sid and not side and (sid not in first_day or day < first_day[sid]):
                first_day[sid] = day
            msg = as_dict(d.get("message"))
            cwd = d.get("cwd")
            if isinstance(cwd, str) and cwd:
                cwds.add(cwd)
            else:
                cwd = None
            if t == "user":
                # 人が打った発言だけ数える（ツール結果・自動挿入・サブエージェントは除く）
                if side or d.get("isMeta") or not isinstance(msg.get("content"), str):
                    continue
                rec["prompts"] += 1
                cm = re.search(r"<command-name>/?([^<]{1,80})</command-name>", msg["content"])
                if cm:
                    rec["tools"]["command"][tool_name(cm.group(1).strip(), include_projects)] += 1
                entrypoint = clean_text(d.get("entrypoint"), 80) or "unknown"
                rec["entrypoints"][entrypoint] += 1
                record_prompt(rec["work"], timestamp, entrypoint.startswith("sdk-"))
                if include_projects and cwd:
                    project = clean_text(os.path.basename(cwd.rstrip("/\\")), 80)
                    if project:
                        rec["projects"][project] += 1
                continue
            model = msg.get("model")
            model = model if isinstance(model, str) else "unknown"
            if model.startswith("<"):  # <synthetic> など API を通っていない応答
                continue
            if not re.match(r"^claude-[a-z0-9.\-]{1,60}$", model):
                model = "other"  # Bedrock の ARN など、口座番号が入りうる名前は出さない
            content = msg.get("content")
            for b in content if isinstance(content, list) else []:
                # 1つの応答が複数行に分かれて記録されるので、道具の呼び出しは呼び出しの ID で重複を除く
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                call_id = b.get("id")
                if isinstance(call_id, str) and call_id:
                    if call_id in seen_tools:
                        continue
                    seen_tools.add(call_id)
                name = b.get("name")
                if not isinstance(name, str):
                    continue
                if name in CLAUDE_EDIT_TOOLS:
                    rec["work"]["file_edits"] += 1
                kind, name = classify_tool(name, as_dict(b.get("input")))
                if kind:
                    rec["tools"][kind][tool_name(name, include_projects)] += 1
            key = tuple(x if isinstance(x, str) else None for x in (msg.get("id"), d.get("requestId")))
            if key != (None, None):
                if key in seen:
                    continue
                seen.add(key)
            u = as_dict(msg.get("usage"))
            cc = as_dict(u.get("cache_creation"))
            w_total = token_count(u, "cache_creation_input_tokens")
            w1 = token_count(cc, "ephemeral_1h_input_tokens")
            m = rec["models"][model]
            m["responses"] += 1
            m["input"] += token_count(u, "input_tokens")
            m["output"] += token_count(u, "output_tokens")
            m["cache_read"] += token_count(u, "cache_read_input_tokens")
            m["cache_write_1h"] += w1
            m["cache_write_5m"] += max(w_total - w1, 0)
            rec["responses"] += 1

    add_work_time(timestamps, days, lambda r: r["work"])
    for sid, day in first_day.items():
        days[day]["sessions"].add(sid)
    out = {}
    for day, rec in days.items():
        models = {}
        cost, unknown = 0.0, []
        for name, m in rec["models"].items():
            c = prices.cost(name, m)
            models[name] = dict(m, cost_usd=None if c is None else round(c, 4))
            if c is None:
                unknown.append(name)
            else:
                cost += c
        item = {
            "sessions": len(rec["sessions"]),
            "prompts": rec["prompts"],
            "responses": rec["responses"],
            "models": models,
            "entrypoints": dict(rec["entrypoints"]),
            "cost_usd": round(cost, 4),
            "work": dict(rec["work"], responses=rec["responses"]),
        }
        if unknown:
            item["unpriced_models"] = sorted(unknown)
        if include_projects:
            item["projects"] = dict(rec["projects"])
        tools = {k: dict(v) for k, v in rec["tools"].items() if v}
        if tools:
            item["tools"] = tools
        out[day] = item
    return out, len(files), prices.as_of, cwds


CODEX_TOOLS = {"CommandExecution": "commands", "FileChange": "file_edits",
               "McpToolCall": "mcp", "SubAgentActivity": "subagents"}
CODEX_CONTEXT_RECORD = re.compile(r'"type"\s*:\s*"turn_context"')


def codex_turn_id(payload):
    turn_id = payload.get("turn_id")
    return turn_id if isinstance(turn_id, str) and turn_id else None


def codex_contexts(path):
    """先にモデルを調べ、turn_context より前の記録も同じターンに分類する。"""
    by_turn, first, review_only = {}, None, True
    for record in jsonl_records(path, CODEX_CONTEXT_RECORD):
        if record.get("type") != "turn_context":
            continue
        payload = as_dict(record.get("payload"))
        model = payload.get("model")
        if not isinstance(model, str) or not NAME_RE.fullmatch(model):
            model = "unknown"
        context = (model, codex_automatic(payload))
        if first is None:
            first = context
        turn_id = codex_turn_id(payload)
        if turn_id is not None:
            by_turn[turn_id] = context
        review_only = review_only and model == CODEX_REVIEW
    return by_turn, first or ("unknown", None), first is not None and review_only


def codex_automatic(context):
    for key in ("is_automated", "automated", "automation"):
        flag = context.get(key)
        if isinstance(flag, bool):
            return flag
    for key in ("entrypoint", "source", "originator"):
        source = context.get(key)
        if isinstance(source, str):
            if source in ("automation", "scheduled", "exec", "codex_exec", "sdk") or source.startswith("sdk-"):
                return True
            if source in ("interactive", "cli", "vscode", "appServer", "tui"):
                return False
    return None


def codex_timestamp(record, payload):
    if record.get("type") == "session_meta":
        timestamp = local_time(payload.get("timestamp"))
        if timestamp is not None:
            return timestamp
    timestamp = local_time(record.get("timestamp")) or local_time(payload.get("timestamp"))
    if timestamp is None and _int(payload.get("completed_at_ms")):
        try:
            return datetime.fromtimestamp(payload["completed_at_ms"] / 1000).astimezone()
        except (ValueError, OverflowError, OSError):
            pass
    return timestamp


def empty_codex():
    return dict(dict.fromkeys(CODEX_FIELDS, 0), work=empty_work(),
                models=defaultdict(lambda: dict.fromkeys(("responses", *CODEX_TOKENS), 0)),
                mcp_servers=defaultdict(int))


def collect_codex_days(include_projects):
    root = os.path.join(codex_dir(), "sessions")
    files = glob.glob(os.path.join(root, "**", "rollout-*.jsonl"), recursive=True)
    days = defaultdict(empty_codex)
    timestamps = defaultdict(list)
    cwds = set()
    for path in files:
        by_turn, current, review_only = codex_contexts(path)
        thread_auto = None
        review_turns = set()
        time_segment = 0
        previous_review = False
        day = "-".join(os.path.relpath(path, root).split(os.sep)[:3])
        if not valid_day(day):
            day = None
        for record in jsonl_records(path):
            kind = record.get("type")
            payload = as_dict(record.get("payload"))
            event = payload.get("type") if kind == "event_msg" else None
            turn_id = codex_turn_id(payload)
            if kind == "thread_settings" or event in ("thread_settings", "thread_settings_applied"):
                context = as_dict(payload.get("thread_settings")) or payload
                thread_auto = codex_automatic(context)
            if event == "task_started" and turn_id in by_turn:
                current = by_turn[turn_id]
            if kind == "turn_context":
                name = payload.get("model")
                model = name if isinstance(name, str) and NAME_RE.fullmatch(name) else "unknown"
                current = model, codex_automatic(payload)
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd:
                    cwds.add(cwd)
            timestamp = codex_timestamp(record, payload)
            if timestamp is not None:
                day = timestamp.date().isoformat()
            if day is None:
                continue
            model, turn_auto = by_turn.get(turn_id, current)
            human_work = not review_only and model != CODEX_REVIEW
            if previous_review != (not human_work):
                time_segment += 1
                previous_review = not human_work
            if timestamp is not None and human_work:
                timestamps[((path, time_segment), day)].append(timestamp.timestamp())
            if kind == "session_meta":
                if not review_only:
                    days[day]["sessions"] += 1
            elif model == CODEX_REVIEW and (kind == "turn_context" or turn_id in by_turn):
                # ID がある場合は開始イベントと複数のコンテキスト記録を一度だけ数える。
                if turn_id is None or turn_id not in review_turns:
                    days[day]["review_turns"] += 1
                    if turn_id is not None:
                        review_turns.add(turn_id)
            if kind == "token_usage_record":
                codex = days[day]
                model_totals = codex["models"][model]
                model_totals["responses"] += 1
                for key in CODEX_TOKENS:
                    count = token_count(as_dict(payload.get("usage")), key)
                    model_totals[key] += count
                    if human_work:
                        codex[key] += count
                    elif key in ("input_tokens", "output_tokens"):
                        codex["review_tokens"] += count
                if human_work:
                    codex["responses"] += 1
                    codex["work"]["responses"] += 1
            elif event == "item_completed" and human_work:
                item = as_dict(payload.get("item"))
                item_type = item.get("type")
                if item_type == "UserMessage":
                    codex = days[day]
                    codex["prompts"] += 1
                    automatic = turn_auto if turn_auto is not None else thread_auto
                    record_prompt(codex["work"], timestamp, automatic)
                elif isinstance(item_type, str) and item_type in CODEX_TOOLS:
                    codex = days[day]
                    codex[CODEX_TOOLS[item_type]] += 1
                    if item_type == "FileChange":
                        codex["work"]["file_edits"] += 1
                    if item_type == "McpToolCall":
                        server = clean_text(item.get("server"), 80)
                        if server:
                            codex["mcp_servers"][tool_name(server, include_projects)] += 1
    add_work_time(timestamps, days, lambda r: r["work"])
    return dict(days), len(files), cwds


TOOL_KINDS = ("skill", "agent", "mcp", "command")
TOOL_LABELS = {"skill": "スキル", "agent": "エージェント", "mcp": "MCP", "command": "コマンド"}


def classify_tool(name, inp):
    if name == "Skill":
        return "skill", str(inp.get("skill") or "?")
    if name in ("Agent", "Task"):
        return "agent", str(inp.get("subagent_type") or "general-purpose")
    if name.startswith("mcp__"):
        parts = name.split("__")
        return "mcp", parts[1] if len(parts) > 2 else name
    return None, None


def tool_name(name, plain):
    """名前のまま出すか、元に戻せない記号にするか。記号でも「何種類か」は数えられる。"""
    name = clean_text(name, 80) or "?"
    if plain:
        return name
    return "#" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]


def count_dir_items(root):
    skills = len(glob.glob(os.path.join(root, "skills", "*", "SKILL.md")))
    agents = len(glob.glob(os.path.join(root, "agents", "*.md")))
    return skills, agents


def project_roots(cwds):
    """作業フォルダから、.claude フォルダを持つ一番近い上位のフォルダを探す（ホームより上は見ない）。"""
    home = os.path.realpath(os.path.expanduser("~"))
    roots = set()
    for c in cwds:
        cur = os.path.realpath(c)
        while cur.startswith(home) and cur != home:
            if os.path.isdir(os.path.join(cur, ".claude")):
                roots.add(cur)
                break
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    return roots


def inventory(cwds):
    """入っているスキル・エージェント・プラグインの数（名前は出さない）。"""
    cd = claude_dir()
    inv = {"skills_user": 0, "agents_user": 0, "skills_project": 0, "agents_project": 0,
           "skills_plugin": 0, "agents_plugin": 0, "skills_synced": 0, "plugins": 0}
    inv["skills_user"], inv["agents_user"] = count_dir_items(cd)
    roots = project_roots(cwds)
    for r in roots:
        s_, a_ = count_dir_items(os.path.join(r, ".claude"))
        inv["skills_project"] += s_
        inv["agents_project"] += a_
    # 有効なプラグイン: 設定の enabledPlugins と、CLI で入れたもの
    markets = load_json(os.path.join(cd, "plugins", "known_marketplaces.json"), {}) or {}
    enabled = set()
    for f in [os.path.join(cd, "settings.json")] + [os.path.join(r, ".claude", n) for r in roots
                                                     for n in ("settings.json", "settings.local.json")]:
        d = load_json(f, {}) or {}
        ep = d.get("enabledPlugins") if isinstance(d, dict) else None
        if isinstance(ep, dict):
            enabled.update(k for k, v in ep.items() if v is True and isinstance(k, str))
    dirs = set()
    for key in enabled:
        name, _, market = key.partition("@")
        loc = (markets.get(market) or {}).get("installLocation") if isinstance(markets.get(market), dict) else None
        for cand in ([os.path.join(loc, "plugins", name), os.path.join(loc, name)] if loc else []):
            if os.path.isdir(cand):
                dirs.add(os.path.realpath(cand))
                break
    inst = load_json(os.path.join(cd, "plugins", "installed_plugins.json"), {}) or {}
    for key, entries in ((inst.get("plugins") or {}) if isinstance(inst, dict) else {}).items():
        for e in entries if isinstance(entries, list) else [entries]:
            if not isinstance(e, dict) or not e.get("installPath"):
                continue
            pp = e.get("projectPath")
            if e.get("scope") in ("local", "project") and pp and os.path.realpath(pp) not in roots:
                continue
            if key in enabled or e.get("scope") == "user":
                dirs.add(os.path.realpath(e["installPath"]))
    inv["plugins"] = len(dirs)
    for pd in dirs:
        inv["skills_plugin"] += len(glob.glob(os.path.join(pd, "skills", "*", "SKILL.md")))
        inv["agents_plugin"] += len(glob.glob(os.path.join(pd, "agents", "*.md")))
    synced = set()  # claude.ai から同期したもの。同じプラグインの古い版（~g1, ~g2 …）が重なるので名前で重複を除く
    for f in glob.glob(os.path.join(cd, "plugins", "synced", "**", "SKILL.md"), recursive=True):
        parts = os.path.relpath(f, os.path.join(cd, "plugins", "synced")).split(os.sep)
        plugin = re.sub(r"~g\d+$", "", parts[-4]) if len(parts) >= 4 else ""
        synced.add((plugin, parts[-2]))
    inv["skills_synced"] = len(synced)
    inv["projects_scanned"] = len(roots)
    inv["as_of"] = date.today().isoformat()
    return inv


def harness_roots(cwds):
    home = os.path.realpath(os.path.expanduser("~"))
    roots = set()
    for cwd in cwds:
        cur = os.path.realpath(cwd)
        parent = os.path.dirname(cur)
        while cur != home and parent != cur:
            if (os.path.isdir(os.path.join(cur, ".claude"))
                    or any(os.path.isfile(os.path.join(cur, name)) for name in ("CLAUDE.md", "AGENTS.md"))):
                roots.add(cur)
                break
            cur, parent = parent, os.path.dirname(parent)
    return roots


def count_instructions(root, filename, counts, prefix):
    path = os.path.join(root, filename)
    if not os.path.isfile(path):
        return
    counts[prefix + "_files"] += 1
    try:
        with open(path, "rb") as f:
            counts[prefix + "_lines"] += sum(1 for _ in f)
    except OSError:
        pass


def settings_counts(path, counts):
    settings = as_dict(load_json(path))
    for matchers in as_dict(settings.get("hooks")).values():
        if isinstance(matchers, list):
            for matcher in matchers:
                hooks = as_dict(matcher).get("hooks")
                if isinstance(hooks, list):
                    counts["hooks"] += sum(isinstance(hook, dict) for hook in hooks)
    counts["mcp_servers"] += len(as_dict(settings.get("mcpServers")))


# Python 3.9 では TOML のパーサーが標準では使えないため、サーバーのテーブル見出しだけを数える。
MCP_TABLE = re.compile(r"""^\s*\[\s*(?:mcp_servers|"mcp_servers"|'mcp_servers')\s*\.\s*
                           (?:[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|'[^']*')\s*\]\s*(?:\#.*)?$""",
                       re.VERBOSE)
TOML_STRING = re.compile(r"""("{3}|'{3}|"(?:[^"\\]|\\.)*"|'[^']*'|\#.*)""")


def count_codex_mcp(path):
    count = 0
    multiline = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if multiline:
                    if re.search(r"(?<!\\)" + multiline, line):
                        multiline = None
                    continue
                if MCP_TABLE.fullmatch(line.rstrip("\r\n")):
                    count += 1
                    continue
                # 見出しに似た文字列が複数行の値に入っていても数えない。
                for match in TOML_STRING.finditer(line):
                    delimiter = match[0]
                    if delimiter in ('"""', "'''"):
                        if not re.search(r"(?<!\\)" + delimiter, line[match.end():]):
                            multiline = delimiter
                        break
    except OSError:
        pass
    return count


def harness_inventory(cwds):
    counts = dict.fromkeys(HARNESS_KEYS, 0)
    roots = harness_roots(cwds)
    counts["projects"] = len(roots)
    for root in roots:
        count_instructions(root, "CLAUDE.md", counts, "claude_md")
        count_instructions(root, "AGENTS.md", counts, "agents_md")
        for path in glob.glob(os.path.join(root, ".claude", "settings*.json")):
            settings_counts(path, counts)
        counts["mcp_servers"] += len(as_dict(as_dict(load_json(os.path.join(root, ".mcp.json"))).get("mcpServers")))
    cd, xd = claude_dir(), codex_dir()
    count_instructions(cd, "CLAUDE.md", counts, "claude_md")
    count_instructions(xd, "AGENTS.md", counts, "agents_md")
    settings_counts(os.path.join(cd, "settings.json"), counts)
    counts["mcp_servers"] += count_codex_mcp(os.path.join(xd, "config.toml"))
    counts["codex_skills"] = count_dir_items(xd)[0]
    counts["as_of"] = date.today().isoformat()
    return counts


# ---------------------------------------------------------------- コマンド

def drive_candidates():
    home = os.path.expanduser("~")
    pats = [
        os.path.join(home, "Library", "CloudStorage", "*"),          # macOS: Google Drive / OneDrive / Dropbox
        os.path.join(home, "Google Drive*"),
        os.path.join(home, "OneDrive*"),
        os.path.join(home, "Dropbox*"),
    ]
    if platform.system() == "Windows":
        pats += [d + ":\\" for d in "DEFGHIJKLMNOPQRSTUVWXYZ"]
    found = []
    for p in pats:
        for c in glob.glob(p):
            if os.path.isdir(c):
                found.append(c)
                for sub in ("マイドライブ", "My Drive", "共有ドライブ", "Shared drives"):
                    s = os.path.join(c, sub)
                    if os.path.isdir(s):
                        found.append(s)
    return found


def cmd_find_drives(a):
    c = drive_candidates()
    if not c:
        print("同期フォルダが見つかりませんでした。Google ドライブ（パソコン版）や OneDrive が入っているか確認してください。")
        return
    print("同期されているフォルダの候補:")
    for p in c:
        print("  " + p)


def cmd_setup(a):
    dest = os.path.abspath(os.path.expanduser(a.dest))
    if not os.path.isdir(dest):
        die("格納先のフォルダがありません: " + dest)
    probe = os.path.join(dest, SUBDIR)
    try:
        os.makedirs(probe, exist_ok=True)
        test = os.path.join(probe, ".write-test")
        with open(test, "w") as f:
            f.write("ok")
        os.remove(test)
    except OSError as e:
        die("格納先に書き込めません（共有の権限が「閲覧者」かもしれません）: %s" % e)
    cfg = {"name": a.name.strip(), "dest": dest, "include_projects": bool(a.projects),
           "set_at": datetime.now().isoformat(timespec="seconds")}
    write_json_atomic(CONFIG_PATH, cfg)
    print("設定しました: " + CONFIG_PATH)
    print("  名前: %s / 格納先: %s / フォルダ別: %s" % (cfg["name"], os.path.join(dest, SUBDIR),
                                                 "送る" if cfg["include_projects"] else "送らない"))


def cmd_retention(a):
    path = os.path.realpath(os.path.join(claude_dir(), "settings.json"))  # シンボリックリンクなら先を直す
    exists = os.path.exists(path)
    s = load_json(path, None) if exists else {}
    if not isinstance(s, dict):
        die("settings.json の形が想定と違うため変更しません。手で cleanupPeriodDays を設定してください: " + path)
    cur = s.get("cleanupPeriodDays")
    if cur is not None and (not isinstance(cur, int) or isinstance(cur, bool) or cur <= 0):
        die("cleanupPeriodDays が %r に設定されています。意図した設定かもしれないので変更しません。" % (cur,))
    if isinstance(cur, int) and cur >= a.days:
        print("すでに %d 日残す設定です。変更しません。" % cur)
        return
    mode = 0o600
    if exists:
        mode = os.stat(path).st_mode & 0o777
        shutil.copy2(path, path + ".bak-usage-check")
    s["cleanupPeriodDays"] = a.days
    write_json_atomic(path, s, mode=mode, sort_keys=False, indent=2)
    print("ログを残す日数を %s → %d 日にしました（元のファイルは settings.json.bak-usage-check）。"
          % (cur if cur is not None else "既定の30", a.days))


def cmd_collect(a):
    cfg = load_json(CONFIG_PATH)
    if not cfg:
        die("まだ設定されていません。先に setup を実行してください。")
    include_projects = cfg.get("include_projects", False)
    days, nfiles, as_of, cwds = collect_days(include_projects)
    codex_days, codex_files, codex_cwds = collect_codex_days(include_projects)
    inv = inventory(cwds)
    harness = harness_inventory(cwds | codex_cwds)
    fresh_days = days.keys() | codex_days.keys()
    if not fresh_days:
        die("Claude Code または Codex の使用記録が見つかりませんでした。")
    out_path = os.path.join(cfg["dest"], SUBDIR, safe_filename(cfg["name"]) + ".json")
    merged = {}
    if os.path.exists(out_path):
        old, why = validate_member(load_json(out_path))
        if old is None:
            print("注意: 格納先にあった自分のファイルの形が正しくないため、取り込まずに作り直します（%s）。" % why)
        else:
            if old["member"] != cfg["name"]:
                print("注意: 格納先のファイルの名前（%s）が設定（%s）と違います。" % (old["member"], cfg["name"]))
            merged = dict(old["days"])
    # 同じ日の数字は道具ごとに上書きし、ログが消えた日や片方だけに残っている日は保持する。
    for day, record in days.items():
        previous_codex = merged.get(day, {}).get("codex")
        merged[day] = record
        if previous_codex is not None:
            merged[day]["codex"] = previous_codex
    for day, record in codex_days.items():
        merged.setdefault(day, {})["codex"] = record
    data = {"schema": SCHEMA, "tool_version": TOOL_VERSION, "member": cfg["name"],
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "prices_as_of": as_of, "inventory": inv, "harness": harness, "days": dict(sorted(merged.items()))}
    tot = summarize({cfg["name"]: data}, None, None)
    print("読んだログ: Claude %d ファイル / Codex %d ファイル / 期間: %s 〜 %s"
          % (nfiles, codex_files, min(fresh_days), max(fresh_days)))
    print_summary(tot)
    print("  入っている数: スキル %d（個人 %d・プロジェクト %d・プラグイン %d・同期 %d）／エージェント %d／プラグイン %d" % (
        inv_total(inv, "skills"), inv["skills_user"], inv["skills_project"], inv["skills_plugin"], inv["skills_synced"],
        inv_total(inv, "agents"), inv["plugins"]))
    if a.dry_run:
        print("（--dry-run なので書き込んでいません）")
        return
    write_json_atomic(out_path, data)
    print("書き込みました: " + out_path)


# ---------------------------------------------------------------- 集計とレポート

def inv_total(inv, what):
    if what == "skills":
        return inv["skills_user"] + inv["skills_project"] + inv["skills_plugin"] + inv["skills_synced"]
    return inv["agents_user"] + inv["agents_project"] + inv["agents_plugin"]


def add_counts(total, counts, keys):
    for key in keys:
        total[key] += counts[key]


def add_work(total, work):
    add_counts(total, work, ("prompts", "responses", "seconds", "file_edits"))
    if work["auto_prompts"] is not None:
        total["auto_prompts"] = (total["auto_prompts"] or 0) + work["auto_prompts"]


def add_heatmap(heatmap, day, work):
    weekday = date.fromisoformat(day).weekday()
    for hour, count in work["heatmap"].items():
        heatmap[(weekday, int(hour))] += count


def daily_work(record):
    if "work" in record:
        yield "Claude", record["work"]
    if "codex" in record:
        yield "Codex", record["codex"]["work"]


def work_values(work):
    prompts = work["prompts"]
    auto = work["auto_prompts"]
    return (round(work["responses"] / prompts, 2) if prompts else None,
            round(work["seconds"] / 60, 1),
            round(auto / prompts * 100, 1) if prompts and auto is not None else None,
            work["file_edits"])


def summarize(members, d_from, d_to):
    per_member, per_day = {}, defaultdict(lambda: defaultdict(int))
    per_model = defaultdict(lambda: defaultdict(float))
    total = defaultdict(float)
    tool_names = defaultdict(lambda: {"calls": 0, "users": set()})
    codex_members, work_members = {}, {}
    codex_models = defaultdict(lambda: defaultdict(int))
    codex_total = defaultdict(int)
    work_total = defaultdict(empty_work)
    heatmaps = defaultdict(lambda: defaultdict(int))
    harness = {}
    recorded_days = set()
    for name, data in members.items():
        m = defaultdict(float)
        active = 0
        for day, r in (data.get("days") or {}).items():
            if (d_from and day < d_from) or (d_to and day > d_to):
                continue
            recorded_days.add(day)
            codex = r.get("codex")
            if codex is not None:
                add_counts(codex_members.setdefault(name, defaultdict(int)), codex, CODEX_FIELDS)
                add_counts(codex_total, codex, CODEX_FIELDS)
                for model, counts in codex["models"].items():
                    add_counts(codex_models[model], counts, ("responses", *CODEX_TOKENS))
            for tool, work in daily_work(r):
                add_work(work_members.setdefault((name, tool), empty_work()), work)
                add_work(work_total[tool], work)
                add_heatmap(heatmaps[tool], day, work)
            if r.get("prompts", 0) > 0:
                active += 1
            for k in ("sessions", "prompts", "responses", "cost_usd"):
                m[k] += r.get(k, 0) or 0
                total[k] += r.get(k, 0) or 0
            for kind, names in (r.get("tools") or {}).items():
                for n, c in names.items():
                    m["calls_" + kind] += c
                    total["calls_" + kind] += c
                    t = tool_names[(kind, n)]
                    t["calls"] += c
                    t["users"].add(name)
            if "sessions" in r:
                daily = per_day[day]
                daily["prompts"] += r.get("prompts", 0)
                daily["sessions"] += r["sessions"]
            for mn, t in (r.get("models") or {}).items():
                toks = sum(t.get(k, 0) for k in ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h"))
                m["tokens"] += toks
                m["output"] += t.get("output", 0)
                total["tokens"] += toks
                per_model[mn]["tokens"] += toks
                per_model[mn]["responses"] += t.get("responses", 0)
                per_model[mn]["cost_usd"] += t.get("cost_usd") or 0
        m["active_days"] = active
        m["updated_at"] = data.get("updated_at", "")
        m["inventory"] = data.get("inventory")
        if data.get("harness") is not None:
            harness[name] = data["harness"]
        per_member[name] = m
    return {"members": per_member, "days": dict(sorted(per_day.items())),
            "first_day": min(recorded_days, default="-"), "last_day": max(recorded_days, default="-"),
            "models": dict(per_model), "total": dict(total),
            "codex_members": codex_members, "codex_models": codex_models, "codex_total": codex_total,
            "work_members": work_members, "work_total": work_total, "heatmaps": heatmaps, "harness": harness,
            "tools": {k: {"calls": v["calls"], "users": len(v["users"])} for k, v in tool_names.items()}}


def fmt_int(n):
    return "{:,}".format(int(round(n)))


def fmt_tok(n):
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= div:
            return "%.1f%s" % (n / div, unit)
    return fmt_int(n)


def fmt_work(work):
    trust, minutes, auto, edits = work_values(work)
    return ("-" if trust is None else "%.2f" % trust, "{:,.1f}".format(minutes),
            "-" if auto is None else "%.1f%%" % auto, fmt_int(edits))


def codex_token_total(counts):
    # キャッシュ入力と推論はそれぞれ入力と出力に含まれる。
    return counts["input_tokens"] + counts["output_tokens"]


def print_summary(s):
    t = s["total"]
    tools_line = "  スキル %s 回 / エージェント %s 回 / MCP %s 回 / コマンド %s 回" % tuple(
        fmt_int(t.get("calls_" + k, 0)) for k in TOOL_KINDS)
    print("  セッション %s / 人の発言 %s / AIの応答 %s / トークン %s / API換算 %s" % (
        fmt_int(t.get("sessions", 0)), fmt_int(t.get("prompts", 0)), fmt_int(t.get("responses", 0)),
        fmt_tok(t.get("tokens", 0)), fmt_money(t.get("cost_usd", 0))))
    print(tools_line)
    c = s["codex_total"]
    if c:
        print("  Codex: セッション %s / 人の発言 %s / AIの応答 %s / トークン %s（推論 %s）"
              " / コマンド %s / ファイル変更 %s / MCP %s / サブエージェント %s"
              " / 自動レビュー %s 回（%s トークン）" % (
                  fmt_int(c["sessions"]), fmt_int(c["prompts"]), fmt_int(c["responses"]),
                  fmt_tok(codex_token_total(c)), fmt_tok(c["reasoning_output_tokens"]),
                  *(fmt_int(c[k]) for k in ("commands", "file_edits", "mcp", "subagents", "review_turns")),
                  fmt_tok(c["review_tokens"])))
    for tool, work in sorted(s["work_total"].items()):
        print("  %s: 任せ度 %s / 作業時間 %s 分 / 自動実行 %s / ファイル編集 %s 回"
              % (tool, *fmt_work(work)))
    for name, snapshot in sorted(s["harness"].items()):
        print("  ハーネス（%s・%s）: 整備されたフォルダ %s / CLAUDE.md %s 件・%s 行"
              " / AGENTS.md %s 件・%s 行 / フック %s / MCP 設定 %s / Codex スキル %s"
              % (name, snapshot["as_of"], *(fmt_int(snapshot[key]) for key in HARNESS_KEYS)))


def bar_chart_svg(days):
    if not days:
        return ""
    keys = [k for k in days if DATE_RE.match(k)]
    vals = [days[k]["prompts"] for k in keys]
    mx = max(vals) or 1
    w, h, pad = 680, 180, 28
    bw = max((w - pad) / len(keys), 2)
    bars = []
    for i, (k, v) in enumerate(zip(keys, vals)):
        bh = (h - 30) * v / mx
        x = pad + i * bw
        bars.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="1.5" class="bar"><title>%s: %d</title></rect>'
                    % (x + 1, h - 20 - bh, max(bw - 2, 1), bh, html.escape(k), v))
    step = max(1, len(keys) // 8)
    labels = "".join('<text x="%.1f" y="%d" class="ax">%s</text>' % (pad + i * bw, h - 4, html.escape(keys[i][5:]))
                     for i in range(0, len(keys), step))
    return ('<svg viewBox="0 0 %d %d" width="100%%" role="img" aria-label="日別の発言数">'
            '<text x="0" y="12" class="ax">%d</text><line x1="%d" y1="%d" x2="%d" y2="%d" class="base"/>%s%s</svg>'
            % (w, h, mx, pad, h - 20, w, h - 20, "".join(bars), labels))


def report_table(headers, rows):
    headings = "".join("<th>" + html.escape(h) + "</th>" for h in headers)
    body = "".join("<tr>" + "".join("<td>" + html.escape(str(v)) + "</td>" for v in row) + "</tr>"
                   for row in rows)
    if not body:
        return '<p class="mute">記録なし</p>'
    return "<table><thead><tr>" + headings + "</tr></thead><tbody>" + body + "</tbody></table>"


def render_heatmap(tool, counts):
    peak = max(counts.values(), default=0) or 1
    headings = "".join('<th scope="col">%d</th>' % hour for hour in range(24))
    rows = []
    for weekday, label in enumerate(WEEKDAYS):
        cells = []
        for hour in range(24):
            n = counts.get((weekday, hour), 0)
            level = (n * 4 + peak - 1) // peak
            cells.append('<td class="heat-%d">%d</td>' % (level, n))
        rows.append('<tr><th scope="row">' + label + '</th>' + "".join(cells) + '</tr>')
    return ('<table class="heatmap"><caption>%s</caption><thead><tr><th scope="col">時</th>%s'
            '</tr></thead><tbody>%s</tbody></table>') % (html.escape(tool), headings, "".join(rows))


def render_new_metrics(s):
    codex = report_table(
        ("名前", "セッション", "人の発言", "AIの応答", "トークン", "推論", "コマンド", "ファイル変更", "自動レビュー"),
        ((name, fmt_int(c["sessions"]), fmt_int(c["prompts"]), fmt_int(c["responses"]),
          fmt_tok(codex_token_total(c)), fmt_tok(c["reasoning_output_tokens"]),
          fmt_int(c["commands"]), fmt_int(c["file_edits"]),
          "%s 回 / %s トークン" % (fmt_int(c["review_turns"]), fmt_tok(c["review_tokens"])))
         for name, c in sorted(s["codex_members"].items())),
    )
    codex_models = report_table(
        ("Codex のモデル", "AIの応答", "入力", "キャッシュ入力", "出力", "推論"),
        ((name, fmt_int(c["responses"]), *(fmt_tok(c[key]) for key in CODEX_TOKENS))
         for name, c in sorted(s["codex_models"].items())),
    ) if s["codex_models"] else ""
    work = report_table(
        ("名前", "道具", "任せ度", "作業時間（分）", "自動実行", "ファイル編集"),
        ((name, tool, *fmt_work(w)) for (name, tool), w in sorted(s["work_members"].items())),
    )
    heatmaps = "".join(render_heatmap(tool, h) for tool, h in sorted(s["heatmaps"].items()))
    harness = report_table(
        ("名前", "確認日", "整備済みフォルダ", "CLAUDE.md 件数 / 行数", "AGENTS.md 件数 / 行数",
         "フック", "MCP 設定", "Codex スキル"),
        ((name, v["as_of"], fmt_int(v["projects"]),
          "%s / %s" % (fmt_int(v["claude_md_files"]), fmt_int(v["claude_md_lines"])),
          "%s / %s" % (fmt_int(v["agents_md_files"]), fmt_int(v["agents_md_lines"])),
          fmt_int(v["hooks"]), fmt_int(v["mcp_servers"]), fmt_int(v["codex_skills"]))
         for name, v in sorted(s["harness"].items())),
    )
    return ('<h2>Codex</h2>' + codex + codex_models
            + '<h2>任せ方と働き方</h2>' + work
            + '<h2>時間帯 × 曜日（全員の発言数）</h2>' + (heatmaps or '<p class="mute">記録なし</p>')
            + '<h2>ハーネスの整備度</h2>' + harness)


def render_html(org, s, d_from, d_to, as_of):
    t = s["total"]
    n = len(s["members"])
    active = sum(1 for name, m in s["members"].items()
                 if m["prompts"] > 0 or s["codex_members"].get(name, {}).get("prompts", 0) > 0)
    period = "%s 〜 %s" % (d_from or s["first_day"], d_to or s["last_day"])
    rows = "".join(
        "<tr><td>%s</td><td>%d</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class='mute'>%s</td></tr>" % (
            html.escape(name), m["active_days"], fmt_int(m["sessions"]), fmt_int(m["prompts"]),
            fmt_tok(m["tokens"]), html.escape(fmt_money(m["cost_usd"])), html.escape(m["updated_at"][:10]))
        for name, m in sorted(s["members"].items(), key=lambda x: -x[1]["prompts"]))
    mrows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            html.escape(k), fmt_int(v["responses"]), fmt_tok(v["tokens"]), html.escape(fmt_money(v["cost_usd"])))
        for k, v in sorted(s["models"].items(), key=lambda x: -x[1]["tokens"]))
    def inv_cell(m, what):
        inv = m.get("inventory")
        return fmt_int(inv_total(inv, what)) if inv else "-"
    trows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            html.escape(name), fmt_int(m["calls_skill"]), fmt_int(m["calls_agent"]), fmt_int(m["calls_mcp"]),
            fmt_int(m["calls_command"]), inv_cell(m, "skills"), inv_cell(m, "agents"))
        for name, m in sorted(s["members"].items(), key=lambda x: -(x[1]["calls_skill"] + x[1]["calls_agent"])))
    top = sorted(s["tools"].items(), key=lambda x: -x[1]["calls"])[:15]
    toprows = "".join(
        "<tr><td>%s</td><td class='l'>%s</td><td>%s</td><td>%s</td></tr>" % (
            TOOL_LABELS[k[0]], html.escape("（名前は非公開）" if k[1].startswith("#") else k[1]),
            fmt_int(v["calls"]), fmt_int(v["users"]))
        for k, v in top) or "<tr><td colspan='4' class='mute'>記録なし</td></tr>"
    kpi = [("利用者", "%d / %d 人" % (active, n)), ("Claude セッション", fmt_int(t.get("sessions", 0))),
           ("Claude 発言", fmt_int(t.get("prompts", 0))), ("Claude トークン", fmt_tok(t.get("tokens", 0))),
           ("Claude API換算", fmt_money(t.get("cost_usd", 0)))]
    kpis = "".join('<div class="kpi"><div class="k">%s</div><div class="v">%s</div></div>'
                   % (html.escape(k), html.escape(v)) for k, v in kpi)
    return """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Claude Code / Codex 使用量レポート</title>
<style>
:root{--ink:#1f2328;--mute:#6a737d;--line:#e3e6ea;--accent:#c96442;--bg:#fff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:13px/1.6 "Hiragino Sans","Yu Gothic","Noto Sans JP",sans-serif}
.page{max-width:760px;margin:0 auto;padding:32px 28px}
h1{font-size:22px;margin:0}h2{font-size:15px;margin:28px 0 8px;border-left:4px solid var(--accent);padding-left:8px}
.sub{color:var(--mute);margin:4px 0 20px}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
.kpi{border:1px solid var(--line);border-radius:8px;padding:10px}.k{color:var(--mute);font-size:11px}.v{font-size:18px;font-weight:700}
table{width:100%%;border-collapse:collapse}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child{text-align:left}th{font-size:11px;color:var(--mute);font-weight:600}
td{white-space:nowrap}td:first-child,td.l{white-space:normal;overflow-wrap:anywhere}table+table{margin-top:10px}
table:not(.heatmap) td:first-child{min-width:56px}
.mute{color:var(--mute)}th.l,td.l{text-align:left}.bar{fill:var(--accent)}.ax{font-size:10px;fill:var(--mute)}.base{stroke:var(--line)}
.heatmap{table-layout:fixed;font-size:10px;margin-bottom:14px;print-color-adjust:exact;-webkit-print-color-adjust:exact}
.heatmap caption{font-weight:600;text-align:left;margin-bottom:4px}
.heatmap td,.heatmap th{padding:3px 0;text-align:center}
.heatmap td{border:1px solid #fff;overflow-wrap:anywhere}
.heat-0{background:#f3f5f7;color:#6a737d}.heat-1{background:#d8eaf1}.heat-2{background:#9ccbdc}
.heat-3{background:#4f8fa8;color:#101820}.heat-4{background:#24586e;color:#fff}
.note{color:var(--mute);font-size:11px;margin-top:24px;line-height:1.7}
@page{size:A4;margin:12mm}
@media print{h2{break-after:avoid}tr,.heatmap{break-inside:avoid}}
</style></head><body><div class="page">
<h1>Claude Code / Codex 使用量レポート</h1>
<div class="sub">%s ／ 期間 %s ／ 作成 %s</div>
<div class="kpis">%s</div>
<h2>Claude Code の日別発言数（全員）</h2>%s
<h2>Claude Code のメンバー別</h2>
<table><thead><tr><th>名前</th><th>使った日数</th><th>セッション</th><th>人の発言</th><th>トークン</th><th>API換算</th><th>最終送信</th></tr></thead><tbody>%s</tbody></table>
<h2>モデル別</h2>
<table><thead><tr><th>モデル</th><th>AIの応答</th><th>トークン</th><th>API換算</th></tr></thead><tbody>%s</tbody></table>
<h2>スキル・エージェントの活用（メンバー別）</h2>
<table><thead><tr><th>名前</th><th>スキル</th><th>エージェント</th><th>MCP</th><th>コマンド</th><th>入っているスキル</th><th>入っているエージェント</th></tr></thead><tbody>%s</tbody></table>
<h2>よく使われた道具（上位15）</h2>
<table><thead><tr><th>種類</th><th class='l'>名前</th><th>回数</th><th>使った人数</th></tr></thead><tbody>%s</tbody></table>
%s
<div class="note">
・対象は Claude Code と Codex のローカルログ。会話の中身は集めていない。<br>
・「人の発言」は利用者が入力した回数。「AIの応答」にはサブエージェントの応答も含む。<br>
・スキル・エージェント・MCP・コマンドは呼び出した回数。「入っている」は各自が最後に送った時点の数（個人・よく作業するフォルダ・有効なプラグイン・claude.ai から同期したものの合計）。<br>
・「API換算」は Anthropic API の公開単価（%s 時点）で計算した目安で、定額プランの実際の支払額ではない。%s<br>
・任せ度は応答数 ÷ 発言数。作業時間は同じセッション・同じ日の5分以内の記録間隔の合計。自動実行の割合は起動元を判別できる記録がある場合に表示する。<br>
・Codex の自動レビューは回数とトークンを別集計する。人の利用量、働き方、時間帯からは除く。
</div></div></body></html>""" % (
        html.escape(org), html.escape(period), date.today().isoformat(), kpis, bar_chart_svg(s["days"]),
        rows, mrows, trows, toprows, render_new_metrics(s), html.escape(as_of),
        ("円は 1ドル＝%s円 で固定して換算。" % fmt_int(YEN)) if YEN else "")


def find_browser():
    cands = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    for n in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge", "msedge"):
        p = shutil.which(n)
        if p:
            return p
    return None


def load_members(a):
    dest = a.dest or (load_json(CONFIG_PATH, {}) or {}).get("dest")
    if not dest:
        die("--dest で格納先を指定してください。")
    folder = os.path.join(os.path.abspath(os.path.expanduser(dest)), SUBDIR)
    members, names = {}, defaultdict(list)
    notices = sys.stderr if getattr(a, "json", False) else sys.stdout
    for p in sorted(glob.glob(os.path.join(folder, "*.json"))):
        d, why = validate_member(load_json(p))
        stem = os.path.basename(p)[:-5]
        if d is None:
            print("注意: %s は形が正しくないため集計から外しました（%s）。" % (os.path.basename(p), why),
                  file=notices)
            continue
        if safe_filename(d["member"]) != stem:
            print("注意: %s の中の名前が「%s」でファイル名と違います（ファイル名で集計します）。"
                  % (os.path.basename(p), d["member"]), file=notices)
        names[d["member"]].append(stem)
        d["member"] = clean_text(stem, 40) or stem
        members[stem] = d
    for n, files in names.items():
        if len(files) > 1:
            print("注意: 名前「%s」のファイルが複数あります: %s" % (n, ", ".join(files)), file=notices)
    if not members:
        die("集まったファイルがありません: " + folder)
    for v in (a.date_from, a.date_to):
        if v and not valid_day(v):
            die("期間は YYYY-MM-DD の形で指定してください: " + v)
    return folder, members


def period_stem(a):
    if a.date_from or a.date_to:
        return "claude-usage_%s_%s" % (a.date_from or "start", a.date_to or date.today().isoformat())
    return "claude-usage_%s" % date.today().strftime("%Y%m%d")


def cmd_report(a):
    folder, members = load_members(a)
    s = summarize(members, a.date_from, a.date_to)
    as_of = next(iter(members.values())).get("prices_as_of", "?")
    out_dir = os.path.join(folder, "reports")
    os.makedirs(out_dir, exist_ok=True)
    stem = period_stem(a)
    html_path = os.path.join(out_dir, stem + ".html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(a.org, s, a.date_from, a.date_to, as_of))
    print("メンバー %d 人分を集計しました。" % len(members))
    print_summary(s)
    print("HTML: " + html_path)
    browser = find_browser()
    if not browser:
        print("Chrome / Edge が見つからないため PDF は作っていません。HTML をブラウザで開き「PDF に保存」してください。")
        return
    pdf_path = os.path.join(out_dir, stem + ".pdf")
    if os.path.exists(pdf_path):
        os.remove(pdf_path)
    url = "file:///" + html_path.replace("\\", "/").lstrip("/")
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run([browser, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                        "--user-data-dir=" + profile,  # スクリプト・iframe・外部通信はページの CSP で止める
                        "--disable-extensions", "--no-first-run",
                        "--disable-background-networking", "--disable-component-update",
                        "--no-proxy-server", "--host-resolver-rules=MAP * ~NOTFOUND",
                        "--print-to-pdf=" + pdf_path, url], capture_output=True, timeout=120)
    if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:  # Chrome は成功しても 0 以外を返すことがある
        print("PDF: " + pdf_path)
    else:
        print("PDF の書き出しに失敗しました。HTML をブラウザで開き「PDF に保存」してください。")


def sheet_safe(v):
    """スプシに貼った時に数式として動かないようにする。"""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def export_metrics(codex_month, work_month, heatmaps, members):
    codex = [["月", "名前", "セッション", "人の発言", "AIの応答", "入力トークン", "キャッシュ入力トークン",
              "出力トークン", "推論トークン", "コマンド", "ファイル変更", "MCP", "サブエージェント",
              "自動レビュー", "自動レビューのトークン"]]
    for (month, name), c in sorted(codex_month.items()):
        codex.append([month, sheet_safe(name), *(c[k] for k in CODEX_FIELDS)])
    work = [["月", "名前", "道具", "任せ度", "作業時間(分)", "自動実行の割合(%)", "ファイル編集回数"]]
    for (month, name, tool), w in sorted(work_month.items()):
        work.append([month, sheet_safe(name), tool, *("" if n is None else n for n in work_values(w))])
    heatmap = [["月", "道具", "曜日", "時", "発言数"]]
    for (month, tool), counts in sorted(heatmaps.items()):
        heatmap.extend([month, tool, label, hour, counts.get((weekday, hour), 0)]
                       for weekday, label in enumerate(WEEKDAYS) for hour in range(24))
    harness = [["名前", "確認日", "整備されたフォルダ数", "CLAUDE.md 行数合計", "AGENTS.md 行数合計",
                "フック数", "MCP 設定数", "Codex スキル数"]]
    for name, member in sorted(members.items()):
        h = member.get("harness")
        if h is not None:
            harness.append([sheet_safe(name), h["as_of"], h["projects"], h["claude_md_lines"], h["agents_md_lines"],
                            h["hooks"], h["mcp_servers"], h["codex_skills"]])
    return {"codex": codex, "work": work, "heatmap": heatmap, "harness": harness}


def export_rows(members, d_from, d_to):
    monthly = defaultdict(lambda: defaultdict(float))
    projects = defaultdict(int)
    daily = defaultdict(lambda: defaultdict(float))
    models = defaultdict(lambda: defaultdict(float))
    tool_month = defaultdict(lambda: {"calls": defaultdict(int), "names": defaultdict(set)})
    tool_names = defaultdict(lambda: {"calls": 0, "users": set()})
    codex_month = defaultdict(lambda: defaultdict(int))
    work_month = defaultdict(empty_work)
    heatmaps = defaultdict(lambda: defaultdict(int))
    for name, data in members.items():
        for day, r in data["days"].items():
            if (d_from and day < d_from) or (d_to and day > d_to):
                continue
            month = day[:7]
            if "codex" in r:
                add_counts(codex_month[(month, name)], r["codex"], CODEX_FIELDS)
            for tool, work in daily_work(r):
                add_work(work_month[(month, name, tool)], work)
                add_heatmap(heatmaps[(month, tool)], day, work)
            if "sessions" not in r:
                continue
            m = monthly[(day[:7], name)]
            m["active_days"] += 1 if r["prompts"] > 0 else 0
            for k in ("sessions", "prompts", "responses", "cost_usd"):
                m[k] += r[k]
            dd = daily[(day, name)]
            for k in ("sessions", "prompts", "responses", "cost_usd"):
                dd[k] += r[k]
            for mn, t in r["models"].items():
                toks = sum(t[k] for k in TOKEN_KEYS if k != "responses")
                m["tokens"] += toks
                dd["tokens"] += toks
                mm = models[(day[:7], name, mn)]
                mm["responses"] += t["responses"]
                mm["tokens"] += toks
                mm["output"] += t["output"]
                mm["cost_usd"] += t["cost_usd"] or 0
            m["updated_at"] = data.get("updated_at", "")[:10]
            tm = tool_month[(day[:7], name)]
            for kind, names in (r.get("tools") or {}).items():
                for tn, c in names.items():
                    tm["calls"][kind] += c
                    tm["names"][kind].add(tn)
                    tt = tool_names[(day[:7], kind, tn)]
                    tt["calls"] += c
                    tt["users"].add(name)
            for pj, n in (r.get("projects") or {}).items():
                projects[(day[:7], name, pj)] += n
    head_m = ["月", "名前", "使った日数", "セッション", "人の発言", "AIの応答", "トークン", "API換算(USD)", "最終送信日"]
    rows_m = [[mon, sheet_safe(name), int(v["active_days"]), int(v["sessions"]), int(v["prompts"]),
               int(v["responses"]), int(v["tokens"]), round(v["cost_usd"], 2), v["updated_at"]]
              for (mon, name), v in sorted(monthly.items())]
    head_p = ["月", "名前", "フォルダ", "人の発言"]
    rows_p = [[mon, sheet_safe(name), sheet_safe(pj), n]
              for (mon, name, pj), n in sorted(projects.items(), key=lambda x: (x[0][0], x[0][1], -x[1]))]
    head_d = ["日付", "名前", "セッション", "人の発言", "AIの応答", "トークン", "API換算(USD)"]
    rows_d = [[day, sheet_safe(name), int(v["sessions"]), int(v["prompts"]), int(v["responses"]),
               int(v["tokens"]), round(v["cost_usd"], 2)] for (day, name), v in sorted(daily.items())]
    head_o = ["月", "名前", "モデル", "AIの応答", "トークン", "出力トークン", "API換算(USD)"]
    rows_o = [[mon, sheet_safe(name), mn, int(v["responses"]), int(v["tokens"]), int(v["output"]),
               round(v["cost_usd"], 2)] for (mon, name, mn), v in sorted(models.items())]
    head_t = ["月", "名前", "スキル呼び出し", "使ったスキルの種類", "エージェント呼び出し", "使ったエージェントの種類",
              "MCP呼び出し", "使ったMCPの種類", "コマンド", "入っているスキル", "入っているエージェント", "プラグイン"]
    rows_t = []
    for (mon, name), v in sorted(tool_month.items()):
        inv = members[name].get("inventory")
        rows_t.append([mon, sheet_safe(name)] + [x for k in ("skill", "agent", "mcp") for x in
                       (int(v["calls"][k]), len(v["names"][k]))] + [int(v["calls"]["command"])] +
                      ([inv_total(inv, "skills"), inv_total(inv, "agents"), inv["plugins"]] if inv else ["", "", ""]))
    head_n = ["月", "種類", "名前", "回数", "使った人数"]
    rows_n = [[mon, TOOL_LABELS[kind], sheet_safe("（名前は非公開）" if n.startswith("#") else n), v["calls"], len(v["users"])]
              for (mon, kind, n), v in sorted(tool_names.items(), key=lambda x: (x[0][0], -x[1]["calls"]))]
    tables = {"monthly": [head_m] + rows_m, "daily": [head_d] + rows_d,
              "tools": [head_t] + rows_t, "tool_names": [head_n] + rows_n,
              "models": [head_o] + rows_o, "projects": [head_p] + rows_p}
    tables.update(export_metrics(codex_month, work_month, heatmaps, members))
    if YEN:  # 円の列を足す（API換算の USD 列のすぐ後ろ）
        for key in ("monthly", "daily", "models"):
            t = tables[key]
            i = t[0].index("API換算(USD)")
            t[0].insert(i + 1, "API換算(円・1ドル%s円)" % fmt_int(YEN))
            for row in t[1:]:
                row.insert(i + 1, int(round(row[i] * YEN)))
    return tables


def cmd_export(a):
    folder, members = load_members(a)
    tables = export_rows(members, a.date_from, a.date_to)
    if a.json:
        json.dump(tables, sys.stdout, ensure_ascii=False)
        return
    out_dir = os.path.join(folder, "reports")
    os.makedirs(out_dir, exist_ok=True)
    for name, rows in tables.items():
        path = os.path.join(out_dir, "%s_%s.csv" % (period_stem(a), name))
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerows(rows)
        print("CSV: " + path)


def main():
    ap = argparse.ArgumentParser(description="Claude Code と Codex の使用量を数える")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("find-drives", help="同期フォルダの候補を出す").set_defaults(fn=cmd_find_drives)
    p = sp.add_parser("setup", help="名前と格納先を設定する")
    p.add_argument("--name", required=True)
    p.add_argument("--dest", required=True)
    p.add_argument("--projects", action="store_true", help="フォルダ（PJ）別の数も送る")
    p.set_defaults(fn=cmd_setup)
    p = sp.add_parser("retention", help="Claude Code のログを残す日数を延ばす")
    p.add_argument("--days", type=int, default=90)
    p.set_defaults(fn=cmd_retention)
    p = sp.add_parser("collect", help="数えて格納先に書く")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_collect)
    p = sp.add_parser("report", help="格納先の全員分を集計して PDF にする")
    p.add_argument("--dest")
    p.add_argument("--org", default="")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    p.set_defaults(fn=cmd_report)
    p = sp.add_parser("export", help="月別・フォルダ別の表を CSV（または JSON）で出す")
    p.add_argument("--dest")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    p.add_argument("--json", action="store_true", help="CSV を書かずに標準出力へ JSON で出す")
    p.set_defaults(fn=cmd_export)
    for sub in sp.choices.values():
        if sub.prog.split()[-1] in ("collect", "report", "export"):
            sub.add_argument("--yen", type=float, help="金額を円で出す（1ドルあたりの円。例 150）")
    a = ap.parse_args()
    global YEN
    if getattr(a, "yen", None):
        if not 1 <= a.yen <= 1000:
            die("--yen は 1〜1000 の数で指定してください")
        YEN = a.yen
    a.fn(a)


if __name__ == "__main__":
    main()
