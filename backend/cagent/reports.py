from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime

from .cases import CaseStore
from .loans import prioritize
from .memory import MemoryStore
from .scoring import choose_strategy, max_overdue_days, score_customer

EMPTY = (None, "", [], {})


def session_memory(conn: sqlite3.Connection, session_id: str) -> dict:
    return {k: v["value"] for k, v in MemoryStore(conn).all("session", session_id).items()}


def _hits(conn: sqlite3.Connection, where: str, params: tuple) -> list[dict]:
    return [dict(r) for r in conn.execute(
        f"SELECT rule_id, rule_type, level, action, checkpoint, ts FROM audit_log WHERE rule_id IS NOT NULL AND {where} "
        "ORDER BY rowid", params)]


def _build(conn: sqlite3.Connection, customer_id: str, policy: dict, now: datetime, session: dict | None,
           mem: dict) -> dict:
    loans = prioritize(conn, customer_id, policy)
    days = max_overdue_days(conn, customer_id)
    will = mem.get("willingness") or score_customer(conn, customer_id, policy, mem.get("intents", [])).model_dump(mode="json")
    if session:
        emotions = mem.get("emotions", [])
        hits = _hits(conn, "session_id=?", (session["session_id"],))
        checks = conn.execute("SELECT COUNT(*) FROM audit_log WHERE session_id=? AND checkpoint IN ('坐席话术', '运营操作')",
                              (session["session_id"],)).fetchone()[0]
    else:
        emotions = [{"emotion": r["emotion_type"], "intensity": r["emotion_intensity"], "time": r["record_time"]}
                    for r in conn.execute("SELECT emotion_type, emotion_intensity, record_time FROM fact_emotion_log "
                                          "WHERE customer_id=? ORDER BY record_time DESC LIMIT 5", (customer_id,))]
        hits = _hits(conn, "customer_id=?", (customer_id,))
        checks = conn.execute("SELECT COUNT(*) FROM audit_log WHERE customer_id=?", (customer_id,)).fetchone()[0]
    history = Counter(r["call_result"] for r in conn.execute(
        "SELECT call_result FROM fact_call_history WHERE customer_id=?", (customer_id,)))
    violations = [r["violated_rule_id"] for r in conn.execute(
        "SELECT violated_rule_id FROM fact_call_history WHERE customer_id=? AND COALESCE(violated_rule_id,'')<>''",
        (customer_id,))]
    quality = [r["issue"] for r in conn.execute("SELECT issue FROM data_quality WHERE customer_id=?", (customer_id,))]
    return {
        "报告类型": "单客户催收报告" if session else "对话前预报告",
        "客户编号": customer_id,
        "会话编号": session["session_id"] if session else None,
        "生成时间": now.isoformat(timespec="seconds"),
        "逾期概况": {"逾期笔数": len(loans.items), "欠款总额": f"{loans.total_amount:.0f}元", "最长逾期天数": days,
                 "处理优先级": loans.suggestion, "排序依据": loans.basis},
        "还款意愿评估": {"评分": will["score"], "等级": will["level"], "置信度": will["confidence"],
                   "因素": [{"因素": f["name"], "得分": f["score"], "权重": f["weight"], "依据": f["note"]}
                          for f in will["factors"]],
                   "对话调整": will["adjustments"], "需人工复核": "是" if will["needs_review"] else "否"},
        "情绪感知": {"轨迹": emotions,
                 "峰值": max(emotions, key=lambda e: e.get("intensity") or 0) if emotions else None,
                 "最近": emotions[-1] if session and emotions else (emotions[0] if emotions else None)},
        "合规审计": {"审核记录数": checks, "命中规则": hits, "历史通话违规": violations, "门禁拦截": mem.get("gate")},
        "协商记录": {"方案协商": mem.get("negotiation", []), "还款承诺": mem.get("promises", []),
                 "减免申请": mem.get("waivers", []), "宽限请求": mem.get("grace_request"),
                 "历史催收结果": dict(history)},
        "建议策略": mem.get("strategy") or choose_strategy(policy, will["level"], days),
        "数据冲突": list(dict.fromkeys(loans.conflicts + quality)),
    }


def session_report(conn: sqlite3.Connection, session_id: str, policy: dict, now: datetime | None = None) -> dict:
    session = CaseStore(conn).get_session(session_id)
    if session is None:
        raise ValueError(f"会话不存在: {session_id}")
    return _build(conn, session["customer_id"], policy, now or datetime.now(), session, session_memory(conn, session_id))


def customer_report(conn: sqlite3.Connection, customer_id: str, policy: dict, now: datetime | None = None) -> dict:
    if conn.execute("SELECT 1 FROM dim_customer WHERE customer_id=?", (customer_id,)).fetchone() is None:
        raise ValueError(f"客户不存在: {customer_id}")
    latest = CaseStore(conn).latest_artifact(customer_id, "催收报告")
    if latest:
        return latest["content"]["data"]
    return _build(conn, customer_id, policy, now or datetime.now(), None, {})


def _line(value) -> str:
    if isinstance(value, dict):
        return "；".join(f"{k}：{_line(v)}" for k, v in value.items() if v not in EMPTY) or "无"
    if isinstance(value, list):
        return "、".join(_line(v) for v in value) or "无"
    return "无" if value in EMPTY else str(value)


def _md(value) -> list[str]:
    if value in EMPTY:
        return ["- 无"]
    if isinstance(value, dict):
        lines = []
        for k, v in value.items():
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                lines += [f"- {k}：", *(f"  - {_line(x)}" for x in v)]
            else:
                lines.append(f"- {k}：{_line(v)}")
        return lines
    if isinstance(value, list):
        return [f"- {_line(v)}" for v in value]
    return [f"- {value}"]


def render_report_markdown(report: dict, sections: list[str]) -> str:
    lines = [f"# {report['报告类型']}：{report['客户编号']}", "", render_customer_text(report), "",
             f"- 会话：{report['会话编号'] or '无'}", f"- 生成时间：{report['生成时间']}"]
    for sec in [*sections, "数据冲突"]:
        lines += ["", f"## {sec}", *_md(report.get(sec))]
    return "\n".join(lines)


def render_customer_text(report: dict) -> str:
    o, w = report["逾期概况"], report["还款意愿评估"]
    text = (f"{report['客户编号']}：逾期{o['逾期笔数']}笔，欠款合计{o['欠款总额']}，最长逾期{o['最长逾期天数']}天；"
            f"还款意愿{w['评分']}分（{w['等级']}，置信度{w['置信度']}），建议策略：{report['建议策略']}。{o['处理优先级']}。")
    if report["数据冲突"]:
        text += f"注意：存在{len(report['数据冲突'])}条数据冲突，需人工复核。"
    return text


def summary_report(conn: sqlite3.Connection, now: datetime, policy: dict, month: str | None = None) -> dict:
    month = month or now.strftime("%Y-%m")
    notes: list[str] = []

    def calls_in(m: str) -> list:
        return conn.execute("SELECT * FROM fact_call_history WHERE substr(call_time,1,7)=?", (m,)).fetchall()

    rows = calls_in(month)
    if not rows:
        last = conn.execute("SELECT MAX(substr(call_time,1,7)) FROM fact_call_history").fetchone()[0]
        if last:
            notes.append(f"{month} 无催收记录，以下统计最近有记录的月份 {last}")
            month, rows = last, calls_in(last)
    positive = set(policy["scoring"]["call_result"]["positive"])
    results = Counter(r["call_result"] for r in rows)
    total = len(rows)
    all_results = Counter(r["call_result"] for r in conn.execute("SELECT call_result FROM fact_call_history"))
    repay = conn.execute("SELECT COUNT(*), COALESCE(SUM(repay_amount), 0) FROM fact_repayment "
                         "WHERE substr(repay_time,1,7)=?", (month,)).fetchone()
    if not repay[0]:
        notes.append(f"{month} 无还款记录")
    levels = Counter(score_customer(conn, r["customer_id"], policy).level
                     for r in conn.execute("SELECT customer_id FROM dim_customer ORDER BY customer_id"))
    handoffs = conn.execute("SELECT COUNT(*) FROM artifacts WHERE kind='移交包' AND substr(created_at,1,7)=?",
                            (month,)).fetchone()[0]
    return {
        "报告类型": "催收汇总报告", "统计月份": month, "生成时间": now.isoformat(timespec="seconds"), "说明": notes,
        "本期指标": {"催收次数": total, "覆盖客户数": len({r["customer_id"] for r in rows}),
                 "正向结果率": f"{sum(results[k] for k in positive) / total:.0%}" if total else "无数据",
                 "结果分布": dict(results), "渠道分布": dict(Counter(r["call_method"] for r in rows))},
        "全量指标": {"历史催收总次数": sum(all_results.values()), "结果分布": dict(all_results)},
        "实际还款": {"笔数": repay[0], "金额": f"{repay[1]:.0f}元"},
        "意愿分布": dict(levels),
        "合规审计": {"通话违规": dict(Counter(r["violated_rule_id"] for r in rows if r["violated_rule_id"])),
                 "系统审计命中": dict(Counter(h["rule_id"] for h in _hits(conn, "substr(ts,1,7)=?", (month,))))},
        "人工接管次数": handoffs,
    }


_AUDIT_LABELS = {"R001": "辱骂", "R002": "威胁", "R003": "虚假承诺", "R004": "违规时段", "R005": "频次超限"}
_ALL_WORDS = ("全部", "所有", "全量")


def report_customer_ids(conn: sqlite3.Connection, customer_id: str | None, text: str = "") -> list[str]:
    """原文要求全部、所有或全量，或没有客户编号时，按全部客户出报告。"""
    if customer_id and not any(w in text for w in _ALL_WORDS):
        if conn.execute("SELECT 1 FROM dim_customer WHERE customer_id=?", (customer_id,)).fetchone() is None:
            raise ValueError(f"客户不存在: {customer_id}")
        return [customer_id]
    return [r["customer_id"] for r in conn.execute("SELECT customer_id FROM dim_customer ORDER BY customer_id")]


def _month_calls(conn: sqlite3.Connection, customer_id: str, month: str) -> list:
    return conn.execute("SELECT * FROM fact_call_history WHERE customer_id=? AND substr(call_time,1,7)=?",
                        (customer_id, month)).fetchall()


def _audit_counts(conn: sqlite3.Connection, customer_id: str, month: str) -> dict[str, int]:
    counts: Counter = Counter()
    for r in conn.execute("SELECT rule_id FROM audit_log WHERE customer_id=? AND substr(ts,1,7)=? AND rule_id IS NOT NULL",
                          (customer_id, month)):
        counts[r["rule_id"]] += 1
    for r in conn.execute("SELECT violated_rule_id FROM fact_call_history WHERE customer_id=? AND substr(call_time,1,7)=? "
                          "AND COALESCE(violated_rule_id,'')<>''", (customer_id, month)):
        counts[r["violated_rule_id"]] += 1
    return {_AUDIT_LABELS.get(k, k): n for k, n in sorted(counts.items())}


def _handoff_count(conn: sqlite3.Connection, customer_id: str, month: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM artifacts a JOIN sessions s ON s.session_id=a.session_id "
        "WHERE a.kind='移交包' AND s.customer_id=? AND substr(a.created_at,1,7)=?",
        (customer_id, month)).fetchone()[0]


def collection_eval_report(conn: sqlite3.Connection, now: datetime, policy: dict,
                           customer_id: str | None = None, text: str = "", month: str | None = None) -> dict:
    month = month or now.strftime("%Y-%m")
    ids = report_customer_ids(conn, customer_id, text)
    customers = []
    for cid in ids:
        rows = _month_calls(conn, cid, month)
        results = Counter(r["call_result"] for r in rows)
        will = score_customer(conn, cid, policy)
        customers.append({
            "客户编号": cid,
            "催收次数": len(rows),
            "还款意愿": {"评分": will.score, "等级": will.level},
            "合规审计": _audit_counts(conn, cid, month),
            "分期协商次数": results.get("协商分期", 0),
            "人工接管次数": _handoff_count(conn, cid, month),
            "建议策略": choose_strategy(policy, will.level, max_overdue_days(conn, cid)),
            "结果分布": dict(results),
        })
    notes = []
    if not any(c["催收次数"] for c in customers):
        notes.append(f"{month} 无催收记录")
    scope = "单客户" if customer_id and not any(w in text for w in _ALL_WORDS) else "全量"
    return {"报告类型": "本月催收评估报告", "统计月份": month, "生成时间": now.isoformat(timespec="seconds"),
            "范围": scope, "说明": notes, "客户": customers}


def compliance_audit_report(conn: sqlite3.Connection, now: datetime, customer_id: str | None = None,
                            text: str = "", month: str | None = None) -> dict:
    month = month or now.strftime("%Y-%m")
    ids = report_customer_ids(conn, customer_id, text)
    customers = [{"客户编号": cid, "命中": _audit_counts(conn, cid, month),
                  "人工接管次数": _handoff_count(conn, cid, month)} for cid in ids]
    scope = "单客户" if customer_id and not any(w in text for w in _ALL_WORDS) else "全量"
    return {"报告类型": "合规审计报告", "统计月份": month, "生成时间": now.isoformat(timespec="seconds"),
            "范围": scope, "客户": customers}


def render_eval_text(report: dict) -> str:
    lines = [f"{report['统计月份']} 本月催收评估报告", f"范围：{report['范围']}"]
    lines += [f"说明：{n}" for n in report["说明"]]
    for c in report["客户"]:
        audit = _line(c["合规审计"]) if c["合规审计"] else "无命中"
        lines.append(f"{c['客户编号']}：催收次数{c['催收次数']}；还款意愿{c['还款意愿']['评分']}分（{c['还款意愿']['等级']}）；"
                     f"合规审计：{audit}；分期协商{c['分期协商次数']}次；人工接管{c['人工接管次数']}次；"
                     f"建议策略：{c['建议策略']}；结果分布：{_line(c['结果分布'])}")
    return "\n".join(lines)


def render_audit_text(report: dict) -> str:
    lines = [f"{report['统计月份']} 合规审计报告", f"范围：{report['范围']}"]
    for c in report["客户"]:
        lines.append(f"{c['客户编号']}：{_line(c['命中']) if c['命中'] else '无命中'}；人工接管{c['人工接管次数']}次")
    return "\n".join(lines)


def render_summary_paragraph(report: dict) -> str:
    cur, repay = report["本期指标"], report["实际还款"]
    audit = report["合规审计"]["系统审计命中"]
    audit_text = _line(audit) if audit else "无命中"
    text = (f"{report['统计月份']}催收汇总：催收次数{cur['催收次数']}次，覆盖{cur['覆盖客户数']}位客户，"
            f"正向结果率{cur['正向结果率']}；意愿分布{_line(report['意愿分布'])}；"
            f"合规命中{audit_text}；人工接管{report['人工接管次数']}次。"
            f"实际还款{repay['笔数']}笔，金额{repay['金额']}。")
    if report["说明"]:
        text += "".join(f"{n}。" for n in report["说明"])
    return text


def render_summary_markdown(report: dict) -> str:
    lines = [f"# {report['报告类型']}", "", render_summary_paragraph(report)]
    for sec in ("本期指标", "全量指标", "实际还款", "意愿分布", "合规审计"):
        lines += ["", f"## {sec}", *_md(report[sec])]
    lines += ["", "## 人工接管", f"- {report['人工接管次数']}次"]
    return "\n".join(lines)


def report_reply(summary: str, artifact_id: str) -> str:
    return f"{summary}\n\n[查看完整报告](/reports/{artifact_id})"


def render_summary_text(report: dict) -> str:
    lines = [f"{report['统计月份']} 催收汇总报告"]
    lines += [f"说明：{n}" for n in report["说明"]]
    for sec in ("本期指标", "全量指标", "实际还款", "意愿分布", "合规审计"):
        lines.append(f"{sec}：{_line(report[sec])}")
    lines.append(f"人工接管次数：{report['人工接管次数']}")
    return "\n".join(lines)
