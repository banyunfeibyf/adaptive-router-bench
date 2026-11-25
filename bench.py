#!/usr/bin/env python3
"""Record and export a WorkBuddy model comparison without making model calls."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile


DEFAULT_ARMS = ("low", "mid", "high", "route")
LEVELS = DEFAULT_ARMS[:3]
ARMS = DEFAULT_ARMS + ("native_auto",)
QUESTION_FILE = Path(__file__).with_name("questions.json")
UNKNOWN = "unknown"


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def write_json(path, value):
    path.write_text(dump_json(value), encoding="utf-8")


def check(condition, message):
    if not condition:
        raise ValueError(message)


def topic_rows(question_file):
    bank = read_json(question_file)
    check(isinstance(bank, dict) and isinstance(bank.get("topics"), list), "题库须包含 topics 数组")
    rows = []
    seen = set()
    for topic in bank["topics"]:
        check(isinstance(topic, dict), "题库主题格式不正确")
        topic_id = topic.get("id")
        check(nonempty(topic_id) and topic_id.isascii() and topic_id.isalnum(), "主题 id 须为 ASCII 字母或数字")
        check(topic_id not in seen, f"主题 id 重复：{topic_id}")
        seen.add(topic_id)
        check(nonempty(topic.get("title")), f"主题缺少标题：{topic_id}")
        questions = topic.get("questions")
        check(isinstance(questions, list) and len(questions) == 3 and all(map(nonempty, questions)),
              f"主题必须有三道非空题目：{topic_id}")
        for turn, question in enumerate(questions, 1):
            rows.append({"id": f"{topic_id}.{turn}", "topic": topic_id, "title": topic["title"],
                         "turn": turn, "question": question})
    check(rows, "题库为空")
    return rows


def bank_hash(rows):
    return hashlib.sha256(dump_json(rows).encode("utf-8")).hexdigest()


def new_config(arms, rows):
    slots = LEVELS + ("router",)
    if "native_auto" in arms:
        slots += ("native_auto",)
    return {
        "schema": 1,
        "arms": list(arms),
        "questions_sha256": bank_hash(rows),
        "binding_notes": "",
        "models": {slot: {"model_id": None, "evidence": None, "capabilities": None, "notes": ""}
                   for slot in slots},
        "arm_bills": {arm: {"kind": None, "start": None, "end": None, "unit": None, "channel": None,
                           "evidence": None, "exclusive": None, "first_id": None, "last_id": None,
                           "record_count": None} for arm in arms},
        "notes": "",
    }


def init_run(args, rows):
    arms = DEFAULT_ARMS + (("native_auto",) if args.native_auto else ())
    args.out.mkdir(parents=True, exist_ok=False)
    try:
        for arm in arms:
            (args.out / "records" / arm).mkdir(parents=True)
        write_json(args.out / "run.json", new_config(arms, rows))
    except BaseException:
        shutil.rmtree(args.out)
        raise
    print(f"{args.out} 已初始化；{len(arms)} 臂 × {len(rows)} 题 = {len(arms) * len(rows)} 条计划记录。")


def load_config(args, rows):
    config = read_json(args.out / "run.json")
    check(isinstance(config, dict) and config.get("schema") == 1, "run.json 版本无效")
    check(config.get("arms") in [list(DEFAULT_ARMS), list(ARMS)], "run.json 的策略列表已更改")
    check(config.get("questions_sha256") == bank_hash(rows), "题库与首次 init 的版本不一致")
    check(isinstance(config.get("models"), dict), "run.json 的 models 必须为对象")
    check(isinstance(config.get("arm_bills"), dict), "run.json 的 arm_bills 必须为对象")
    if hasattr(args, "arm"):
        check(args.arm in config["arms"], f"本轮未启用 {args.arm}")
    return config


def meta_template(arm):
    template = {
        "session_ref": None, "call_log_complete": None, "fallback": None, "notes": "",
        "calls": [{"kind": "answer", "outcome": "used", "model_id": None, "evidence": None,
                   "actual": None, "estimate": None}],
    }
    if arm == "route":
        template["route"] = {"level": None, "evidence": None, "notes": ""}
        template["calls"].insert(0, {"kind": "classification", "outcome": "used", "model_id": None,
                                     "evidence": None, "actual": None, "estimate": None})
    return template


def amount(value):
    check(type(value) in (str, int, float) and str(value).strip(), "金额或积分必须是数值")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("金额或积分必须是数值") from exc
    check(number.is_finite() and number >= 0, "金额或积分必须为有限非负值")
    return number


def optional_text(obj, key, prefix):
    check(obj.get(key) is None or isinstance(obj[key], str), f"{prefix}.{key} 须为文本或 null")


def optional_bool(obj, key):
    check(obj.get(key) is None or type(obj[key]) is bool, f"{key} 须为 true、false 或 null")


def validate_cost(value, name):
    if value is None:
        return
    check(isinstance(value, dict), f"{name} 须为对象或 null")
    amount(value.get("amount"))
    for key in ("unit", "channel"):
        check(nonempty(value.get(key)), f"{name}.{key} 缺失")
    source = "evidence" if name == "actual" else "method"
    check(nonempty(value.get(source)), f"{name}.{source} 缺失")


def validate_meta(meta, arm):
    check(isinstance(meta, dict) and isinstance(meta.get("calls"), list), "meta 须包含 calls 数组")
    for key in ("session_ref", "notes"):
        optional_text(meta, key, "meta")
    optional_bool(meta, "call_log_complete")
    optional_bool(meta, "fallback")
    if arm == "route":
        route = meta.get("route")
        check(isinstance(route, dict) and route.get("level") in (*LEVELS, None),
              "route.level 须为 low、mid、high 或 null")
        for key in ("evidence", "notes"):
            optional_text(route, key, "route")
    for call in meta["calls"]:
        check(isinstance(call, dict), "calls 每项须为对象")
        check(call.get("kind") in ("answer", "classification", "classify_and_answer", "other"), "调用 kind 无效")
        check(call.get("outcome") in ("used", "discarded", "failed", UNKNOWN), "调用 outcome 无效")
        for key in ("model_id", "evidence", "notes"):
            optional_text(call, key, "call")
        for key in ("actual", "estimate"):
            validate_cost(call.get(key), key)
    check(sum(call["outcome"] == "used" and call["kind"] in ("answer", "classify_and_answer") for call in meta["calls"]) <= 1,
          "每题最多一个 outcome=used 的 answer 调用")


def records_for(path, arm, rows):
    records = {}
    expected = {row["id"]: index for index, row in enumerate(rows)}
    directory = path / "records" / arm
    check(directory.is_dir(), f"缺少记录目录：{directory}")
    for file in sorted(directory.glob("*.json")):
        row_id = file.stem
        check(row_id in expected, f"未知记录：{arm}/{row_id}")
        record = read_json(file)
        check(isinstance(record, dict) and record.get("id") == row_id and record.get("arm") == arm
              and (nonempty(record.get("answer")) or (record.get("status")=="failed" and nonempty(record.get("failure")))), f"损坏的记录：{arm}/{row_id}")
        validate_meta(record.get("meta"), arm)
        records[row_id] = record
    for index, row in enumerate(rows):
        if row["id"] not in records:
            check(len(records) == index, f"{arm} 存在跳题；首个缺失是 {row['id']}")
            break
    return {row["id"]: records[row["id"]] for row in rows[:len(records)]}


def show_next(arm, rows, records):
    if len(records) == len(rows):
        print(f"{arm} 已记录全部题目。")
        return
    row = rows[len(records)]
    print(f"{row['id']}  {row['title']}")
    for prior in rows[len(records) - row["turn"] + 1:len(records)]:
        print(f"\n前文 · 用户 {prior['turn']}\n{prior['question']}")
        print(f"\n前文 · 助手 {prior['turn']}\n{records[prior['id']]['answer']}")
    print(f"\n本轮用户\n{row['question']}")


def record_answer(args, rows, records):
    check(len(records) < len(rows), f"{args.arm} 已记录全部题目")
    row = rows[len(records)]
    check(args.id == row["id"], f"{args.arm} 下一题是 {row['id']}；拒绝重复记录或跳题")
    answer = args.file.read_bytes().decode("utf-8")
    check(nonempty(answer), "原答或技术失败说明为空")
    meta = read_json(args.meta)
    validate_meta(meta, args.arm)
    final = args.out / "records" / args.arm / f"{args.id}.json"
    output = {"id": args.id, "arm": args.arm, "answer": answer, "meta": meta}
    if args.failed:
        output.update(status="failed", failure=answer, answer="")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=final.parent,
                                         prefix=".record-", delete=False) as temporary:
            temp_path = Path(temporary.name)
            temporary.write(dump_json(output))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.link(temp_path, final)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    print(f"已记录 {args.arm}/{args.id}。")


def evidence_status(arm, meta, models):
    invalid = []
    unknown = []
    used_answers = [call for call in meta["calls"] if call["kind"] in ("answer", "classify_and_answer") and call["outcome"] == "used"]
    role = arm
    has_binding_check = arm != "native_auto"
    if meta.get("fallback") is True:
        invalid.append("已报告降级或回退")
    elif meta.get("fallback") is None:
        unknown.append("是否回退未知")
    if meta.get("call_log_complete") is not True:
        unknown.append("调用清单完整性未确认")
    if not nonempty(meta.get("session_ref")):
        unknown.append("缺少会话证据")
    if arm == "route":
        route = meta["route"]
        role = route.get("level")
        if role is None or not nonempty(route.get("evidence")):
            unknown.append("路由分级或分级证据缺失")
        combined = [c for c in meta["calls"] if c["kind"] == "classify_and_answer" and c["outcome"] == "used"]
        if combined and role != "low":
            invalid.append("同一次分类直答仅用于低档")
        if any(isinstance(models.get(k),dict) for k in ("low","router")) and (models.get("low") or {}).get("model_id") != (models.get("router") or {}).get("model_id"):
            invalid.append("分类器绑定与轻量模型不一致")
        classifiers = [call for call in meta["calls"]
                       if call["kind"] in ("classification", "classify_and_answer") and call["outcome"] == "used"]
        if not classifiers:
            unknown.append("缺少实际用于分级的调用")
        for classifier in classifiers:
            check_binding("router", classifier, models, invalid, unknown)
    if not used_answers:
        unknown.append("缺少生成实际答案的调用")
    elif has_binding_check and role is not None:
        check_binding(role, used_answers[0], models, invalid, unknown)
    elif not all(nonempty(used_answers[0].get(key)) for key in ("model_id", "evidence")):
        unknown.append("缺少自动策略所选模型或该调用证据")
    for call in meta["calls"]:
        if call["outcome"] == UNKNOWN:
            unknown.append("调用结果未知")
        if not all(nonempty(call.get(key)) for key in ("model_id", "evidence")):
            unknown.append("至少一次调用缺少模型标识或调用证据")
    status = "invalid" if invalid else UNKNOWN if unknown else "reported_verified"
    return {"status": status, "issues": list(dict.fromkeys(invalid + unknown))}


def check_binding(role, call, models, invalid, unknown):
    slot = models.get(role)
    if not isinstance(slot, dict) or not all(nonempty(slot.get(key)) for key in ("model_id", "evidence")):
        unknown.append(f"{role} 缺少模型绑定或绑定证据")
        return
    if not all(nonempty(call.get(key)) for key in ("model_id", "evidence")):
        unknown.append(f"{role} 缺少实际模型标识或调用证据")
    elif slot["model_id"].strip() != call["model_id"].strip():
        invalid.append(f"{role} 实际模型与计划绑定不符")


def bill_for(bill, record_ids):
    if not isinstance(bill, dict):
        return None, "臂级账单非对象"
    if not any(value is not None and value != "" for value in bill.values()):
        return None, "未填写臂级账单"
    try:
        kind = bill.get("kind")
        check(kind in ("cumulative_spend", "remaining_balance"), "未知账单类型")
        check(bill.get("exclusive") is True, "未确认记录区间内账单仅涵盖该臂")
        check(record_ids and bill.get("first_id") == record_ids[0] and bill.get("last_id") == record_ids[-1]
              and type(bill.get("record_count")) is int and bill["record_count"] == len(record_ids),
              "账单范围与本次记录不一致")
        check(all(nonempty(bill.get(key)) for key in ("unit", "channel", "evidence")), "缺少账单单位、渠道或证据")
        start, end = amount(bill.get("start")), amount(bill.get("end"))
        used = end - start if kind == "cumulative_spend" else start - end
        check(used >= 0, "账单差额为负")
        return {"channel": bill["channel"], "unit": bill["unit"], "amount": str(used),
                "evidence": bill["evidence"], "records_covered": len(record_ids)}, None
    except ValueError as exc:
        return None, str(exc)


def cost_summary(arm, records, bill, planned):
    actual = defaultdict(Decimal)
    estimates = defaultdict(Decimal)
    unconfirmed_log = actual_missing = estimate_missing = calls_count = 0
    for record in records.values():
        meta = record["meta"]
        if meta.get("call_log_complete") is not True or not meta["calls"]:
            unconfirmed_log += 1
        for call in meta["calls"]:
            calls_count += 1
            for name, totals in (("actual", actual), ("estimate", estimates)):
                entry = call.get(name)
                if entry is None:
                    if name == "actual":
                        actual_missing += 1
                    else:
                        estimate_missing += 1
                else:
                    source = entry["method"] if name == "estimate" else None
                    totals[entry["channel"], entry["unit"], source] += amount(entry["amount"])
    arm_bill, bill_issue = bill_for(bill, list(records))

    def group(totals):
        return [{"channel": key[0], "unit": key[1], "amount": str(value),
                 **({"method": key[2]} if key[2] is not None else {})}
                for key, value in sorted(totals.items())]

    complete_actual = bool(records) and bool(calls_count) and unconfirmed_log == actual_missing == 0
    return {
        "arm": arm, "records_present": len(records), "records_planned": planned,
        "selected_actual_source": "arm_bill" if arm_bill else "per_call" if complete_actual else UNKNOWN,
        "selected_actual_totals": [arm_bill] if arm_bill else group(actual) if complete_actual else None,
        "actual_scope": "full_arm" if len(records) == planned else "recorded_prefix",
        "arm_bill_issue": bill_issue,
        "per_call_actual_known_subtotals": group(actual),
        "per_call_estimates_by_method": group(estimates),
        "calls_reported": calls_count, "calls_without_actual_amount": actual_missing,
        "calls_without_estimate": estimate_missing, "records_with_unconfirmed_call_log": unconfirmed_log,
    }


def model_mapping(config):
    distinct = defaultdict(list)
    for role, slot in config["models"].items():
        if isinstance(slot, dict) and nonempty(slot.get("model_id")):
            distinct[slot["model_id"].strip()].append(role)
    return [{"model_id": identity, "roles": roles, "binding_notes": config.get("binding_notes")}
            for identity, roles in distinct.items() if len(roles) > 1]


def packed_blind(rows, by_arm):
    rng = secrets.SystemRandom()
    blind_rows = []
    mapping = []
    for start in range(0, len(rows), 3):
        theme = rows[start:start + 3]
        theme_series = []
        for arm, records in by_arm.items():
            record_prefix = [row for row in theme if row["id"] in records]
            if not record_prefix:
                continue
            code = secrets.token_hex(12)
            mapping.append({"series_id": code, "arm": arm, "topic_id": theme[0]["topic"]})
            theme_series.append([
                {"series_id": code, "topic_id": row["topic"], "title": row["title"], "turn": row["turn"],
                 "question": row["question"], "answer": records[row["id"]]["answer"]}
                for row in record_prefix if records[row["id"]].get("status")!="failed"
            ])
        rng.shuffle(theme_series)
        for series in theme_series:
            blind_rows.extend(series)
    return blind_rows, mapping


def pack_results(args, rows, config):
    by_arm = {arm: records_for(args.out, arm, rows) for arm in config["arms"]}
    planned = len(rows)
    expected = planned * len(by_arm)
    recorded = sum(map(len, by_arm.values()))
    check(recorded > 0, "尚无结果可回传")
    check(recorded == expected or args.partial, f"仅记录 {recorded}/{expected}；部分回传请加 --partial")
    integrity = {}
    for arm, records in by_arm.items():
        evidence = {row_id: evidence_status(arm, rec["meta"], config["models"]) for row_id, rec in records.items()}
        integrity[arm] = {"recorded": len(records), "planned": planned,
                          "missing_ids": [row["id"] for row in rows if row["id"] not in records],
                          "model_evidence_counts": dict(Counter(item["status"] for item in evidence.values())),
                          "model_evidence": evidence}
    failures={arm:[i for i,r in rs.items() if r.get("status")=="failed"] for arm,rs in by_arm.items()}
    failed_count=sum(map(len,failures.values()))
    all_verified = all(all(item["status"] == "reported_verified" for item in arm["model_evidence"].values())
                       for arm in integrity.values())
    costs = [cost_summary(arm, records, config["arm_bills"].get(arm), planned)
             for arm, records in by_arm.items()]
    blind_rows, mapping = packed_blind(rows, by_arm)
    base = args.out / "exports"
    base.mkdir(exist_ok=True)
    designation = ("complete_with_failures" if failed_count else "complete") if recorded == expected else "partial"
    prefix = f"{designation}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-"
    staging = Path(tempfile.mkdtemp(prefix=".packing-", dir=base))
    result = base / (prefix + secrets.token_hex(8))
    try:
        (staging / "blind").mkdir()
        (staging / "private").mkdir()
        with (staging / "blind" / "answers.jsonl").open("w", encoding="utf-8") as file:
            for row in blind_rows:
                file.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        private = staging / "private"
        write_json(private / "blind-map.json", mapping)
        write_json(private / "costs.json", costs)
        write_json(private / "run.json", config)
        write_json(private / "raw-records.json", by_arm)
        write_json(private / "integrity.json", {
            "record_coverage": designation, "recorded": recorded, "expected": expected, "delivered":recorded-failed_count, "failed":failures,
            "model_evidence": "reported_verified" if all_verified and recorded == expected else "incomplete_or_invalid",
            "all_arm_actual_costs_known": recorded == expected and all(item["selected_actual_totals"] is not None for item in costs),
            "arms": integrity, "shared_model_bindings": model_mapping(config),
            "blind_payload_contains_original_answer_text": True,
        })
        check(not result.exists(), f"导出目录已存在：{result}")
        staging.rename(result)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    zipped = shutil.make_archive(str(result), "zip", root_dir=result)
    print(f"{zipped}\n记录 {recorded}/{expected}（{designation}）。模型证据及账单范围见 private。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "meta", "next", "record", "pack"):
        sub = subs.add_parser(name)
        if name != "meta":
            sub.add_argument("--out", type=Path, default=Path("wb-results"))
            sub.add_argument("--questions", type=Path, default=QUESTION_FILE)
        if name in ("next", "record", "meta"):
            sub.add_argument("--arm", required=True, choices=ARMS)
        if name == "init":
            sub.add_argument("--native-auto", action="store_true")
        if name == "record":
            sub.add_argument("--id", required=True)
            sub.add_argument("--failed", action="store_true", help="本题彻底失败；--file 为失败原因，不进匿名答案")
            sub.add_argument("--file", required=True, type=Path)
            sub.add_argument("--meta", required=True, type=Path)
        if name == "pack":
            sub.add_argument("--partial", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "meta":
            print(dump_json(meta_template(args.arm)), end="")
            return 0
        rows = topic_rows(args.questions)
        if args.command == "init":
            init_run(args, rows)
            return 0
        config = load_config(args, rows)
        if args.command == "pack":
            pack_results(args, rows, config)
        else:
            records = records_for(args.out, args.arm, rows)
            if args.command == "next":
                show_next(args.arm, rows, records)
            else:
                record_answer(args, rows, records)
        return 0
    except (ValueError, OSError) as exc:
        print(f"无法完成：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
