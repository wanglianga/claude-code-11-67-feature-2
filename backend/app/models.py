"""数据模型：产品目录、课题档案、资格实例、额度账本、访问事件与审计快照。"""
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# 敏感等级：1 公开 / 2 内部 / 3 敏感 / 4 高度敏感（直接标识、遗传信息等）
SENSITIVITY_LABELS = {1: "L1 公开", 2: "L2 内部", 3: "L3 敏感", 4: "L4 高度敏感"}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(sa.String(256))
    display_name: Mapped[str] = mapped_column(sa.String(64))
    # ADMIN 数据治理管理员 / PI 课题负责人 / ANALYST 数据分析师 / AUDITOR 审计员
    role: Mapped[str] = mapped_column(sa.String(16))
    # 分析师/PI 登录后在各课题中对应的成员姓名（成员记录按课题分别建档）
    member_name: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, default=datetime.utcnow)


class DataProduct(Base):
    __tablename__ = "data_products"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(sa.String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(sa.String(128))
    source_system: Mapped[str] = mapped_column(sa.String(64))   # 来源系统：PACS/LIS/随访平台/样本库
    update_frequency: Mapped[str] = mapped_column(sa.String(32))  # 更新频率：每日/每周/每月
    owner_dept: Mapped[str] = mapped_column(sa.String(64), default="")
    description: Mapped[str] = mapped_column(sa.Text, default="")
    status: Mapped[str] = mapped_column(sa.String(16), default="上架")  # 上架/下架
    catalog_version: Mapped[int] = mapped_column(sa.Integer, default=1)  # 目录版本，字段变更即递增
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, default=datetime.utcnow)

    fields: Mapped[list["ProductField"]] = relationship(
        back_populates="product", order_by="ProductField.id", cascade="all, delete-orphan"
    )


class ProductField(Base):
    __tablename__ = "product_fields"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(sa.ForeignKey("data_products.id"), index=True)
    field_name: Mapped[str] = mapped_column(sa.String(64))
    semantic: Mapped[str] = mapped_column(sa.String(256))       # 字段语义（业务含义）
    data_type: Mapped[str] = mapped_column(sa.String(32), default="string")
    sensitivity_level: Mapped[int] = mapped_column(sa.Integer)  # 1-4
    tags: Mapped[list] = mapped_column(JSONB, default=list)     # 直接标识符/准标识符/遗传信息 等
    available_from: Mapped[date] = mapped_column(sa.Date)       # 可用时间范围起
    available_to: Mapped[date] = mapped_column(sa.Date)         # 可用时间范围止
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True)

    product: Mapped[DataProduct] = relationship(back_populates="fields")


class CatalogChangeLog(Base):
    """目录变更日志：字段/产品每次修改留痕，供审计对比"当时定义"。"""

    __tablename__ = "catalog_change_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(sa.ForeignKey("data_products.id"), index=True)
    change_type: Mapped[str] = mapped_column(sa.String(32))  # 新增字段/修改字段/停用字段/更新产品
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    actor: Mapped[str] = mapped_column(sa.String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, default=datetime.utcnow)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(sa.String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(sa.String(128))
    pi_name: Mapped[str] = mapped_column(sa.String(64))
    status: Mapped[str] = mapped_column(sa.String(16), default="在研")  # 在研/结题/暂停
    start_date: Mapped[date] = mapped_column(sa.Date)
    end_date: Mapped[date] = mapped_column(sa.Date)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, default=datetime.utcnow)

    purposes: Mapped[list["ProjectPurpose"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="ProjectPurpose.id"
    )
    members: Mapped[list["ProjectMember"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="ProjectMember.id"
    )
    ethics: Mapped[list["EthicsApproval"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="EthicsApproval.id"
    )


class ProjectPurpose(Base):
    """课题申报的研究目的及其用途条件（字段放行的约束之一）。"""

    __tablename__ = "project_purposes"
    __table_args__ = (sa.UniqueConstraint("project_id", "purpose_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(sa.ForeignKey("projects.id"), index=True)
    purpose_code: Mapped[str] = mapped_column(sa.String(32))
    description: Mapped[str] = mapped_column(sa.String(256))
    max_sensitivity_level: Mapped[int] = mapped_column(sa.Integer, default=2)
    forbidden_tags: Mapped[list] = mapped_column(JSONB, default=list)
    export_allowed: Mapped[bool] = mapped_column(sa.Boolean, default=False)

    project: Mapped[Project] = relationship(back_populates="purposes")


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (sa.UniqueConstraint("project_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(sa.ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(sa.String(64))
    role: Mapped[str] = mapped_column(sa.String(32))  # PI/数据分析师/研究员/统计师
    valid_from: Mapped[date] = mapped_column(sa.Date)
    valid_to: Mapped[date] = mapped_column(sa.Date)
    status: Mapped[str] = mapped_column(sa.String(16), default="在职")  # 在职/离任

    project: Mapped[Project] = relationship(back_populates="members")


class EthicsApproval(Base):
    """伦理批件：适用对象、允许用途、敏感等级上限与有效期。"""

    __tablename__ = "ethics_approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(sa.ForeignKey("projects.id"), index=True)
    approval_no: Mapped[str] = mapped_column(sa.String(64), unique=True)
    scope_subjects: Mapped[str] = mapped_column(sa.String(256))  # 适用对象
    allowed_purposes: Mapped[list] = mapped_column(JSONB, default=list)  # 用途编码列表，["*"] 表示全部
    max_sensitivity_level: Mapped[int] = mapped_column(sa.Integer, default=3)
    forbidden_tags: Mapped[list] = mapped_column(JSONB, default=list)
    valid_from: Mapped[date] = mapped_column(sa.Date)
    valid_to: Mapped[date] = mapped_column(sa.Date)
    status: Mapped[str] = mapped_column(sa.String(16), default="有效")  # 有效/失效/暂停
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, default=datetime.utcnow)

    project: Mapped[Project] = relationship(back_populates="ethics")


class QualificationBatch(Base):
    """资格开通批次：一次开通申请的处理结果，逐项独立状态，绝不整体假报成功。"""

    __tablename__ = "qualification_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_no: Mapped[str] = mapped_column(sa.String(40), unique=True, index=True)
    title: Mapped[str] = mapped_column(sa.String(128), default="")
    status: Mapped[str] = mapped_column(sa.String(16), default="处理中")  # 完成/部分完成/失败
    total: Mapped[int] = mapped_column(sa.Integer, default=0)
    succeeded: Mapped[int] = mapped_column(sa.Integer, default=0)
    failed: Mapped[int] = mapped_column(sa.Integer, default=0)
    created_by: Mapped[str] = mapped_column(sa.String(64))
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)

    items: Mapped[list["QualificationInstance"]] = relationship(back_populates="batch")


class QualificationInstance(Base):
    """资格实例：按（项目 × 产品 × 用途）隔离的字段投影与有效期。

    设计选择：采用独立资格实例模式（每个项目-产品-用途一条记录），
    审计直观、撤回传播范围明确；同一三元组同一时刻至多一条"已签发"。
    """

    __tablename__ = "qualification_instances"
    __table_args__ = (
        sa.Index(
            "uq_active_qualification",
            "project_id", "product_id", "purpose_code",
            unique=True,
            postgresql_where=sa.text("status = '已签发'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int | None] = mapped_column(sa.ForeignKey("qualification_batches.id"), nullable=True)
    project_id: Mapped[int] = mapped_column(sa.ForeignKey("projects.id"), index=True)
    product_id: Mapped[int] = mapped_column(sa.ForeignKey("data_products.id"), index=True)
    purpose_code: Mapped[str] = mapped_column(sa.String(32))
    status: Mapped[str] = mapped_column(sa.String(16), default="已签发")  # 已签发/签发失败/已撤回
    fail_reason: Mapped[str | None] = mapped_column(sa.String(256), nullable=True)
    # 字段投影：编译时求交后的可用人字段及各自有效窗口（快照，签发后不再随目录变化）
    field_projection: Mapped[list] = mapped_column(JSONB, default=list)
    covered_member_ids: Mapped[list] = mapped_column(JSONB, default=list)  # 签发时在职成员范围
    # 资格依据：伦理批件快照、用途条件、成员范围、被剔除字段及原因
    basis: Mapped[dict] = mapped_column(JSONB, default=dict)
    catalog_version: Mapped[int] = mapped_column(sa.Integer, default=1)  # 签发依据的目录版本
    valid_from: Mapped[date] = mapped_column(sa.Date)
    valid_to: Mapped[date] = mapped_column(sa.Date)
    version: Mapped[int] = mapped_column(sa.Integer, default=1)
    issued_at: Mapped[datetime] = mapped_column(sa.DateTime, default=datetime.utcnow)
    issued_by: Mapped[str] = mapped_column(sa.String(64), default="system")
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(sa.String(256), nullable=True)

    batch: Mapped[QualificationBatch | None] = relationship(back_populates="items")
    product: Mapped[DataProduct] = relationship()
    project: Mapped[Project] = relationship()


class QuotaAccount(Base):
    """额度账户：已批准预算 + 实际消耗 + 并发占用上限（按课题建账）。"""

    __tablename__ = "quota_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(sa.ForeignKey("projects.id"), unique=True, index=True)
    max_concurrent: Mapped[int] = mapped_column(sa.Integer, default=3)      # 并发占用上限
    budget_query: Mapped[int] = mapped_column(sa.Integer, default=0)        # 已批准查询次数
    used_query: Mapped[int] = mapped_column(sa.Integer, default=0)
    budget_export_rows: Mapped[int] = mapped_column(sa.Integer, default=0)  # 已批准导出行数
    used_export_rows: Mapped[int] = mapped_column(sa.Integer, default=0)
    budget_compute_hours: Mapped[float] = mapped_column(sa.Float, default=0)  # 已批准机时
    used_compute_hours: Mapped[float] = mapped_column(sa.Float, default=0)

    project: Mapped[Project] = relationship()


class QuotaReservation(Base):
    """并发占用记录：查询/导出执行期间占用一个并发槽。"""

    __tablename__ = "quota_reservations"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(sa.ForeignKey("quota_accounts.id"), index=True)
    kind: Mapped[str] = mapped_column(sa.String(16))  # 查询/导出
    status: Mapped[str] = mapped_column(sa.String(16), default="占用中")  # 占用中/已结算/已释放
    access_event_id: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    note: Mapped[str] = mapped_column(sa.String(256), default="")
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, default=datetime.utcnow)
    released_at: Mapped[datetime | None] = mapped_column(sa.DateTime, nullable=True)


class UsageEvent(Base):
    """用量事件：一次实际消耗。idempotency_key 全局唯一，重复上报只扣一次。"""

    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(sa.ForeignKey("quota_accounts.id"), index=True)
    kind: Mapped[str] = mapped_column(sa.String(16))  # 查询/导出/机时
    amount: Mapped[float] = mapped_column(sa.Float, default=1)
    idempotency_key: Mapped[str] = mapped_column(sa.String(128), unique=True, index=True)
    receipt_no: Mapped[str] = mapped_column(sa.String(40), unique=True, index=True)
    access_event_id: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    balance_after: Mapped[dict] = mapped_column(JSONB, default=dict)  # 扣减后的账户快照
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, default=datetime.utcnow)

    reports: Mapped[list["UsageReport"]] = relationship(back_populates="event", order_by="UsageReport.id")


class UsageReport(Base):
    """用量上报记录：每次上报（含断线重试的重复上报）都留痕，重复回执可查。"""

    __tablename__ = "usage_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(sa.ForeignKey("usage_events.id"), index=True)
    is_duplicate: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    note: Mapped[str] = mapped_column(sa.String(256), default="")
    reported_at: Mapped[datetime] = mapped_column(sa.DateTime, default=datetime.utcnow)

    event: Mapped[UsageEvent] = relationship(back_populates="reports")


class AnalysisPlan(Base):
    """研究分析计划：登记拟用产品（含登记时的目录版本）、时间范围、筛选条件、
    联结方式与输出粒度。估计过期判定以本表登记的产品版本与条件快照为准。"""

    __tablename__ = "analysis_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_no: Mapped[str] = mapped_column(sa.String(40), unique=True, index=True)
    title: Mapped[str] = mapped_column(sa.String(128))
    project_id: Mapped[int | None] = mapped_column(sa.ForeignKey("projects.id"), nullable=True, index=True)
    purpose_code: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    # 拟用产品：[{product_id, code, name, catalog_version}]，catalog_version = 登记时的目录版本
    products: Mapped[list] = mapped_column(JSONB, default=list)
    time_from: Mapped[date] = mapped_column(sa.Date)
    time_to: Mapped[date] = mapped_column(sa.Date)
    # 筛选条件：[{field, op(eq/in/range/missing/not_missing), value}]
    filters: Mapped[list] = mapped_column(JSONB, default=list)
    # 联结：[{left_product_id, right_product_id, join_type}]
    joins: Mapped[list] = mapped_column(JSONB, default=list)
    output_granularity: Mapped[str] = mapped_column(sa.String(32), default="明细行")
    cache_expectation: Mapped[str] = mapped_column(sa.String(16), default="冷")  # 冷/热/混合
    load_hint: Mapped[float] = mapped_column(sa.Float, default=1.0)  # 预期并发作业数
    expected_export_rows: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    plan_signature: Mapped[str] = mapped_column(sa.String(512), index=True)
    status: Mapped[str] = mapped_column(sa.String(16), default="已登记")  # 已登记/已归档/待重登
    created_by: Mapped[str] = mapped_column(sa.String(64))
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime, default=datetime.utcnow)


class PartitionProfile(Base):
    """数据规模画像：按（产品 × 分区）记录记录量、缺失率、索引统计与缓存状态。"""

    __tablename__ = "partition_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(sa.ForeignKey("data_products.id"), index=True)
    partition: Mapped[str] = mapped_column(sa.String(32))  # 2024 / 2025-H1 / 历史归档
    time_from: Mapped[date] = mapped_column(sa.Date)
    time_to: Mapped[date] = mapped_column(sa.Date)
    record_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    missing_rate: Mapped[float] = mapped_column(sa.Float, default=0.0)
    # {"indexed_fields": [...], "index_hit_rate": 0.86, "index_count": n,
    #  "distinct_values": {"field": n}}
    index_stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    cache_state: Mapped[str] = mapped_column(sa.String(16), default="冷")  # 冷/热
    profile_load: Mapped[float] = mapped_column(sa.Float, default=1.0)  # 画像采样时并发
    sampled_at: Mapped[datetime] = mapped_column(sa.DateTime, default=datetime.utcnow)
    note: Mapped[str] = mapped_column(sa.String(256), default="")


class OperatorBaseline(Base):
    """组合成本模型的数据产品算子基准（scan/join/aggregate/output）。

    每个产品 × 算子一行；params 保存每行毫秒成本、索引/冷缓存/缺失惩罚等。
    """

    __tablename__ = "operator_baselines"
    __table_args__ = (sa.UniqueConstraint("product_id", "op"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(sa.ForeignKey("data_products.id"), index=True)
    op: Mapped[str] = mapped_column(sa.String(16))  # scan/join/aggregate/output
    baseline_version: Mapped[int] = mapped_column(sa.Integer, default=1)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    calibration_min_load: Mapped[float] = mapped_column(sa.Float, default=1.0)
    calibration_max_load: Mapped[float] = mapped_column(sa.Float, default=4.0)
    sample_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime, default=datetime.utcnow)


class JobLedger(Base):
    """历史作业账本：同类计划的实际 CPU、扫描/导出行数、环境负载与中止状态。

    中止作业（权限撤回/用户取消/资源不足）是右删失样本：观测到的耗用只是
    完整执行成本的下界，`censored=true`、`training_eligible=false`，不参与训练。
    """

    __tablename__ = "job_ledger"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_no: Mapped[str] = mapped_column(sa.String(40), unique=True, index=True)
    project_id: Mapped[int | None] = mapped_column(sa.ForeignKey("projects.id"), nullable=True)
    plan_id: Mapped[int | None] = mapped_column(sa.ForeignKey("analysis_plans.id"), nullable=True)
    products: Mapped[list] = mapped_column(JSONB, default=list)
    time_from: Mapped[date] = mapped_column(sa.Date)
    time_to: Mapped[date] = mapped_column(sa.Date)
    filters: Mapped[list] = mapped_column(JSONB, default=list)
    joins: Mapped[list] = mapped_column(JSONB, default=list)
    join_types: Mapped[list] = mapped_column(JSONB, default=list)
    output_granularity: Mapped[str] = mapped_column(sa.String(32), default="明细行")
    plan_signature: Mapped[str] = mapped_column(sa.String(512), index=True)
    raw_rows: Mapped[int] = mapped_column(sa.Integer, default=0)       # 时间范围内分区记录量合计
    input_rows: Mapped[int] = mapped_column(sa.Integer, default=0)     # 筛选前参与扫描的行
    selectivity: Mapped[float] = mapped_column(sa.Float, default=1.0)
    rows_by_product: Mapped[dict] = mapped_column(JSONB, default=dict)
    missing_by_product: Mapped[dict] = mapped_column(JSONB, default=dict)
    index_hit_by_product: Mapped[dict] = mapped_column(JSONB, default=dict)
    scan_rows: Mapped[int] = mapped_column(sa.Integer, default=0)
    output_rows: Mapped[int] = mapped_column(sa.Integer, default=0)
    cpu_seconds: Mapped[float] = mapped_column(sa.Float, default=0.0)
    wall_seconds: Mapped[float] = mapped_column(sa.Float, default=0.0)
    cache_state: Mapped[str] = mapped_column(sa.String(16), default="冷")
    env_load: Mapped[float] = mapped_column(sa.Float, default=1.0)  # 执行时并发作业数
    status: Mapped[str] = mapped_column(sa.String(16), default="成功")  # 成功/中止
    end_reason: Mapped[str] = mapped_column(sa.String(32), default="完成")
    censored: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    training_eligible: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    started_at: Mapped[datetime] = mapped_column(sa.DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime] = mapped_column(sa.DateTime, default=datetime.utcnow)
    note: Mapped[str] = mapped_column(sa.String(256), default="")


class EstimatorModel(Base):
    """估计模型版本：分位数经验模型（empirical）或组合算子模型（operator）。

    训练样本 id、校准范围、覆盖率与参数均入库，估计结果可按 model_id 回查。
    """

    __tablename__ = "estimator_models"

    id: Mapped[int] = mapped_column(primary_key=True)
    version_label: Mapped[str] = mapped_column(sa.String(32), unique=True, index=True)
    route: Mapped[str] = mapped_column(sa.String(16))  # empirical/operator
    status: Mapped[str] = mapped_column(sa.String(16), default="active")  # active/retired
    trained_at: Mapped[datetime] = mapped_column(sa.DateTime, default=datetime.utcnow)
    training_job_ids: Mapped[list] = mapped_column(JSONB, default=list)
    training_sample_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    excluded_censored_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    calibration: Mapped[dict] = mapped_column(JSONB, default=dict)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)
    note: Mapped[str] = mapped_column(sa.String(256), default="")


class CostEstimate(Base):
    """作业提交前的资源成本估计：区间而非伪精确值，保存模型版本与训练样本回查。"""

    __tablename__ = "cost_estimates"

    id: Mapped[int] = mapped_column(primary_key=True)
    estimate_no: Mapped[str] = mapped_column(sa.String(40), unique=True, index=True)
    plan_id: Mapped[int] = mapped_column(sa.ForeignKey("analysis_plans.id"), index=True)
    route: Mapped[str] = mapped_column(sa.String(16))  # empirical/operator
    model_id: Mapped[int | None] = mapped_column(sa.ForeignKey("estimator_models.id"), nullable=True)
    status: Mapped[str] = mapped_column(sa.String(24))  # ok/widened/refused/insufficient_samples
    intervals: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    interval_center: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    uncertainty_factors: Mapped[list] = mapped_column(JSONB, default=list)
    applicable_sample_range: Mapped[dict] = mapped_column(JSONB, default=dict)
    applied_samples: Mapped[list] = mapped_column(JSONB, default=list)
    baselines_used: Mapped[list] = mapped_column(JSONB, default=list)
    route_recommendation: Mapped[dict] = mapped_column(JSONB, default=dict)
    load_assessment: Mapped[dict] = mapped_column(JSONB, default=dict)
    warnings: Mapped[list] = mapped_column(JSONB, default=list)
    plan_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    snapshot_signature: Mapped[str] = mapped_column(sa.String(512), default="")
    is_stale: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    stale_reason: Mapped[str | None] = mapped_column(sa.String(256), nullable=True)
    created_by: Mapped[str] = mapped_column(sa.String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, default=datetime.utcnow)


class AccessEvent(Base):
    """数据访问事件：携带当时的产品定义、资格依据与额度变化快照，供审计还原。"""

    __tablename__ = "access_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True)  # 客户端请求号，重试幂等
    project_id: Mapped[int] = mapped_column(sa.ForeignKey("projects.id"), index=True)
    product_id: Mapped[int] = mapped_column(sa.ForeignKey("data_products.id"), index=True)
    purpose_code: Mapped[str] = mapped_column(sa.String(32))
    member_id: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    member_name: Mapped[str] = mapped_column(sa.String(64))
    qualification_id: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    kind: Mapped[str] = mapped_column(sa.String(16), default="查询")  # 查询/导出
    status: Mapped[str] = mapped_column(sa.String(16))  # 成功/拒绝
    deny_reason: Mapped[str | None] = mapped_column(sa.String(256), nullable=True)
    requested_fields: Mapped[list] = mapped_column(JSONB, default=list)
    granted_fields: Mapped[list] = mapped_column(JSONB, default=list)
    product_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)       # 当时的产品定义
    qualification_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)  # 当时的资格依据
    quota_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)          # 当时的额度变化
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, default=datetime.utcnow)
