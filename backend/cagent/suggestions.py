"""根据最新 AI 回复，生成当前角色接下来可能说的三句话。"""
from __future__ import annotations

from cagent.llm import GuardedLLM, LLMError

_ROLES = {
    "operator": "运营人员，接下来会继续查询、核对或下指令",
    "customer": "逾期客户，接下来会回应催收",
    "agent": "催收坐席，接下来会对客户说话，必须合规，不能威胁、辱骂或承诺一定减免",
    "human_agent": "人工坐席，接下来会和客户商量还款",
}

_SYSTEM = (
    "根据刚刚的 AI 回复，写出这个角色最可能接着说的 3 句话。\n"
    "每句不超过 28 个字，能接上回复里的事实，不要复述原句，不要编造回复里没有的金额、日期或承诺。\n"
    '只输出 json：{"items": ["...", "...", "..."]}'
)


def _clean(items) -> list[str]:
    out: list[str] = []
    if not isinstance(items, list):
        return out
    for item in items:
        text = " ".join(str(item).split())
        if not text or text in out:
            continue
        out.append(text[:40])
        if len(out) == 3:
            break
    return out


def _fallback(reply: str, role: str, customer_id: str | None) -> list[str]:
    cid = customer_id or "C0001"
    text = reply or ""
    if role == "customer":
        pool = []
        if "分期" in text or "月供" in text:
            pool.append("月供能不能再低一点？")
        if "核实" in text or "身份证" in text:
            pool.append("我身份证后四位是1234")
        if "转人工" in text or "人工" in text:
            pool.append("那你让人工跟我说")
        pool += ["我最近手头紧，能不能晚几天还？", "能不能分12期？", "你们再打电话我就投诉"]
    elif role == "agent":
        pool = [
            "您好，我是贷后专员，沟通账单前先核实一下您的身份。",
            "账单情况我说明一下，也想听您的还款打算。",
            "如果暂时有困难，我们可以看一下标准分期方案。",
        ]
    elif role == "human_agent":
        pool = ["您刚才说的情况我记下了。", "我们登记一个下一次还款的时间可以吗？", "还有哪部分金额您现在能先还？"]
    else:
        pool = []
        if any(w in text for w in ("欠", "逾期", "金额")):
            pool.append(f"把{cid}每笔逾期再列清楚")
        if any(w in text for w in ("意愿", "画像", "信用")):
            pool.append(f"看下{cid}的还款意愿")
        if any(w in text for w in ("可以", "不能", "时段", "外呼")):
            pool.append(f"现在能给{cid}打电话吗？")
        if "报告" in text:
            pool.append(f"生成{cid}的本月催收评估报告")
        pool += [f"查询{cid}的欠款", f"看下{cid}的客户画像", f"对{cid}发起催收"]
    return _clean(pool)


def suggest(llm: GuardedLLM, reply: str, role: str, customer_id: str | None = None) -> list[str]:
    voice = _ROLES.get(role, _ROLES["operator"])
    if llm.available and reply.strip():
        try:
            out = llm.json("suggest", _SYSTEM, {"角色": voice, "客户编号": customer_id or "", "AI回复": reply[:1500]})
            items = _clean(out.get("items") if isinstance(out, dict) else None)
            if len(items) == 3:
                return items
        except LLMError:
            pass
    return _fallback(reply, role if role in _ROLES else "operator", customer_id)
