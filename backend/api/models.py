from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from cagent.desensitize import display_mask
from cagent.loans import LoanPriority
from cagent.scoring import Willingness


def masked(value: Any) -> Any:
    if isinstance(value, str):
        return display_mask(value)
    if isinstance(value, dict):
        return {k: masked(v) for k, v in value.items()}
    if isinstance(value, list):
        return [masked(v) for v in value]
    return value


class CustomerBrief(BaseModel):
    customer_id: str
    customer_name: str | None
    age: int | None
    occupation: str | None
    region: str | None
    customer_level: str | None
    credit_score: int | None
    monthly_income: float | None
    overdue_count: int
    overdue_amount: float


class CustomerDetail(BaseModel):
    customer_id: str
    profile: dict[str, Any]
    behaviors: list[dict[str, Any]]
    emotions: list[dict[str, Any]]
    repayments: list[dict[str, Any]]
    memory: dict[str, Any]
    overdues: LoanPriority
    willingness: Willingness | None
    open_session_id: str | None


class SessionRow(BaseModel):
    session_id: str
    case_id: str
    customer_id: str
    mode: str
    pack_id: str
    config_version: str | None
    channel: str | None
    started_at: str
    ended_at: str | None
    end_reason: str | None


class SessionListItem(SessionRow):
    resumable: bool


class Message(BaseModel):
    turn: int
    role: str
    text: str
    meta: dict[str, Any]
    created_at: str


class MemoryItem(BaseModel):
    value: Any
    source: str | None
    updated_at: str | None


class PostTask(BaseModel):
    session_id: str
    name: str
    stage: int
    status: str
    attempts: int
    error: str | None
    updated_at: str | None


class Artifact(BaseModel):
    artifact_id: str
    case_id: str | None
    session_id: str | None
    kind: str
    content: Any
    created_at: str
    archived: int


class Todo(BaseModel):
    todo_id: str
    case_id: str | None
    session_id: str | None
    kind: str
    content: str
    due: str | None
    status: str
    created_at: str


class SessionDetail(BaseModel):
    session: SessionRow
    messages: list[Message]
    memory: dict[str, MemoryItem]
    handoff: Artifact | None
    postcall: list[PostTask]


class CaseRow(BaseModel):
    case_id: str
    customer_id: str
    pack_id: str
    status: str
    created_at: str
    updated_at: str


class CaseDetail(BaseModel):
    case: CaseRow
    sessions: list[SessionRow]
    artifacts: list[Artifact]
    todos: list[Todo]


class AuditRow(BaseModel):
    log_id: str
    ts: str
    customer_id: str | None
    checkpoint: str
    scene: str | None
    content: str | None
    rule_id: str | None
    rule_type: str | None
    level: str | None
    action: str | None
    handoff: int
    config_version: str | None
    session_id: str | None


class MemoryValue(BaseModel):
    value: Any
    operator: str = "运营"


class RetryIn(BaseModel):
    name: str | None = None


class Ok(BaseModel):
    ok: bool = True


class TokenUsage(BaseModel):
    calls: int
    unmetered: int
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int
    cost_cny: float


class CostSession(TokenUsage):
    session_id: str | None
    customer_id: str | None
    started_at: str | None


class PriceCard(BaseModel):
    cache_hit: float
    cache_miss: float
    output: float


class CostReport(BaseModel):
    model: str
    currency: str
    unit: str
    off_peak: PriceCard
    peak: PriceCard
    peak_note: str
    summary: TokenUsage
    sessions: list[CostSession]
