"""记录已完成的阶段反馈，并为同类任务选择模型。"""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

LEVELS = ("low", "mid", "high")
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--state", default="wb-router-state.sqlite")
subs = parser.add_subparsers(dest="action", required=True)
choose = subs.add_parser("choose")
choose.add_argument("--id", required=True)
choose.add_argument("--family", required=True, help="同类任务名称")
choose.add_argument("--stage", choices=["retrieve","verify","judge","compose"], required=True)
choose.add_argument("--initial", choices=LEVELS, required=True)
choose.add_argument("--models", required=True, help='JSON文件，含low/mid/high的本机真实模型名')
choose.add_argument("--risk", choices=["ordinary","critical"], default="ordinary")
choose.add_argument("--challenger", help="可实际调用的已确认候选模型，普通风险小流量探索")
choose.add_argument("--pending", action="store_true", help="先登记选择，完成该阶段后才接受反馈")
complete = subs.add_parser("complete")
complete.add_argument("--id", required=True)
fb = subs.add_parser("feedback")
fb.add_argument("--id", required=True)
fb.add_argument("--signal", choices=["satisfied","corrected","followup","unclear"], required=True)
fb.add_argument("--reason", required=True)
fb.add_argument("--attribution", choices=["model","external","unclear"], default="unclear")
prefs = subs.add_parser("explore")
prefs.add_argument("setting", choices=["on","off"])
args = parser.parse_args()
db = sqlite3.connect(args.state, timeout=20)
db.executescript("""
CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, family TEXT, stage TEXT, model TEXT, output TEXT, signature TEXT);
CREATE TABLE IF NOT EXISTS feedback (id TEXT PRIMARY KEY, signal TEXT, attribution TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS policy_choices (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    mode TEXT NOT NULL,
    rollback_after INTEGER
);
CREATE INDEX IF NOT EXISTS policy_choices_scope ON policy_choices (scope, mode);
""")
def respond(value):
    print(json.dumps(value, ensure_ascii=False))
def baseline_signals(model):
    rows = db.execute("""
        SELECT f.signal, d.output, d.signature, p.mode
        FROM decisions d JOIN feedback f ON f.id = d.id
        LEFT JOIN policy_choices p ON p.id = d.id
        WHERE d.family = ? AND d.stage = ? AND d.model = ? AND f.attribution = 'model'
        AND f.signal IN ('satisfied', 'corrected')
        AND (p.mode = 'baseline' OR p.id IS NULL)
        ORDER BY d.rowid DESC
    """, (args.family, args.stage, model))
    signals = []
    for signal, output, signature, mode in rows:
        # Long-lived databases may contain decisions recorded before policy modes existed.
        if mode is None and (json.loads(output).get("exploring") or json.loads(signature)[3] != "ordinary"):
            continue
        signals.append(signal)
        if len(signals) == 3:
            break
    return signals


def escalation_signals(scope, model):
    rows = db.execute("""
        SELECT f.signal
        FROM policy_choices p JOIN decisions d ON d.id = p.id JOIN feedback f ON f.id = d.id
        WHERE p.scope = ? AND p.mode = 'escalated' AND d.model = ?
        AND f.attribution = 'model' AND f.signal IN ('satisfied', 'corrected')
        ORDER BY d.rowid DESC LIMIT 3
    """, (scope, model))
    return [signal for (signal,) in rows]


def trial_signals(scope, target, after, limit, include_adopted=False):
    modes = ("trial", "adopted") if include_adopted else ("trial", "trial")
    rows = db.execute("""
        SELECT f.signal
        FROM policy_choices p JOIN decisions d ON d.id = p.id JOIN feedback f ON f.id = d.id
        WHERE p.scope = ? AND d.model = ? AND p.mode IN (?, ?) AND d.rowid > ?
        AND f.attribution = 'model' AND f.signal IN ('satisfied', 'corrected')
        ORDER BY d.rowid DESC LIMIT ?
    """, (scope, target, *modes, after, limit))
    return [signal for (signal,) in rows]


def alternative(scope, tier, models, history):
    if args.challenger:
        if args.challenger == models[tier]:
            return None
        target_tier = None
        target = args.challenger
    elif tier != "low":
        target_tier = LEVELS[LEVELS.index(tier) - 1]
        target = models[target_tier]
        if target == models[tier]:
            return None
    else:
        return None

    watermark, = db.execute("""
        SELECT COALESCE(MAX(p.rollback_after), 0)
        FROM policy_choices p JOIN decisions d ON d.id = p.id
        WHERE p.scope = ? AND d.model = ?
    """, (scope, target)).fetchone()
    if trial_signals(scope, target, watermark, 3) == ["satisfied"] * 3:
        return target_tier, target, "adopted", "同类同阶段的替代模型最近三次试用均获明确满意，采用该模型"

    if watermark:
        completed_baselines, = db.execute("""
            SELECT COUNT(*)
            FROM policy_choices p JOIN decisions d ON d.id = p.id
            LEFT JOIN pending n ON n.id = d.id
            WHERE p.scope = ? AND p.mode IN ('baseline', 'escalated') AND d.rowid > ? AND n.id IS NULL
        """, (scope, watermark)).fetchone()
        if completed_baselines < 3:
            return None

    sample = int.from_bytes(hashlib.sha256(args.id.encode()).digest()[:4], "big") % 10 == 0
    can_try = bool(args.challenger) or history == ["satisfied"] * 3
    if sample and can_try:
        if trial_signals(scope, target, 0, 2, include_adopted=True) != ["corrected"] * 2:
            return target_tier, target, "trial", "普通风险任务小流量试用替代模型"
    return None


def decide():
    models = json.loads(Path(args.models).read_text(encoding="utf-8"))
    if any(not isinstance(models.get(k), str) or not models[k].strip() for k in LEVELS):
        raise SystemExit("模型绑定未填完整")
    key = json.dumps([args.family, args.stage, args.initial, args.risk, args.challenger, models],
                     sort_keys=True, ensure_ascii=False)
    previous = db.execute("SELECT output, signature FROM decisions WHERE id = ?", (args.id,)).fetchone()
    if previous:
        if previous[1] != key:
            raise SystemExit("该ID已有另一项决定，请使用新的阶段ID")
        return json.loads(previous[0])

    tier = args.initial
    model = models[tier]
    mode = "fixed" if args.risk == "critical" else "baseline"
    reason = "关键风险任务沿用本轮指定档位" if mode == "fixed" else "维持本轮模型初判"
    scope = json.dumps([key, tier], ensure_ascii=False)
    if mode == "baseline":
        history = baseline_signals(model)
        if history[:2] == ["corrected", "corrected"] and tier != "high":
            tier = LEVELS[LEVELS.index(tier) + 1]
            model = models[tier]
            mode = "escalated"
            reason = "同类同阶段基准模型最近两次均有明确模型归因纠正，升一档"
            scope = json.dumps([key, tier], ensure_ascii=False)
            history = escalation_signals(scope, model)
        stable = mode == "baseline" or history == ["satisfied"] * 3
        enabled = db.execute("SELECT value FROM config WHERE key = 'explore'").fetchone() == ("on",)
        if stable and enabled:
            chosen = alternative(scope, tier, models, history)
            if chosen is not None:
                tier, model, mode, reason = chosen

    value = {"id": args.id, "stage": args.stage, "initial": args.initial, "model": model, "tier": tier,
             "mode": mode, "exploring": mode == "trial", "reason": reason}
    db.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?)",
               (args.id, args.family, args.stage, model, json.dumps(value, ensure_ascii=False), key))
    db.execute("INSERT INTO policy_choices (id, scope, mode) VALUES (?, ?, ?)", (args.id, scope, mode))
    if args.pending:
        db.execute("INSERT INTO pending VALUES (?)", (args.id,))
    return value


with db:
    db.execute("BEGIN IMMEDIATE")
    if args.action=="choose": respond(decide())
    elif args.action=="explore":
        db.execute("INSERT OR REPLACE INTO config VALUES('explore',?)",(args.setting,));respond({"exploration":args.setting})
    elif args.action == "complete":
        if not db.execute("SELECT 1 FROM decisions WHERE id=?", (args.id,)).fetchone():
            raise SystemExit("没有对应阶段决定")
        db.execute("DELETE FROM pending WHERE id=?", (args.id,))
        respond({"id": args.id, "complete": True})
    else:
        if not db.execute("SELECT 1 FROM decisions WHERE id=?", (args.id,)).fetchone():
            raise SystemExit("没有对应阶段决定")
        if db.execute("SELECT 1 FROM pending WHERE id=?", (args.id,)).fetchone():
            raise SystemExit("该阶段未完成")
        db.execute("INSERT OR REPLACE INTO feedback VALUES(?,?,?,?)",(args.id,args.signal,args.attribution,args.reason))
        if args.signal == "corrected" and args.attribution == "model":
            db.execute("""
                UPDATE policy_choices SET rollback_after = (SELECT MAX(rowid) FROM decisions)
                WHERE id = ? AND mode = 'adopted' AND rollback_after IS NULL
            """, (args.id,))
        respond({"id":args.id,"saved":True})
db.close()

