"""本地反馈记忆。实际分级和推理由 WorkBuddy 执行。仅供可选的长期使用。"""
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
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
""")
def respond(value):
    print(json.dumps(value, ensure_ascii=False))
def decide():
    models=json.loads(Path(args.models).read_text(encoding="utf-8"))
    if any(not isinstance(models.get(k),str) or not models[k].strip() for k in LEVELS):
        raise SystemExit("模型绑定未填完整")
    key=json.dumps([args.family,args.stage,args.initial,args.risk,args.challenger,models],sort_keys=True,ensure_ascii=False)
    prev=db.execute("SELECT output,signature FROM decisions WHERE id=?",(args.id,)).fetchone()
    if prev:
        if prev[1]!=key: raise SystemExit("该ID已有另一项决定，请使用新的阶段ID")
        return json.loads(prev[0])
    tier=args.initial
    model=models[tier]
    history=db.execute("""SELECT f.signal,f.attribution FROM decisions d JOIN feedback f ON f.id=d.id
        WHERE d.family=? AND d.stage=? AND d.model=? AND f.attribution='model' AND f.signal IN ('satisfied','corrected') ORDER BY d.rowid DESC LIMIT 3""",
        (args.family,args.stage,model)).fetchall()
    enabled=db.execute("SELECT value FROM config WHERE key='explore'").fetchone()==('on',)
    sample=int.from_bytes(hashlib.sha256(args.id.encode()).digest()[:4],'big')%10==0
    def recent_failures(candidate):
        votes=db.execute("""SELECT f.signal FROM decisions d JOIN feedback f ON f.id=d.id
            WHERE d.family=? AND d.stage=? AND d.model=? AND f.attribution='model'
            AND f.signal IN ('satisfied','corrected') ORDER BY d.rowid DESC LIMIT 2""",
            (args.family,args.stage,candidate)).fetchall()
        return votes==[('corrected',),('corrected',)]
    why="维持本轮模型初判"; trial=False
    if len(history)>=2 and all(s=="corrected" for s,_ in history[:2]) and tier!="high":
        tier=LEVELS[LEVELS.index(tier)+1]; model=models[tier]; why="同类同阶段该模型最近两次有效反馈均指出能力失误，升一档"
    elif enabled and sample and args.risk=="ordinary":
        if args.challenger and args.challenger!=model and not recent_failures(args.challenger):
            model=args.challenger;why="小流量试用已确认可调用的新候选";trial=True
        elif not args.challenger and tier!="low" and not recent_failures(models[LEVELS[LEVELS.index(tier)-1]]) and len(history)==3 and all(s=="satisfied" for s,_ in history):
            tier=LEVELS[LEVELS.index(tier)-1];model=models[tier];why="近期三次明确满意，小流量试低一档";trial=True
    val={"id":args.id,"stage":args.stage,"initial":args.initial,"model":model,"tier":tier if not (trial and args.challenger==model) else None,"exploring":trial,"reason":why}
    db.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?)",(args.id,args.family,args.stage,model,json.dumps(val,ensure_ascii=False),key))
    return val
with db:
    db.execute("BEGIN IMMEDIATE")
    if args.action=="choose": respond(decide())
    elif args.action=="explore":
        db.execute("INSERT OR REPLACE INTO config VALUES('explore',?)",(args.setting,));respond({"exploration":args.setting})
    else:
        if not db.execute("SELECT 1 FROM decisions WHERE id=?",(args.id,)).fetchone():raise SystemExit("没有对应阶段决定")
        db.execute("INSERT OR REPLACE INTO feedback VALUES(?,?,?,?)",(args.id,args.signal,args.attribution,args.reason))
        respond({"id":args.id,"saved":True})
db.close()

