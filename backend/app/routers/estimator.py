"""研究查询资源成本估计路由。

只提供提交前的估计与依据回查，**不**预留额度、不调度任务。
"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    AnalysisPlan, CostEstimate, DataProduct, EstimatorModel, JobLedger,
    OperatorBaseline, PartitionProfile, User,
)
from ..schemas import (
    EstimateIn, JobRecordIn, ModelTrainIn, PlanCreateIn, PlanUpdateIn,
    ProfileUpsertIn,
)
from ..security import current_user, require_roles
from ..services import estimator as svc
from ..services.estimator import EstimatorError

router = APIRouter(prefix="/api/estimator", tags=["资源成本估计"])

ESTIMATOR_ROLES = ("ADMIN", "PI", "ANALYST")


# ---------------- 序列化 ----------------

def _plan_dict(p: AnalysisPlan) -> dict:
    return {
        "id": p.id, "plan_no": p.plan_no, "title": p.title,
        "project_id": p.project_id, "purpose_code": p.purpose_code,
        "products": p.products, "time_from": p.time_from.isoformat(),
        "time_to": p.time_to.isoformat(), "filters": p.filters, "joins": p.joins,
        "output_granularity": p.output_granularity,
        "cache_expectation": p.cache_expectation, "load_hint": p.load_hint,
        "expected_export_rows": p.expected_export_rows,
        "plan_signature": p.plan_signature, "status": p.status,
        "created_by": p.created_by, "created_at": p.created_at.isoformat(),
        "updated_at": p.updated_at.isoformat(),
    }


def _job_dict(j: JobLedger) -> dict:
    return {
        "id": j.id, "job_no": j.job_no, "project_id": j.project_id, "plan_id": j.plan_id,
        "products": j.products, "time_from": j.time_from.isoformat(),
        "time_to": j.time_to.isoformat(), "filters": j.filters, "joins": j.joins,
        "join_types": j.join_types, "output_granularity": j.output_granularity,
        "raw_rows": j.raw_rows, "input_rows": j.input_rows,
        "selectivity": j.selectivity, "scan_rows": j.scan_rows,
        "output_rows": j.output_rows, "cpu_seconds": j.cpu_seconds,
        "wall_seconds": j.wall_seconds, "cache_state": j.cache_state,
        "env_load": j.env_load, "status": j.status, "end_reason": j.end_reason,
        "censored": j.censored, "training_eligible": j.training_eligible,
        "started_at": j.started_at.isoformat(), "finished_at": j.finished_at.isoformat(),
        "note": j.note,
    }


def _model_dict(m: EstimatorModel, with_params: bool = False) -> dict:
    d = {
        "id": m.id, "version_label": m.version_label, "route": m.route,
        "status": m.status, "trained_at": m.trained_at.isoformat(),
        "training_sample_count": m.training_sample_count,
        "excluded_censored_count": m.excluded_censored_count,
        "training_job_ids": m.training_job_ids,
        "calibration": m.calibration, "metrics": m.metrics, "note": m.note,
    }
    if with_params:
        d["params"] = m.params
    return d


def _estimate_dict(db: Session, e: CostEstimate, detail: bool = False) -> dict:
    stale = svc.evaluate_staleness(db, e, persist=True)
    d = {
        "id": e.id, "estimate_no": e.estimate_no, "plan_id": e.plan_id,
        "route": e.route, "model_id": e.model_id, "status": e.status,
        "intervals": e.intervals, "interval_center": e.interval_center,
        "uncertainty_factors": e.uncertainty_factors,
        "applicable_sample_range": e.applicable_sample_range,
        "route_recommendation": e.route_recommendation,
        "load_assessment": e.load_assessment, "warnings": e.warnings,
        "is_stale": stale["is_stale"], "stale_reason": stale["stale_reason"],
        "created_by": e.created_by, "created_at": e.created_at.isoformat(),
    }
    if detail:
        model = db.get(EstimatorModel, e.model_id) if e.model_id else None
        d["applied_samples"] = e.applied_samples
        d["baselines_used"] = e.baselines_used
        d["plan_snapshot"] = e.plan_snapshot
        d["snapshot_signature"] = e.snapshot_signature
        d["model"] = _model_dict(model) if model else None
        d["stale_rule"] = stale["rule"]
        d["disclaimer"] = "本估计只用于方案调整与额度申请参考，不代替额度预留与任务调度。"
    return d


# ---------------- 分析计划 ----------------

@router.post("/plans")
def create_plan(
    payload: PlanCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(*ESTIMATOR_ROLES)),
):
    try:
        plan = svc.create_plan(db, payload, user.username)
    except EstimatorError as exc:
        raise HTTPException(400, str(exc))
    db.commit()
    return _plan_dict(plan)


@router.get("/plans")
def list_plans(db: Session = Depends(get_db), _: User = Depends(current_user)):
    plans = db.query(AnalysisPlan).order_by(AnalysisPlan.id.desc()).limit(200).all()
    return [_plan_dict(p) for p in plans]


@router.get("/plans/{plan_id}")
def plan_detail(plan_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    plan = db.get(AnalysisPlan, plan_id)
    if plan is None:
        raise HTTPException(404, "分析计划不存在")
    estimates = (
        db.query(CostEstimate).filter(CostEstimate.plan_id == plan.id)
        .order_by(CostEstimate.id.desc()).limit(20).all()
    )
    payload = {
        "plan": _plan_dict(plan),
        "estimates": [_estimate_dict(db, e) for e in estimates],
    }
    db.commit()  # 持久化读取时重新判定的过期标记（无变化时不产生写）
    return payload


@router.put("/plans/{plan_id}")
def update_plan(
    plan_id: int,
    payload: PlanUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(*ESTIMATOR_ROLES)),
):
    plan = db.get(AnalysisPlan, plan_id)
    if plan is None:
        raise HTTPException(404, "分析计划不存在")
    try:
        plan = svc.update_plan(db, plan, payload)
    except EstimatorError as exc:
        raise HTTPException(400, str(exc))
    db.commit()
    return _plan_dict(plan)


@router.post("/plans/{plan_id}/refresh-version")
def refresh_plan_version(
    plan_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(*ESTIMATOR_ROLES)),
):
    """重登产品版本。旧估计仍保留并标记过期，不沿用原成本。"""
    plan = db.get(AnalysisPlan, plan_id)
    if plan is None:
        raise HTTPException(404, "分析计划不存在")
    svc.refresh_plan_versions(db, plan)
    db.commit()
    return _plan_dict(plan)


# ---------------- 估计 ----------------

@router.post("/plans/{plan_id}/estimate")
def estimate_plan(
    plan_id: int,
    payload: EstimateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(*ESTIMATOR_ROLES)),
):
    plan = db.get(AnalysisPlan, plan_id)
    if plan is None:
        raise HTTPException(404, "分析计划不存在")
    if payload.route not in ("auto", "empirical", "operator", "both"):
        raise HTTPException(400, "route 仅支持 auto/empirical/operator/both")
    try:
        est = svc.run_estimate(
            db, plan, route=payload.route, cache_state=payload.cache_state,
            concurrent_jobs=payload.concurrent_jobs,
            expected_export_rows=payload.expected_export_rows,
            actor=user.username,
        )
    except EstimatorError as exc:
        raise HTTPException(400, str(exc))
    db.commit()
    result = _estimate_dict(db, est, detail=True)
    result["request_context"] = getattr(est, "response_extra", {})
    db.commit()
    return result


@router.get("/estimates")
def list_estimates(
    plan_id: int | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
):
    q = db.query(CostEstimate).order_by(CostEstimate.id.desc())
    if plan_id:
        q = q.filter(CostEstimate.plan_id == plan_id)
    rows = [_estimate_dict(db, e) for e in q.limit(100).all()]
    db.commit()
    return rows


@router.get("/estimates/{estimate_id}")
def estimate_detail(
    estimate_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
):
    est = db.get(CostEstimate, estimate_id)
    if est is None:
        raise HTTPException(404, "估计记录不存在")
    payload = _estimate_dict(db, est, detail=True)
    db.commit()
    return payload


# ---------------- 数据规模画像 ----------------

@router.get("/profiles")
def list_profiles(
    product_id: int | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
):
    q = db.query(PartitionProfile).order_by(PartitionProfile.product_id, PartitionProfile.time_from)
    if product_id:
        q = q.filter(PartitionProfile.product_id == product_id)
    return [svc.profile_dict(p) for p in q.all()]


@router.post("/profiles")
def upsert_profile(
    payload: ProfileUpsertIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("ADMIN")),
):
    try:
        profile = svc.upsert_profile(db, payload)
    except EstimatorError as exc:
        raise HTTPException(400, str(exc))
    db.commit()
    return svc.profile_dict(profile)


# ---------------- 历史作业账本 ----------------

@router.get("/jobs")
def list_jobs(
    product_code: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
):
    q = db.query(JobLedger).order_by(JobLedger.id.desc())
    if status:
        q = q.filter(JobLedger.status == status)
    jobs = q.limit(300).all()
    if product_code:
        jobs = [j for j in jobs if any(p.get("code") == product_code for p in (j.products or []))]
    return [_job_dict(j) for j in jobs]


@router.post("/jobs")
def record_job(
    payload: JobRecordIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("ADMIN", "PI")),
):
    """登记历史作业。中止作业一律记为右删失样本，不参与训练。"""
    if payload.status not in ("成功", "中止"):
        raise HTTPException(400, "status 仅支持 成功/中止")
    if payload.status == "中止" and payload.end_reason == "完成":
        raise HTTPException(400, "中止作业必须注明中止原因（权限撤回/用户取消/资源不足）")
    try:
        time_from = svc._parse_date(payload.time_from, "time_from")
        time_to = svc._parse_date(payload.time_to, "time_to")
        products = svc.resolve_products(db, payload.product_ids)
    except EstimatorError as exc:
        raise HTTPException(400, str(exc))
    if time_from > time_to:
        raise HTTPException(400, "时间范围起点不能晚于终点")
    filters = [f.model_dump() for f in payload.filters]
    joins = [j.model_dump() for j in payload.joins]
    # 无论中止原因是否在枚举内，中止作业的耗用都只是完整成本的下界
    censored = payload.status == "中止"

    view = _job_dataset_view(db, products, time_from, time_to)
    distinct = view["distinct_values"]
    missing_avg = svc._avg_missing(view["missing_by_product"])
    selectivity, _ = svc.estimate_selectivity(filters, distinct, missing_avg)
    rows_by_product = {pid: round(rows * selectivity, 1)
                       for pid, rows in view["raw_by_product"].items()}
    job = JobLedger(
        job_no="JOB-" + datetime.utcnow().strftime("%y%m%d") + "-"
               + uuid.uuid4().hex[:6].upper(),
        project_id=payload.project_id, plan_id=payload.plan_id,
        products=[{"product_id": p["product_id"], "code": p["code"]} for p in products],
        time_from=time_from, time_to=time_to, filters=filters, joins=joins,
        join_types=sorted({j.get("join_type", "inner") for j in joins}),
        output_granularity=payload.output_granularity,
        plan_signature=svc.build_signature(products, time_from, time_to, filters, joins,
                                           payload.output_granularity),
        raw_rows=int(view["raw_total"]), input_rows=int(view["raw_total"]),
        selectivity=selectivity, rows_by_product=rows_by_product,
        missing_by_product=view["missing_by_product"],
        index_hit_by_product=view["index_hit_by_product"],
        scan_rows=payload.scan_rows, output_rows=payload.output_rows,
        cpu_seconds=payload.cpu_seconds, wall_seconds=payload.wall_seconds,
        cache_state=payload.cache_state, env_load=payload.env_load,
        status=payload.status, end_reason=payload.end_reason,
        censored=censored, training_eligible=not censored,
        note=payload.note,
    )
    db.add(job)
    db.commit()
    return _job_dict(job)


def _job_dataset_view(db: Session, products: list[dict], time_from, time_to) -> dict:
    """不复用 AnalysisPlan 实例：该对象不参与 flush，避免被误当待插入计划。"""
    return svc.dataset_view_for_products(db, products, time_from, time_to)


# ---------------- 模型 ----------------

@router.get("/models")
def list_models(db: Session = Depends(get_db), _: User = Depends(current_user)):
    models = db.query(EstimatorModel).order_by(EstimatorModel.id.desc()).limit(50).all()
    return [_model_dict(m) for m in models]


@router.get("/models/{model_id}")
def model_detail(model_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    model = db.get(EstimatorModel, model_id)
    if model is None:
        raise HTTPException(404, "模型版本不存在")
    jobs = db.query(JobLedger).filter(JobLedger.id.in_(model.training_job_ids or [-1])).all()
    return {
        "model": _model_dict(model, with_params=True),
        "training_samples": [_job_dict(j) for j in jobs],
    }


@router.post("/models/train")
def train_models(
    payload: ModelTrainIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("ADMIN")),
):
    if payload.route not in ("empirical", "operator", "both"):
        raise HTTPException(400, "route 仅支持 empirical/operator/both")
    try:
        models = svc.train_models(db, payload.route, payload.note)
    except EstimatorError as exc:
        raise HTTPException(400, str(exc))
    db.commit()
    return [_model_dict(m) for m in models]


@router.get("/baselines")
def list_baselines(db: Session = Depends(get_db), _: User = Depends(current_user)):
    rows = db.query(OperatorBaseline).order_by(OperatorBaseline.product_id, OperatorBaseline.op).all()
    products = {p.id: p.code for p in db.query(DataProduct).all()}
    return [{
        "id": b.id, "product_id": b.product_id,
        "product_code": products.get(b.product_id),
        "op": b.op, "baseline_version": b.baseline_version,
        "params": b.params, "calibration_range": [b.calibration_min_load, b.calibration_max_load],
        "sample_count": b.sample_count, "updated_at": b.updated_at.isoformat(),
    } for b in rows]


@router.get("/backtest")
def backtest(
    route: str = "empirical",
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
):
    if route not in ("empirical", "operator"):
        raise HTTPException(400, "route 仅支持 empirical/operator")
    try:
        return svc.backtest(db, route)
    except EstimatorError as exc:
        raise HTTPException(400, str(exc))


@router.post("/backfill-profiles")
def backfill_profiles(
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("ADMIN")),
):
    """使估计器基于现有产品目录补齐缺失的分区画像（演示数据用，幂等）。"""
    from ..seed_estimator import seed_estimator_profiles
    created = seed_estimator_profiles(db)
    db.commit()
    return {"created": created}
