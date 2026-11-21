"""请求体模型。"""
from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str
    password: str


class FieldUpdateIn(BaseModel):
    sensitivity_level: int | None = Field(default=None, ge=1, le=4)
    tags: list[str] | None = None
    available_from: str | None = None  # YYYY-MM-DD
    available_to: str | None = None
    is_active: bool | None = None
    semantic: str | None = None


class BatchItemIn(BaseModel):
    project_id: int
    product_id: int
    purpose_code: str


class BatchCreateIn(BaseModel):
    title: str = ""
    items: list[BatchItemIn]


class RevokeIn(BaseModel):
    reason: str = ""


class AccessQueryIn(BaseModel):
    project_id: int
    product_id: int
    purpose_code: str
    kind: str = "查询"  # 查询/导出
    requested_fields: list[str] = []
    request_id: str  # 客户端请求号：断线重试幂等
    export_rows: int = Field(default=100, ge=1)


class ReservationIn(BaseModel):
    project_id: int
    kind: str = "查询"
    note: str = ""


class UsageReportIn(BaseModel):
    project_id: int
    kind: str = "机时"  # 查询/导出/机时
    amount: float = 1
    idempotency_key: str
    note: str = ""


# ---------------- 成本估计 ----------------

class PlanProductIn(BaseModel):
    product_id: int


class PlanFilterIn(BaseModel):
    field: str
    op: str = "eq"  # eq/in/range/missing/not_missing
    value: str | float | int | list | None = None


class PlanJoinIn(BaseModel):
    left_product_id: int
    right_product_id: int
    join_type: str = "inner"  # inner/left/full/anti/semi


class PlanCreateIn(BaseModel):
    title: str
    project_id: int | None = None
    purpose_code: str | None = None
    product_ids: list[int]
    time_from: str  # YYYY-MM-DD
    time_to: str
    filters: list[PlanFilterIn] = []
    joins: list[PlanJoinIn] = []
    output_granularity: str = "明细行"  # 明细行/受试者级/月度汇总/窗口排名
    cache_expectation: str = "冷"  # 冷/热/混合
    load_hint: float = Field(default=1.0, ge=0.1)
    expected_export_rows: int | None = Field(default=None, ge=1)


class PlanUpdateIn(BaseModel):
    title: str | None = None
    product_ids: list[int] | None = None
    time_from: str | None = None
    time_to: str | None = None
    filters: list[PlanFilterIn] | None = None
    joins: list[PlanJoinIn] | None = None
    output_granularity: str | None = None
    cache_expectation: str | None = None
    load_hint: float | None = Field(default=None, ge=0.1)
    expected_export_rows: int | None = None


class EstimateIn(BaseModel):
    route: str = "auto"  # auto/empirical/operator
    cache_state: str | None = None
    concurrent_jobs: float | None = Field(default=None, ge=0.1)
    expected_export_rows: int | None = Field(default=None, ge=1)
    reason: str = ""


class JobRecordIn(BaseModel):
    """历史作业账本登记。中止作业必须标注中止原因（记为右删失样本）。"""

    plan_id: int | None = None
    project_id: int | None = None
    product_ids: list[int]
    time_from: str
    time_to: str
    filters: list[PlanFilterIn] = []
    joins: list[PlanJoinIn] = []
    output_granularity: str = "明细行"
    scan_rows: int = Field(ge=0)
    output_rows: int = Field(ge=0)
    cpu_seconds: float = Field(ge=0)
    wall_seconds: float = Field(ge=0)
    cache_state: str = "冷"
    env_load: float = Field(default=1.0, ge=0.1)
    status: str = "成功"  # 成功/中止
    end_reason: str = "完成"  # 完成/权限撤回/用户取消/资源不足
    note: str = ""


class ProfileUpsertIn(BaseModel):
    product_id: int
    partition: str
    time_from: str
    time_to: str
    record_count: int = Field(ge=0)
    missing_rate: float = Field(default=0.0, ge=0, le=1)
    indexed_fields: list[str] = []
    index_hit_rate: float = Field(default=0.6, ge=0, le=1)
    distinct_values: dict[str, int] = {}
    cache_state: str = "冷"
    profile_load: float = Field(default=1.0, ge=0.1)
    note: str = ""


class ModelTrainIn(BaseModel):
    route: str = "both"  # empirical/operator/both
    note: str = ""

