#!/usr/bin/env python3
"""Claude Code の使用量を数えて、組織の共有フォルダに集め、PDF レポートにする。

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

TOOL_VERSION = "0.2.0"
SCHEMA = 1
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".claude-usage-check")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
SUBDIR = "claude-usage"  # 格納先の中に作るフォルダ


# ---------------------------------------------------------------- 共通

def claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
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
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h", "responses")


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10 ** 15


def _num(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 10 ** 9)


def clean_text(v, limit):
    if not isinstance(v, str):
        return None
    v = re.sub(r"[\x00-\x1f\x7f<>]", "", v).strip()
    return v[:limit] or None


def validate_member(d):
    """形が正しければ検証済みの dict を、そうでなければ (None, 理由) を返す。"""
    if not isinstance(d, dict) or d.get("schema") != SCHEMA:
        return None, "形式の版が違う"
    member = clean_text(d.get("member"), 40)
    if not member:
        return None, "名前がない"
    days_in = d.get("days")
    if not isinstance(days_in, dict):
        return None, "日別の数字がない"
    days = {}
    for day, r in days_in.items():
        if not (isinstance(day, str) and DATE_RE.match(day) and isinstance(r, dict)):
            return None, "日付の形が違う"
        if not all(_int(r.get(k, 0)) for k in ("sessions", "prompts", "responses")) or not _num(r.get("cost_usd", 0)):
            return None, "数字の形が違う"
        models = {}
        for mn, t in (r.get("models") or {}).items():
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
        days[day] = item
    as_of = d.get("prices_as_of")
    updated = d.get("updated_at")
    return {"schema": SCHEMA, "member": member, "days": days,
            "prices_as_of": as_of if isinstance(as_of, str) and DATE_RE.match(as_of) else "?",
            "updated_at": updated[:25] if isinstance(updated, str) and DATE_RE.match(updated[:10]) else "",
            "tool_version": clean_text(d.get("tool_version"), 20) or ""}, None


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


def local_day(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()
    except (ValueError, AttributeError):
        return None


def collect_days(include_projects):
    """~/.claude/projects の JSONL から日別の数字を作る。会話の本文は保持しない。"""
    root = os.path.join(claude_dir(), "projects")
    files = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    prices = Prices()
    days = defaultdict(lambda: {
        "sessions": set(), "prompts": 0, "responses": 0,
        "models": defaultdict(lambda: dict(empty_tokens(), responses=0)),
        "entrypoints": defaultdict(int), "projects": defaultdict(int),
    })
    seen = set()
    first_day = {}  # セッションは始まった日に1回だけ数える
    for path in files:
        try:
            f = open(path, encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with f:
            for line in f:
                if '"type":"user"' not in line and '"type":"assistant"' not in line \
                        and '"type": "user"' not in line and '"type": "assistant"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                t = d.get("type")
                day = local_day(d.get("timestamp"))
                if t not in ("user", "assistant") or not day:
                    continue
                rec = days[day]
                side = bool(d.get("isSidechain"))
                sid = d.get("sessionId")
                if sid and not side and (sid not in first_day or day < first_day[sid]):
                    first_day[sid] = day
                msg = d.get("message") or {}
                if t == "user":
                    # 人が打った発言だけ数える（ツール結果・自動挿入・サブエージェントは除く）
                    if side or d.get("isMeta") or not isinstance(msg.get("content"), str):
                        continue
                    rec["prompts"] += 1
                    rec["entrypoints"][d.get("entrypoint") or "unknown"] += 1
                    if include_projects and d.get("cwd"):
                        rec["projects"][os.path.basename(d["cwd"].rstrip("/\\")) or d["cwd"]] += 1
                    continue
                model = msg.get("model") or "unknown"
                if model.startswith("<"):  # <synthetic> など API を通っていない応答
                    continue
                if not re.match(r"^claude-[a-z0-9.\-]{1,60}$", model):
                    model = "other"  # Bedrock の ARN など、口座番号が入りうる名前は出さない
                key = (msg.get("id"), d.get("requestId"))
                if key != (None, None):
                    if key in seen:
                        continue
                    seen.add(key)
                u = msg.get("usage") or {}
                cc = u.get("cache_creation") or {}
                w_total = u.get("cache_creation_input_tokens") or 0
                w1 = cc.get("ephemeral_1h_input_tokens") or 0
                m = rec["models"][model]
                m["responses"] += 1
                m["input"] += u.get("input_tokens") or 0
                m["output"] += u.get("output_tokens") or 0
                m["cache_read"] += u.get("cache_read_input_tokens") or 0
                m["cache_write_1h"] += w1
                m["cache_write_5m"] += max(w_total - w1, 0)
                rec["responses"] += 1

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
        }
        if unknown:
            item["unpriced_models"] = sorted(unknown)
        if include_projects:
            item["projects"] = dict(rec["projects"])
        out[day] = item
    return out, len(files), prices.as_of


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
    days, nfiles, as_of = collect_days(cfg.get("include_projects", False))
    if not days:
        die("Claude Code のログが見つかりませんでした: " + os.path.join(claude_dir(), "projects"))
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
    merged.update(days)  # 同じ日は新しい数字で上書き。ログが消えた古い日は残す
    data = {"schema": SCHEMA, "tool_version": TOOL_VERSION, "member": cfg["name"],
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "prices_as_of": as_of, "days": dict(sorted(merged.items()))}
    tot = summarize({cfg["name"]: data}, None, None)
    print("読んだログ: %d ファイル / 期間: %s 〜 %s" % (nfiles, min(days), max(days)))
    print_summary(tot)
    if a.dry_run:
        print("（--dry-run なので書き込んでいません）")
        return
    write_json_atomic(out_path, data)
    print("書き込みました: " + out_path)


# ---------------------------------------------------------------- 集計とレポート

def summarize(members, d_from, d_to):
    per_member, per_day = {}, defaultdict(lambda: defaultdict(int))
    per_model = defaultdict(lambda: defaultdict(float))
    total = defaultdict(float)
    for name, data in members.items():
        m = defaultdict(float)
        active = 0
        for day, r in (data.get("days") or {}).items():
            if (d_from and day < d_from) or (d_to and day > d_to):
                continue
            if r.get("prompts", 0) > 0:
                active += 1
            for k in ("sessions", "prompts", "responses", "cost_usd"):
                m[k] += r.get(k, 0) or 0
                total[k] += r.get(k, 0) or 0
            per_day[day]["prompts"] += r.get("prompts", 0)
            per_day[day]["sessions"] += r.get("sessions", 0)
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
        per_member[name] = m
    return {"members": per_member, "days": dict(sorted(per_day.items())),
            "models": dict(per_model), "total": dict(total)}


def fmt_int(n):
    return "{:,}".format(int(round(n)))


def fmt_tok(n):
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= div:
            return "%.1f%s" % (n / div, unit)
    return fmt_int(n)


def print_summary(s):
    t = s["total"]
    print("  セッション %s / 人の発言 %s / AIの応答 %s / トークン %s / API換算 $%.2f" % (
        fmt_int(t.get("sessions", 0)), fmt_int(t.get("prompts", 0)), fmt_int(t.get("responses", 0)),
        fmt_tok(t.get("tokens", 0)), t.get("cost_usd", 0)))


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


def render_html(org, s, d_from, d_to, as_of):
    t = s["total"]
    n = len(s["members"])
    active = sum(1 for m in s["members"].values() if m["prompts"] > 0)
    period = "%s 〜 %s" % (d_from or (min(s["days"]) if s["days"] else "-"), d_to or (max(s["days"]) if s["days"] else "-"))
    rows = "".join(
        "<tr><td>%s</td><td>%d</td><td>%s</td><td>%s</td><td>%s</td><td>$%s</td><td class='mute'>%s</td></tr>" % (
            html.escape(name), m["active_days"], fmt_int(m["sessions"]), fmt_int(m["prompts"]),
            fmt_tok(m["tokens"]), "{:,.2f}".format(m["cost_usd"]), html.escape(m["updated_at"][:10]))
        for name, m in sorted(s["members"].items(), key=lambda x: -x[1]["prompts"]))
    mrows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>$%s</td></tr>" % (
            html.escape(k), fmt_int(v["responses"]), fmt_tok(v["tokens"]), "{:,.2f}".format(v["cost_usd"]))
        for k, v in sorted(s["models"].items(), key=lambda x: -x[1]["tokens"]))
    kpi = [("利用者", "%d / %d 人" % (active, n)), ("セッション", fmt_int(t.get("sessions", 0))),
           ("人の発言", fmt_int(t.get("prompts", 0))), ("トークン", fmt_tok(t.get("tokens", 0))),
           ("API換算", "$%s" % fmt_int(t.get("cost_usd", 0)))]
    kpis = "".join('<div class="kpi"><div class="k">%s</div><div class="v">%s</div></div>'
                   % (html.escape(k), html.escape(v)) for k, v in kpi)
    return """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Claude 使用量レポート</title>
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
.mute{color:var(--mute)}.bar{fill:var(--accent)}.ax{font-size:10px;fill:var(--mute)}.base{stroke:var(--line)}
.note{color:var(--mute);font-size:11px;margin-top:24px;line-height:1.7}
@page{size:A4;margin:12mm}
</style></head><body><div class="page">
<h1>Claude 使用量レポート</h1>
<div class="sub">%s ／ 期間 %s ／ 作成 %s</div>
<div class="kpis">%s</div>
<h2>日別の発言数（全員）</h2>%s
<h2>メンバー別</h2>
<table><tr><th>名前</th><th>使った日数</th><th>セッション</th><th>人の発言</th><th>トークン</th><th>API換算</th><th>最終送信</th></tr>%s</table>
<h2>モデル別</h2>
<table><tr><th>モデル</th><th>AIの応答</th><th>トークン</th><th>API換算</th></tr>%s</table>
<div class="note">
・対象は Claude Code のみ（claude.ai のチャットは含まない）。会話の中身は集めていない。<br>
・「人の発言」は利用者が入力した回数。「AIの応答」にはサブエージェントの応答も含む。<br>
・「API換算」は Anthropic API の公開単価（%s 時点）で計算した目安で、定額プランの実際の支払額ではない。<br>
・各メンバーの PC に残っているログ（既定では30日）から数えている。送信していない期間は空白になる。
</div></div></body></html>""" % (
        html.escape(org), html.escape(period), date.today().isoformat(), kpis, bar_chart_svg(s["days"]),
        rows, mrows, html.escape(as_of))


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
    for p in sorted(glob.glob(os.path.join(folder, "*.json"))):
        d, why = validate_member(load_json(p))
        stem = os.path.basename(p)[:-5]
        if d is None:
            print("注意: %s は形が正しくないため集計から外しました（%s）。" % (os.path.basename(p), why))
            continue
        if safe_filename(d["member"]) != stem:
            print("注意: %s の中の名前が「%s」でファイル名と違います（ファイル名で集計します）。" % (os.path.basename(p), d["member"]))
        names[d["member"]].append(stem)
        d["member"] = clean_text(stem, 40) or stem
        members[stem] = d
    for n, files in names.items():
        if len(files) > 1:
            print("注意: 名前「%s」のファイルが複数あります: %s" % (n, ", ".join(files)))
    if not members:
        die("集まったファイルがありません: " + folder)
    for v in (a.date_from, a.date_to):
        if v and not DATE_RE.match(v):
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


def export_rows(members, d_from, d_to):
    monthly = defaultdict(lambda: defaultdict(float))
    projects = defaultdict(int)
    for name, data in members.items():
        for day, r in data["days"].items():
            if (d_from and day < d_from) or (d_to and day > d_to):
                continue
            m = monthly[(day[:7], name)]
            m["active_days"] += 1 if r["prompts"] > 0 else 0
            for k in ("sessions", "prompts", "responses", "cost_usd"):
                m[k] += r[k]
            for t in r["models"].values():
                m["tokens"] += sum(t[k] for k in TOKEN_KEYS if k != "responses")
            m["updated_at"] = data.get("updated_at", "")[:10]
            for pj, n in (r.get("projects") or {}).items():
                projects[(day[:7], name, pj)] += n
    head_m = ["月", "名前", "使った日数", "セッション", "人の発言", "AIの応答", "トークン", "API換算(USD)", "最終送信日"]
    rows_m = [[mon, sheet_safe(name), int(v["active_days"]), int(v["sessions"]), int(v["prompts"]),
               int(v["responses"]), int(v["tokens"]), round(v["cost_usd"], 2), v["updated_at"]]
              for (mon, name), v in sorted(monthly.items())]
    head_p = ["月", "名前", "フォルダ", "人の発言"]
    rows_p = [[mon, sheet_safe(name), sheet_safe(pj), n]
              for (mon, name, pj), n in sorted(projects.items(), key=lambda x: (x[0][0], x[0][1], -x[1]))]
    return [head_m] + rows_m, [head_p] + rows_p


def cmd_export(a):
    folder, members = load_members(a)
    monthly, projects = export_rows(members, a.date_from, a.date_to)
    if a.json:
        json.dump({"monthly": monthly, "projects": projects}, sys.stdout, ensure_ascii=False)
        return
    out_dir = os.path.join(folder, "reports")
    os.makedirs(out_dir, exist_ok=True)
    for name, rows in (("monthly", monthly), ("projects", projects)):
        path = os.path.join(out_dir, "%s_%s.csv" % (period_stem(a), name))
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerows(rows)
        print("CSV: " + path)


def main():
    ap = argparse.ArgumentParser(description="Claude Code の使用量を数える")
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
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
