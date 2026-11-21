"""研究查询资源成本估计服务。

职责边界：**只做提交前的成本估计**，不预留额度、不调度任务、不扣减用量；
估计结果供课题负责人调整分析方案或发起额度申请。

关键规则（与题目要求逐条对应）：
- 双路线：分位数经验模型（历史覆盖好时首选）与组合算子成本模型（新结构可解释）。
- 输出区间：每个资源维度给出低/中/高三点，绝不输出伪精确点值。
- 过期判定：计划的产品版本、时间范围、过滤条件、联结或粒度一旦变化，
  旧估计立即标记过期，不得沿用。
- 右删失：中止作业（权限撤回/用户取消/资源不足）的观测只是成本下界，
  标记 censored 且不参与训练。
- 负载越界：并发负载远离模型校准范围时，扩大区间；极端越界拒绝常规估计。
- 可回查：估计结果保存模型版本、训练样本 id、校准范围与画像依据。
"""
import hashlib
import json
import math
import uuid
from datetime import date, datetime

from sqlalchemy.orm import Session

from ..models import (
    AnalysisPlan, CostEstimate, DataProduct, EstimatorModel, JobLedger,
    OperatorBaseline, PartitionProfile,
)
from . import cost_model as cm

OUTPUT_GRANULARITIES = ["明细行", "受试者级", "月度汇总", "窗口排名"]
CENSORING_REASONS = {"权限撤回", "用户取消", "资源不足"}
LOAD_WIDEN_RATIO = 1.35      # 超出校准上限多少倍开始显著放大区间
LOAD_REFUSE_RATIO = 2.5      # 超出多少倍拒绝给出常规估计（仍输出参考区间）


class EstimatorError(Exception):
    """估计请求不合法或无法给出结果。"""


# --------------------------------------------------------------------------
# 签名与快照
# --------------------------------------------------------------------------

def _norm_filters(filters: list[dict]) -> list[dict]:
    out = []
    for f in filters or []:
        value = f.get("value")
        if isinstance(value, list):
            value = sorted(str(v) for v in value)
        elif value is not None:
            value = str(value)
        out.append({"field": str(f.get("field", "")), "op": str(f.get("op", "eq")),
                    "value": value})
    return sorted(out, key=lambda x: (x["field"], x["op"], json.dumps(x["value"], sort_keys=True, ensure_ascii=False)))


def _norm_joins(joins: list[dict]) -> list[dict]:
    out = []
    for j in joins or []:
        out.append({
            "left_product_id": int(j.get("left_product_id")),
            "right_product_id": int(j.get("right_product_id")),
            "join_type": str(j.get("join_type") or "inner").lower(),
        })
    return sorted(out, key=lambda x: (x["left_product_id"], x["right_product_id"], x["join_type"]))


def _norm_products(products: list[dict]) -> list[dict]:
    out = [
        {"product_id": int(p["product_id"]),
         "code": p.get("code", ""),
         "catalog_version": int(p.get("catalog_version") or 1)}
        for p in products or []
    ]
    return sorted(out, key=lambda x: x["product_id"])


def build_signature(products: list[dict], time_from: date, time_to: date,
                    filters: list[dict], joins: list[dict], granularity: str) -> str:
    payload = {
        "v": 1,
        "products": _norm_products(products),
        "time": [time_from.isoformat(), time_to.isoformat()],
        "filters": _norm_filters(filters),
        "joins": _norm_joins(joins),
        "granularity": granularity,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _parse_date(value: str, label: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise EstimatorError(f"{label} 必须是 YYYY-MM-DD 格式")


def resolve_products(db: Session, product_ids: list[int]) -> list[dict]:
    seen: set[int] = set()
    resolved = []
    for pid in product_ids:
        if pid in seen:
            continue
        seen.add(pid)
        product = db.get(DataProduct, pid)
        if product is None:
            raise EstimatorError(f"产品 id={pid} 不存在")
        resolved.append({
            "product_id": product.id, "code": product.code, "name": product.name,
            "catalog_version": product.catalog_version,
        })
    if not resolved:
        raise EstimatorError("计划至少需要一个产品")
    return sorted(resolved, key=lambda x: x["product_id"])


def plan_snapshot(plan: AnalysisPlan) -> dict:
    return {
        "plan_id": plan.id, "plan_no": plan.plan_no, "title": plan.title,
        "products": _norm_products(plan.products or []),
        "time_from": plan.time_from.isoformat(), "time_to": plan.time_to.isoformat(),
        "filters": _norm_filters(plan.filters or []),
        "joins": _norm_joins(plan.joins or []),
        "output_granularity": plan.output_granularity,
        "cache_expectation": plan.cache_expectation,
        "load_hint": plan.load_hint,
        "expected_export_rows": plan.expected_export_rows,
    }


# --------------------------------------------------------------------------
# 计划 CRUD
# --------------------------------------------------------------------------

def create_plan(db: Session, payload, actor: str) -> AnalysisPlan:
    time_from = _parse_date(payload.time_from, "time_from")
    time_to = _parse_date(payload.time_to, "time_to")
    if time_from > time_to:
        raise EstimatorError("时间范围起点不能晚于终点")
    products = resolve_products(db, payload.product_ids)
    filters = [f.model_dump() for f in payload.filters]
    joins = [j.model_dump() for j in payload.joins]
    plan = AnalysisPlan(
        plan_no="PLAN-" + uuid.uuid4().hex[:10].upper(),
        title=payload.title, project_id=payload.project_id,
        purpose_code=payload.purpose_code, products=products,
        time_from=time_from, time_to=time_to, filters=filters, joins=joins,
        output_granularity=payload.output_granularity,
        cache_expectation=payload.cache_expectation, load_hint=payload.load_hint,
        expected_export_rows=payload.expected_export_rows,
        plan_signature=build_signature(products, time_from, time_to, filters, joins,
                                       payload.output_granularity),
        created_by=actor,
    )
    db.add(plan)
    db.flush()
    return plan


def update_plan(db: Session, plan: AnalysisPlan, payload) -> AnalysisPlan:
    if payload.title is not None:
        plan.title = payload.title
    if payload.product_ids is not None:
        plan.products = resolve_products(db, payload.product_ids)
    if payload.time_from is not None:
        plan.time_from = _parse_date(payload.time_from, "time_from")
    if payload.time_to is not None:
        plan.time_to = _parse_date(payload.time_to, "time_to")
    if payload.filters is not None:
        plan.filters = [f.model_dump() for f in payload.filters]
    if payload.joins is not None:
        plan.joins = [j.model_dump() for j in payload.joins]
    if payload.output_granularity is not None:
        plan.output_granularity = payload.output_granularity
    if payload.cache_expectation is not None:
        plan.cache_expectation = payload.cache_expectation
    if payload.load_hint is not None:
        plan.load_hint = payload.load_hint
    if payload.expected_export_rows is not None:
        plan.expected_export_rows = payload.expected_export_rows
    if plan.time_from > plan.time_to:
        raise EstimatorError("时间范围起点不能晚于终点")
    plan.plan_signature = build_signature(
        plan.products, plan.time_from, plan.time_to, plan.filters, plan.joins,
        plan.output_granularity,
    )
    plan.updated_at = datetime.utcnow()
    plan.status = "已登记"
    db.flush()
    return plan


def refresh_plan_versions(db: Session, plan: AnalysisPlan) -> AnalysisPlan:
    """重新登记：采纳当前目录版本（旧估计仍标记过期，不沿用原成本）。"""
    products = resolve_products(db, [p["product_id"] for p in (plan.products or [])])
    plan.products = products
    plan.plan_signature = build_signature(
        products, plan.time_from, plan.time_to, plan.filters, plan.joins,
        plan.output_granularity,
    )
    plan.updated_at = datetime.utcnow()
    plan.status = "已登记"
    db.flush()
    return plan


# --------------------------------------------------------------------------
# 数据规模画像
# --------------------------------------------------------------------------

def profile_dict(p: PartitionProfile) -> dict:
    return {
        "id": p.id, "product_id": p.product_id,
        "partition": p.partition,
        "time_from": p.time_from.isoformat(), "time_to": p.time_to.isoformat(),
        "record_count": p.record_count, "missing_rate": p.missing_rate,
        "index_stats": p.index_stats,
        "cache_state": p.cache_state, "profile_load": p.profile_load,
        "sampled_at": p.sampled_at.isoformat(), "note": p.note,
    }


def upsert_profile(db: Session, payload) -> PartitionProfile:
    if db.get(DataProduct, payload.product_id) is None:
        raise EstimatorError(f"产品 id={payload.product_id} 不存在")
    time_from = _parse_date(payload.time_from, "time_from")
    time_to = _parse_date(payload.time_to, "time_to")
    if time_from > time_to:
        raise EstimatorError("画像分区时间范围非法")
    profile = (
        db.query(PartitionProfile)
        .filter(PartitionProfile.product_id == payload.product_id,
                PartitionProfile.partition == payload.partition)
        .first()
    )
    if profile is None:
        profile = PartitionProfile(product_id=payload.product_id, partition=payload.partition)
        db.add(profile)
    profile.time_from = time_from
    profile.time_to = time_to
    profile.record_count = payload.record_count
    profile.missing_rate = payload.missing_rate
    profile.index_stats = {
        "indexed_fields": payload.indexed_fields,
        "index_hit_rate": payload.index_hit_rate,
        "index_count": len(payload.indexed_fields),
        "distinct_values": payload.distinct_values,
    }
    profile.cache_state = payload.cache_state
    profile.profile_load = payload.profile_load
    profile.sampled_at = datetime.utcnow()
    profile.note = payload.note
    db.flush()
    return profile


def _overlap_days(a0: date, a1: date, b0: date, b1: date) -> int:
    start = max(a0, b0)
    end = min(a1, b1)
    return max((end - start).days + 1, 0)


def dataset_view(db: Session, plan: AnalysisPlan, cache_state: str | None = None) -> dict:
    """按计划时间范围汇总各产品分区的规模画像。"""
    return dataset_view_for_products(db, plan.products or [], plan.time_from, plan.time_to,
                                     cache_state, plan.cache_expectation)


def dataset_view_for_products(db: Session, products: list[dict], time_from: date, time_to: date,
                              cache_state: str | None = None,
                              cache_expectation: str | None = None) -> dict:
    product_ids = [p["product_id"] for p in (products or [])]
    profiles = (
        db.query(PartitionProfile)
        .filter(PartitionProfile.product_id.in_(product_ids or [-1]))
        .order_by(PartitionProfile.product_id, PartitionProfile.time_from)
        .all()
    )
    raw_by_product: dict[int, float] = {pid: 0.0 for pid in product_ids}
    missing_by_product: dict[int, float] = {}
    index_by_product: dict[int, float] = {}
    distinct_map: dict[str, int] = {}
    cache_states: list[str] = []
    coverage_by_product: dict[int, float] = {}
    partitions: list[dict] = []

    for pid in product_ids:
        product_profiles = [p for p in profiles if p.product_id == pid]
        total_days = max((time_to - time_from).days + 1, 1)
        covered_days = 0
        rows = 0.0
        miss_weight = 0.0
        idx_weight = 0.0
        for p in product_profiles:
            part_days = max((p.time_to - p.time_from).days + 1, 1)
            overlap = _overlap_days(p.time_from, p.time_to, time_from, time_to)
            if overlap <= 0:
                continue
            covered_days += overlap
            rows += p.record_count * overlap / part_days
            miss_weight += p.missing_rate * overlap
            idx = float((p.index_stats or {}).get("index_hit_rate", 0.6))
            idx_weight += idx * overlap
            for field, n in ((p.index_stats or {}).get("distinct_values") or {}).items():
                distinct_map[field] = max(distinct_map.get(field, 0), int(n))
            cache_states.append(p.cache_state)
            partitions.append({
                "product_id": pid, "partition": p.partition,
                "time_from": p.time_from.isoformat(), "time_to": p.time_to.isoformat(),
                "record_count": p.record_count, "miss_rate": p.missing_rate,
                "index_hit_rate": idx, "cache_state": p.cache_state,
                "overlap_days": overlap,
            })
        raw_by_product[pid] = rows
        effective_days = covered_days or 0
        missing_by_product[pid] = round(miss_weight / effective_days, 4) if effective_days else 0.15
        index_by_product[pid] = round(idx_weight / effective_days, 4) if effective_days else 0.55
        coverage_by_product[pid] = round(min(covered_days / total_days, 1.0), 3)

    raw_total = sum(raw_by_product.values())
    all_cache = cache_states or ["冷"]
    profile_state = "冷"
    if all(c == "热" for c in all_cache):
        profile_state = "热"
    elif any(c == "热" for c in all_cache):
        profile_state = "混合"

    return {
        "raw_by_product": {str(k): round(v, 1) for k, v in raw_by_product.items()},
        "missing_by_product": {str(k): v for k, v in missing_by_product.items()},
        "index_hit_by_product": {str(k): v for k, v in index_by_product.items()},
        "coverage_by_product": {str(k): v for k, v in coverage_by_product.items()},
        "distinct_values": distinct_map,
        "raw_total": raw_total,
        "profile_cache_state": profile_state,
        "partitions": partitions,
        "cache_state": cache_state or cache_expectation or profile_state,
    }


def estimate_selectivity(filters: list[dict], distinct_map: dict[str, int],
                         missing_rate: float) -> tuple[float, list[dict]]:
    """按筛选条件与分区基数估计选择率；返回总选择率与逐条件细节。"""
    if not filters:
        return 1.0, []
    total = 1.0
    details = []
    for f in _norm_filters(filters):
        op = f["op"]
        field = f["field"]
        distinct = max(int(distinct_map.get(field, 20) or 20), 2)
        if op == "eq":
            s = 1.0 / distinct
        elif op == "in":
            n = len(f["value"]) if isinstance(f["value"], list) else 1
            s = min(n / distinct, 1.0)
        elif op == "range":
            s = 0.30
        elif op == "missing":
            s = max(missing_rate, 0.001)
        elif op == "not_missing":
            s = max(1.0 - missing_rate, 0.001)
        elif op == "contains":
            s = 0.08
        else:
            s = 0.20
        s = min(max(s, 0.0005), 1.0)
        total *= s
        details.append({"field": field, "op": op, "selectivity": round(s, 5),
                        "basis": f"distinct={distinct}" if op in ("eq", "in")
                        else ("分区缺失率" if op in ("missing", "not_missing") else "经验默认")})
    total = min(max(total, 0.0002), 1.0)
    return round(total, 8), details


# --------------------------------------------------------------------------
# 历史作业 → 训练样本
# --------------------------------------------------------------------------

def _job_class(job: JobLedger) -> dict:
    codes = sorted(p.get("code", "") for p in (job.products or []))
    joins = sorted({j.get("join_type", "inner") for j in (job.joins or [])})
    granularity = job.output_granularity or "明细行"
    return {
        "codes": codes,
        "granularity": granularity,
        "join_types": joins,
        "class_exact": cm.class_key(codes, granularity, joins),
        "class_gran": cm.class_key_products(codes, granularity),
        "class_prod": cm.class_key_products_only(codes),
    }


def _job_features(job: JobLedger) -> list[float]:
    volume = float(job.raw_rows or job.input_rows or 0)
    return cm.make_features(volume, float(job.selectivity or 1.0),
                            len(job.joins or []), job.cache_state, float(job.env_load or 1.0))


def training_job_dicts(db: Session) -> tuple[list[dict], list[JobLedger]]:
    jobs = db.query(JobLedger).order_by(JobLedger.id).all()
    eligible: list[dict] = []
    censored: list[JobLedger] = []
    for job in jobs:
        is_censored = job.censored or job.status != "成功" or job.end_reason in CENSORING_REASONS
        if is_censored or not job.training_eligible:
            censored.append(job)
            continue
        cls = _job_class(job)
        eligible.append({
            "job_id": job.id, "job_no": job.job_no,
            "features": _job_features(job),
            "targets": {
                "cpu_seconds": float(job.cpu_seconds),
                "wall_seconds": float(job.wall_seconds),
                "scan_rows": float(job.scan_rows),
                "output_rows": float(job.output_rows),
            },
            "volume_rows": float(job.raw_rows or job.input_rows or 0),
            "selectivity": float(job.selectivity or 1.0),
            "load": float(job.env_load or 1.0),
            "cache_state": job.cache_state,
            "granularity": job.output_granularity,
            "join_types": cls["join_types"],
            "rows_by_product": {str(k): float(v) for k, v in (job.rows_by_product or {}).items()},
            "missing_by_product": job.missing_by_product or {},
            "index_hit_by_product": job.index_hit_by_product or {},
            "filters": job.filters or [],
            "joins": job.joins or [],
            "output_rows_override": None,
            **{k: cls[k] for k in ("class_exact", "class_gran", "class_prod", "codes")},
        })
    return eligible, censored


def baselines_map(db: Session) -> dict:
    out: dict = {}
    for b in db.query(OperatorBaseline).all():
        out[f"{b.product_id}:{b.op}"] = {
            "product_id": b.product_id, "op": b.op,
            "version": b.baseline_version, "params": b.params or {},
            "sample_count": b.sample_count,
            "calibration_range": [b.calibration_min_load, b.calibration_max_load],
        }
    return out


def _calibration_summary(jobs: list[dict]) -> dict:
    if not jobs:
        return {"load_range": [1.0, 4.0], "volume_range": [0, 0], "cache_states": [],
                "granularities": [], "join_types": []}
    return {
        "load_range": [min(j["load"] for j in jobs), max(j["load"] for j in jobs)],
        "volume_range": [min(j["volume_rows"] for j in jobs), max(j["volume_rows"] for j in jobs)],
        "cache_states": sorted({j["cache_state"] for j in jobs}),
        "granularities": sorted({j["granularity"] for j in jobs}),
        "join_types": sorted({t for j in jobs for t in j["join_types"]}),
    }


def _coverage_metrics(route: str, model: EstimatorModel, jobs: list[dict]) -> dict:
    """样本内覆盖率：实际值落入估计区间的比例（用于模型版本留痕，非独立验证）。"""
    hits = {d: 0 for d in cm.DIMENSIONS}
    total = 0
    for job in jobs:
        try:
            if route == "empirical":
                level, stratum = cm.lookup_stratum(model.params, job["codes"],
                                                   job["granularity"], job["join_types"])
                if stratum is None:
                    continue
                pred = cm.predict_empirical(stratum, job["features"], job["volume_rows"], job["load"])
                intervals = pred["intervals"]
            else:
                view = _job_view(job)
                pred = cm.predict_operator(view, baselines_map_from_params(model.params),
                                           model.params.get("calibration") or {})
                intervals = pred["intervals"]
        except Exception:  # 训练指标不应阻断模型创建
            continue
        total += 1
        for dim in cm.DIMENSIONS:
            iv = intervals.get(dim)
            if iv and iv["low"] <= job["targets"][dim] <= iv["high"]:
                hits[dim] += 1
    return {
        "in_sample_n": total,
        "coverage": {d: round(hits[d] / total, 3) if total else 0.0 for d in cm.DIMENSIONS},
        "nominal": 0.90,
        "note": "样本内覆盖率，非独立验证；独立回测见 /api/estimator/backtest",
    }


def _job_view(job: dict) -> dict:
    return {
        "rows_by_product": job["rows_by_product"],
        "joins": job["joins"],
        "granularity": job["granularity"],
        "cache_state": job["cache_state"],
        "load": job["load"],
        "missing_by_product": job["missing_by_product"],
        "index_hit_by_product": job["index_hit_by_product"],
        "has_filters": bool(job["filters"]),
        "expected_export_rows": job.get("output_rows_override"),
        "parallelism": 4.0,
    }


def baselines_map_from_params(params: dict) -> dict:
    return params.get("baselines") or {}


# --------------------------------------------------------------------------
# 模型训练
# --------------------------------------------------------------------------

def _next_version(db: Session, prefix: str, route: str) -> str:
    count = db.query(EstimatorModel).filter(EstimatorModel.route == route).count()
    return f"{prefix}-v{count + 1}"


def train_models(db: Session, route: str = "both", note: str = "") -> list[EstimatorModel]:
    jobs, censored = training_job_dicts(db)
    if len(jobs) < 4:
        raise EstimatorError(f"可训练历史作业不足（{len(jobs)} 条），无法建立可靠模型")
    created: list[EstimatorModel] = []

    if route in ("both", "empirical"):
        params = cm.train_empirical(jobs, min_samples=5)
        calibration = _calibration_summary(jobs)
        calibration["note"] = "经验模型按分层样本拟合，校准范围即各层样本区间"
        model = EstimatorModel(
            version_label=_next_version(db, "EMP", "empirical"), route="empirical",
            training_job_ids=[j["job_id"] for j in jobs],
            training_sample_count=len(jobs),
            excluded_censored_count=len(censored),
            calibration=calibration, params=params,
            note=note or "分位数经验模型：按产品组合×粒度×联结分层",
        )
        model.metrics = _coverage_metrics("empirical", model, jobs)
        db.query(EstimatorModel).filter(
            EstimatorModel.route == "empirical", EstimatorModel.status == "active"
        ).update({"status": "retired"})
        db.add(model)
        db.flush()
        created.append(model)

    if route in ("both", "operator"):
        baselines = baselines_map(db)
        calibration = cm.train_operator(jobs, baselines)
        calibration.update({
            "load_range": calibration["calibration_load"],
            "volume_range": calibration["calibration_volume"],
            "cache_states": sorted({j["cache_state"] for j in jobs}),
            "granularities": sorted({j["granularity"] for j in jobs}),
            "join_types": sorted({t for j in jobs for t in j["join_types"]}),
        })
        params = {
            "calibration": calibration,
            "baselines": {k: {"version": v["version"], "params": v["params"],
                              "sample_count": v["sample_count"]}
                          for k, v in baselines.items()},
            "uncalibrated_operator_policy": "使用默认基准，并放大区间",
        }
        model = EstimatorModel(
            version_label=_next_version(db, "OP", "operator"), route="operator",
            training_job_ids=[j["job_id"] for j in jobs],
            training_sample_count=len(jobs),
            excluded_censored_count=len(censored),
            calibration=calibration, params=params,
            note=note or "组合算子成本模型：scan/join/aggregate/output 累加 + 历史比值校准",
        )
        model.metrics = _coverage_metrics("operator", model, jobs)
        db.query(EstimatorModel).filter(
            EstimatorModel.route == "operator", EstimatorModel.status == "active"
        ).update({"status": "retired"})
        db.add(model)
        db.flush()
        created.append(model)

    return created


def active_model(db: Session, route: str) -> EstimatorModel | None:
    return (
        db.query(EstimatorModel)
        .filter(EstimatorModel.route == route, EstimatorModel.status == "active")
        .order_by(EstimatorModel.id.desc())
        .first()
    )


# --------------------------------------------------------------------------
# 单路线预测
# --------------------------------------------------------------------------

def _empirical_predict(model: EstimatorModel, plan: AnalysisPlan, view: dict,
                       volume: float, selectivity: float, load: float) -> dict:
    codes = sorted(p["code"] for p in (plan.products or []))
    join_types = sorted({j.get("join_type", "inner") for j in (plan.joins or [])})
    level, stratum = cm.lookup_stratum(model.params, codes, plan.output_granularity, join_types)
    if stratum is None:
        return {"available": False, "reason": "历史作业没有覆盖该产品组合/粒度/联结的分层，无法用经验模型外推",
                "intervals": None, "coverage_level": "none"}
    features = cm.make_features(volume, selectivity, len(plan.joins or []),
                                view["cache_state"], load)
    pred = cm.predict_empirical(stratum, features, volume, load)
    # 选择率偏离样本范围时进一步放大
    widen = []
    if pred["widen_factor"] > 1.0:
        widen.append({
            "kind": "sample_extrapolation", "factor": pred["widen_factor"],
            "detail": "计划数据规模或并发负载超出训练样本范围，区间已按外推比例放大",
        })
    return {
        "available": True,
        "intervals": pred["intervals"],
        "coverage_level": level,
        "stratum_key": "+".join([
            "+".join(codes),
            plan.output_granularity,
            "+".join(join_types) if join_types else "none",
        ]),
        "stratum_sample_ids": stratum["sample_ids"],
        "stratum_sample_nos": stratum["sample_nos"],
        "sample_range": {
            "sample_count": stratum["n"],
            "volume_range": stratum["volume_range"],
            "load_range": stratum["load_range"],
            "cache_states": stratum["cache_states"],
            "granularity": stratum["granularity"],
            "join_types": stratum["join_types"],
            "training_level": level,
        },
        "extra_widen": widen,
        "widen_factor": pred["widen_factor"],
        "reason": f"命中分层（{level}）历史样本 {stratum['n']} 条",
    }


def _operator_predict(model: EstimatorModel, plan: AnalysisPlan, view: dict,
                      load: float, expected_rows: int | None) -> dict:
    baselines = (model.params or {}).get("baselines") or {}
    op_view = {
        "rows_by_product": view["raw_by_product"],
        "joins": plan.joins or [],
        "granularity": plan.output_granularity,
        "cache_state": view["cache_state"],
        "load": load,
        "missing_by_product": view["missing_by_product"],
        "index_hit_by_product": view["index_hit_by_product"],
        "has_filters": bool(plan.filters),
        "expected_export_rows": expected_rows,
        "parallelism": 4.0,
    }
    # 筛选条件降低扫描量
    selectivity, _ = estimate_selectivity(plan.filters or [], view["distinct_values"],
                                          _avg_missing(view["missing_by_product"]))
    op_view["rows_by_product"] = {
        pid: rows * selectivity for pid, rows in view["raw_by_product"].items()
    }
    pred = cm.predict_operator(op_view, baselines, (model.params or {}).get("calibration") or {})
    used_baselines = []
    for item in pred["operators"]["scan"]["products"]:
        entry = baselines.get(f"{item['product_id']}:scan")
        used_baselines.append({
            "product_id": item["product_id"], "op": "scan",
            "baseline_version": entry["version"] if entry else None,
            "calibrated": entry is not None,
        })
    for item in pred["operators"]["join"]["joins"]:
        entry = baselines.get(f"{item['right_product_id']}:join")
        used_baselines.append({
            "product_id": item["right_product_id"], "op": "join",
            "baseline_version": entry["version"] if entry else None,
            "calibrated": entry is not None,
        })
    # 聚合与输出算子使用驱动表的既有基准；缺失即为未校准（新算子场景）
    agg_pid = pred["operators"].get("output_product_id") \
        or (int(next(iter(op_view["rows_by_product"]))) if op_view["rows_by_product"] else None)
    if agg_pid is not None:
        for op in ("aggregate", "output"):
            entry = baselines.get(f"{agg_pid}:{op}")
            used_baselines.append({
                "product_id": int(agg_pid), "op": op,
                "baseline_version": entry["version"] if entry else None,
                "calibrated": entry is not None,
            })
    return {
        "available": True,
        "intervals": pred["intervals"],
        "operators": pred["operators"],
        "coverage_level": "operator",
        "load_factor": pred["load_factor"],
        "has_unknown_join": pred["has_unknown_join"],
        "has_wide_join": pred["has_wide_join"],
        "scan_uncertainty": pred["scan_uncertainty"],
        "output_uncertainty": pred["output_uncertainty"],
        "baselines_used": used_baselines,
        "reason": "按 scan/join/aggregate/output 算子组合计算，并用历史实际/预测比值校准",
    }


def _avg_missing(missing_by_product: dict) -> float:
    vals = [float(v) for v in missing_by_product.values()]
    return sum(vals) / len(vals) if vals else 0.1


# --------------------------------------------------------------------------
# 估计编排
# --------------------------------------------------------------------------

RESOURCE_LABELS = {
    "cpu_seconds": "CPU 时间（秒）",
    "wall_seconds": "实际耗时（秒）",
    "scan_rows": "扫描行数",
    "output_rows": "导出行数",
}


def _load_assessment(load: float, calibration_max: float, calibration_min: float) -> dict:
    ratio = load / calibration_max if calibration_max > 0 else 1.0
    if ratio > LOAD_REFUSE_RATIO:
        status = "refused"
    elif ratio > LOAD_WIDEN_RATIO:
        status = "widened"
    elif load < calibration_min * 0.4 and calibration_min > 0:
        status = "widened"
    else:
        status = "ok"
    return {
        "concurrent_jobs": round(load, 2),
        "calibration_range": [round(calibration_min, 2), round(calibration_max, 2)],
        "ratio_to_max": round(ratio, 3),
        "status": status,
        "policy": ("拒绝常规估计：并发负载远离校准范围，仅返回参考区间"
                   if status == "refused" else
                   "区间已扩大：并发负载超出校准范围" if status == "widened" else
                   "并发负载处于校准范围内"),
    }


def _scale_intervals(intervals: dict, factor: float) -> dict:
    if factor <= 1.0 or not intervals:
        return intervals
    out = {}
    for dim, iv in intervals.items():
        if not iv:
            out[dim] = None
            continue
        out[dim] = {
            "low": round(max(iv["low"] / factor, 0.0), 4),
            "mid": iv["mid"],
            "high": round(iv["high"] * factor, 4),
        }
    return out


def _compare_routes(emp: dict | None, op: dict | None, plan: AnalysisPlan) -> dict:
    novel_structure = bool(op and (op.get("has_unknown_join") or op.get("has_wide_join"))) or \
        plan.output_granularity not in OUTPUT_GRANULARITIES[:2]
    emp_cover = {"exact": 3, "granularity": 2, "products": 1}.get(
        (emp or {}).get("coverage_level"), 0)
    op_available = bool(op and op.get("available"))
    emp_available = bool(emp and emp.get("available"))
    history_rich = emp_cover >= 2
    # 经验路线外推过猛（规模/负载远离样本）时，宁可改用可解释的算子路线
    emp_extrapolated = bool(emp_available and float(emp.get("widen_factor") or 1.0) >= 1.5)
    recommendation = "empirical" if (
        emp_available and history_rich and not novel_structure and not emp_extrapolated
    ) else ("operator" if op_available else ("empirical" if emp_available else None))
    if emp_extrapolated and op_available:
        reasons_extra = "经验路线需大幅外推（规模/负载超出样本），改用组合算子路线"
    else:
        reasons_extra = None
    reasons = []
    if emp_available:
        reasons.append(f"经验模型覆盖层级={emp_cover}/3（3=完全同构计划）")
    else:
        reasons.append("经验模型无可比分层")
    if op_available:
        reasons.append("组合算子模型可解释计划结构，但依赖算子基准维护")
    if novel_structure:
        reasons.append("计划含新联结/宽扇出/少见粒度，经验样本稀少，优先可解释的算子路线")
    elif history_rich:
        reasons.append("历史计划覆盖充分且结构常规，经验路线能吸收真实系统开销")
    if reasons_extra:
        reasons.append(reasons_extra)
    return {
        "recommended": recommendation,
        "reason": "；".join(reasons),
        "novel_structure": novel_structure,
        "empirical_coverage_level": emp_cover,
        "empirical_available": emp_available,
        "operator_available": op_available,
    }


def _estimate_no(db: Session) -> str:
    return "EST-" + uuid.uuid4().hex[:10].upper()


def run_estimate(db: Session, plan: AnalysisPlan, route: str = "auto",
                 cache_state: str | None = None, concurrent_jobs: float | None = None,
                 expected_export_rows: int | None = None, actor: str = "system") -> CostEstimate:
    if not plan.products:
        raise EstimatorError("计划未登记产品")

    view = dataset_view(db, plan, cache_state)
    load = float(concurrent_jobs if concurrent_jobs is not None else (plan.load_hint or 1.0))
    if expected_export_rows is None:
        expected_export_rows = plan.expected_export_rows
    volume = float(view["raw_total"])
    if volume <= 0:
        raise EstimatorError("计划时间范围内没有任何分区画像（数据规模为 0），无法估计")

    selectivity, filter_details = estimate_selectivity(
        plan.filters or [], view["distinct_values"], _avg_missing(view["missing_by_product"]))

    emp_model = active_model(db, "empirical")
    op_model = active_model(db, "operator")
    if route == "empirical" and emp_model is None:
        raise EstimatorError("尚无经验模型版本，请先训练或改用 operator 路线")
    if route == "operator" and op_model is None:
        raise EstimatorError("尚无算子模型版本，请先训练或改用 empirical 路线")

    # 两条路线都计算：主路线按请求/推荐选择，另一条保留用于对比与回退
    emp = _empirical_predict(emp_model, plan, view, volume, selectivity, load) \
        if emp_model is not None else None
    op = _operator_predict(op_model, plan, view, load, expected_export_rows) \
        if op_model is not None else None

    if route == "auto":
        comparison = _compare_routes(emp, op, plan)
        chosen = comparison["recommended"]
    elif route == "both":
        comparison = _compare_routes(emp, op, plan)
        chosen = comparison["recommended"] or "operator"
    else:
        comparison = _compare_routes(emp, op, plan)
        chosen = route

    primary = emp if chosen == "empirical" else op
    if primary is None or not primary.get("available"):
        other = op if chosen == "empirical" else emp
        if route in ("empirical", "operator"):
            # 显式指定路线时不静默改用另一条；说明原因并提示可改用的路线
            hint = "；可改用 operator 组合算子路线" if (other and other.get("available")) else ""
            why = (emp or op or {}).get("reason") or "所选路线缺少模型"
            raise EstimatorError(f"{why}{hint}")
        if other is None or not other.get("available"):
            raise EstimatorError("两条路线都无法给出估计：历史样本与算子基准均不足")
        primary = other
        chosen = "operator" if chosen == "empirical" else "empirical"

    model = emp_model if chosen == "empirical" else op_model
    calibration = (model.calibration or {}) if model else {}
    load_range = calibration.get("load_range") or [1.0, 4.0]
    load_assessment = _load_assessment(load, float(load_range[1]), float(load_range[0]))

    intervals = primary["intervals"]
    status = "ok"
    warnings: list[str] = []
    factors: list[dict] = []

    # 负载策略：先放大，极端越界则拒绝常规估计
    if load_assessment["status"] in ("widened", "refused"):
        factor = min(2.4, 1.0 + 0.6 * math.log(max(load_assessment["ratio_to_max"], 1.0)))
        intervals = _scale_intervals(intervals, factor)
        factors.append({
            "kind": "concurrency_load", "factor": round(factor, 3),
            "detail": (f"执行环境并发 {load:g}，校准范围 {load_range[0]:g}~{load_range[1]:g}；"
                       f"{load_assessment['policy']}"),
        })
        status = "widened"
        warnings.append(load_assessment["policy"])
        if load_assessment["status"] == "refused":
            status = "refused"
            warnings.append("该结果仅作参考，不能直接用于额度申请；请降低并发或重新校准模型")

    # 数据画像覆盖不足（冷数据范围/画像缺失）
    low_cov = [pid for pid, c in view["coverage_by_product"].items() if float(c) < 0.5]
    if low_cov:
        factor = 1.30 if min(float(view["coverage_by_product"][p]) for p in low_cov) > 0 else 1.55
        intervals = _scale_intervals(intervals, factor)
        status = "widened" if status == "ok" else status
        factors.append({
            "kind": "cold_data_range", "factor": factor,
            "detail": (f"产品 {', '.join(sorted(low_cov))} 的计划时间范围画像覆盖不足"
                       "（冷数据/历史归档），按样本外推并放大区间"),
        })
        warnings.append("计划时间范围存在画像覆盖不足的分区（冷数据范围）")
    # 无筛选条件的全量扫描
    if not (plan.filters or []):
        factors.append({
            "kind": "no_filter", "factor": 1.0,
            "detail": "未登记筛选条件，按全量扫描估计；实际可显著低于估计",
        })
    # 经验路线外推
    if chosen == "empirical":
        for w in primary.get("extra_widen") or []:
            factors.append(w)
        if primary["coverage_level"] in ("products", "granularity"):
            factors.append({
                "kind": "coarse_history_class",
                "factor": 1.0,
                "detail": "仅命中较粗层级的历史分层，计划结构（粒度/联结）与样本不完全同构",
            })
    else:
        if primary.get("has_unknown_join"):
            intervals = _scale_intervals(intervals, 1.20)
            factors.append({
                "kind": "unknown_join_operator", "factor": 1.20,
                "detail": "计划包含未校准的联结类型，算子模型按保守默认基准估算，区间已放宽",
            })
        if primary.get("has_wide_join"):
            factors.append({
                "kind": "wide_join_fanout", "factor": 1.0,
                "detail": "宽扇出联结（右表多行匹配）会放大行数与聚合成本，结果行数不确定性升高",
            })
        uncalibrated = [b for b in primary["baselines_used"] if not b["calibrated"]]
        if uncalibrated:
            factor = 1.0 + min(0.30, 0.10 * len(uncalibrated))
            intervals = _scale_intervals(intervals, factor)
            factors.append({
                "kind": "uncalibrated_baseline", "factor": round(factor, 3),
                "detail": f"存在 {len(uncalibrated)} 个未维护的算子基准，已使用默认基准并放大区间",
            })
            warnings.append("部分算子基准尚未校准（新算子），建议补齐基准后再用于额度申请")
    if primary.get("coverage_level") == "exact":
        n = (primary.get("sample_range") or {}).get("sample_count", 0)
        if n < 12:
            factors.append({
                "kind": "small_sample", "factor": 1.0,
                "detail": f"完全同构历史样本仅 {n} 条，分位数尾部估计不稳",
            })
    factors.append({
        "kind": "cache_state", "factor": 1.0,
        "detail": (f"缓存预期「{view['cache_state']}」，画像分区缓存状态「{view['profile_cache_state']}」"
                   "；缓存命中会显著降低扫描与 CPU"),
    })

    # 右删失样本的提示
    censored_hits = _matching_censored(db, plan)
    if censored_hits:
        warnings.append(
            f"{len(censored_hits)} 条同类中止作业为右删失样本（实际耗用仅为下界），未参与训练"
        )
        factors.append({
            "kind": "censored_samples", "factor": 1.0,
            "detail": "存在同类中止作业（权限撤回/用户取消/资源不足），真实完整成本可能高于观测值",
        })

    # 只保存区间中心，供排序/展示参考；对外始终以区间为主
    interval_center = {
        dim: (intervals.get(dim) or {}).get("mid") if intervals.get(dim) else None
        for dim in cm.DIMENSIONS
    }

    applied_samples = _applied_samples(db, primary, chosen, model)
    sample_range = primary.get("sample_range") if chosen == "empirical" else {
        "sample_count": (model.training_sample_count if model else 0),
        "volume_range": (model.calibration or {}).get("volume_range") if model else None,
        "load_range": (model.calibration or {}).get("load_range") if model else None,
        "cache_states": (model.calibration or {}).get("cache_states") if model else None,
        "granularity": (model.calibration or {}).get("granularities") if model else None,
        "join_types": (model.calibration or {}).get("join_types") if model else None,
        "training_level": "operator",
    }

    comparison_payload = {
        "empirical": _route_summary(emp),
        "operator": _route_summary(op),
        "recommended": comparison["recommended"],
        "selected": chosen,
        "reason": comparison["reason"],
        "novel_structure": comparison["novel_structure"],
        "note": "两条路线均可估计同一计划的资源需求；本估计器按历史覆盖与解释需求选择主路线，另一条路线结果一并保留对比。",
    }

    snapshot = plan_snapshot(plan)
    est = CostEstimate(
        estimate_no=_estimate_no(db), plan_id=plan.id, route=chosen,
        model_id=model.id if model else None,
        status=status,
        intervals=intervals,
        interval_center=interval_center,
        uncertainty_factors=factors,
        applicable_sample_range=sample_range or {},
        applied_samples=applied_samples,
        baselines_used=primary.get("baselines_used") or [],
        route_recommendation=comparison_payload,
        load_assessment=load_assessment,
        warnings=warnings,
        plan_snapshot=snapshot,
        snapshot_signature=plan.plan_signature,
        is_stale=False,
        created_by=actor,
    )
    db.add(est)
    db.flush()
    # 仅用于本次响应（不落库）：筛选选择率推导与数据规模视图，便于解释
    est.response_extra = {
        "plan_signature": plan.plan_signature,
        "filter_details": filter_details,
        "selectivity": selectivity,
        "dataset_view": view,
        "concurrent_jobs": load,
    }
    return est


def _route_summary(route_result: dict | None) -> dict:
    if not route_result:
        return {"available": False, "reason": "路线未启用或缺少模型"}
    return {
        "available": bool(route_result.get("available")),
        "intervals": route_result.get("intervals"),
        "coverage_level": route_result.get("coverage_level"),
        "reason": route_result.get("reason"),
        "baselines_used": route_result.get("baselines_used"),
        "operators": route_result.get("operators"),
    }


def _matching_censored(db: Session, plan: AnalysisPlan) -> list[JobLedger]:
    _, censored = training_job_dicts(db)
    codes = sorted(p["code"] for p in (plan.products or []))
    matched = []
    for job in censored:
        cls = _job_class(job)
        if set(cls["codes"]) == set(codes):
            matched.append(job)
    return matched


def _applied_samples(db: Session, primary: dict, chosen: str, model: EstimatorModel | None) -> list[dict]:
    if chosen == "empirical":
        ids = primary.get("stratum_sample_ids") or []
        jobs = db.query(JobLedger).filter(JobLedger.id.in_(ids or [-1])).order_by(JobLedger.id).all()
        role = f"training:{primary.get('coverage_level')}"
    else:
        ids = (model.training_job_ids if model else []) or []
        jobs = db.query(JobLedger).filter(JobLedger.id.in_(ids or [-1])).order_by(JobLedger.id).all()
        role = "training:operator_calibration"
    return [{
        "job_id": j.id, "job_no": j.job_no, "role": role,
        "cpu_seconds": j.cpu_seconds, "wall_seconds": j.wall_seconds,
        "scan_rows": j.scan_rows, "output_rows": j.output_rows,
        "env_load": j.env_load, "cache_state": j.cache_state,
        "granularity": j.output_granularity, "status": j.status,
        "censored": j.censored, "training_eligible": j.training_eligible,
    } for j in jobs]


# --------------------------------------------------------------------------
# 过期判定
# --------------------------------------------------------------------------

def evaluate_staleness(db: Session, est: CostEstimate, persist: bool = True) -> dict:
    snap = est.plan_snapshot or {}
    reasons: list[str] = []
    plan = db.get(AnalysisPlan, est.plan_id)
    if plan is None:
        reasons.append("分析计划已删除")
    else:
        cur_products = _norm_products(plan.products or [])
        if cur_products != snap.get("products"):
            old_codes = {p["product_id"] for p in snap.get("products") or []}
            new_codes = {p["product_id"] for p in cur_products}
            if old_codes != new_codes:
                reasons.append("拟用产品集合已变更")
            else:
                reasons.append("分析计划登记的产品版本已变更")
        if plan.time_from.isoformat() != snap.get("time_from") or \
                plan.time_to.isoformat() != snap.get("time_to"):
            reasons.append("时间范围已变更")
        if _norm_filters(plan.filters or []) != snap.get("filters"):
            reasons.append("过滤条件已变更")
        if _norm_joins(plan.joins or []) != snap.get("joins"):
            reasons.append("联结方式已变更")
        if plan.output_granularity != snap.get("output_granularity"):
            reasons.append("输出粒度已变更")
        # 目录版本：登记之后产品定义发生变化，旧成本直接失效
        for p in cur_products:
            product = db.get(DataProduct, p["product_id"])
            if product is None:
                reasons.append(f"产品 id={p['product_id']} 已从目录移除")
            elif int(p["catalog_version"]) != int(product.catalog_version):
                reasons.append(
                    f"产品 {product.code} 目录版本已从 v{p['catalog_version']} 变为 v{product.catalog_version}")
        if plan.plan_signature != est.snapshot_signature and not reasons:
            reasons.append("计划内容与估计时快照不一致")
    stale = bool(reasons)
    reason_text = "；".join(dict.fromkeys(reasons)) if stale else None
    if persist and (est.is_stale != stale or est.stale_reason != reason_text):
        est.is_stale = stale
        est.stale_reason = reason_text
    return {
        "is_stale": stale,
        "stale_reason": reason_text,
        "rule": "计划的产品版本/时间范围/过滤条件/联结/粒度任一项改变，旧估计即过期，不得沿用",
    }


# --------------------------------------------------------------------------
# 回测
# --------------------------------------------------------------------------

def backtest(db: Session, route: str = "empirical", holdout_ratio: float = 0.25) -> dict:
    """区间覆盖回测。

    - empirical：分层内留一法（leave-one-out）。每次用其余样本重建同一层模型再预测
      被留出的作业，避免样本内自评的乐观偏差；无法命中任何层的样本单列。
    - operator：按时间顺序切分（早期训练、最新检验），检验结构模型对未见作业的覆盖。
    目标覆盖率为名义 90%。
    """
    jobs, censored = training_job_dicts(db)
    if len(jobs) < 6:
        raise EstimatorError(f"可训练历史作业不足（{len(jobs)} 条），无法回测")

    hits = {d: 0 for d in cm.DIMENSIONS}
    misses: list[dict] = []
    evaluated = 0
    train_jobs: list[str] = []
    test_jobs: list[str] = []

    if route == "empirical":
        min_samples = 5
        for job in jobs:
            rest = [j for j in jobs if j["job_id"] != job["job_id"]]
            params = cm.train_empirical(rest, min_samples=min_samples)
            level, stratum = cm.lookup_stratum(params, job["codes"], job["granularity"],
                                               job["join_types"])
            train_jobs = sorted({n for st in _all_stratum_nos(params) for n in st})
            test_jobs.append(job["job_no"])
            if stratum is None:
                misses.append({"job_no": job["job_no"], "reason": "留出样本无可比训练分层（未外推）"})
                continue
            pred = cm.predict_empirical(stratum, job["features"], job["volume_rows"], job["load"])
            intervals = pred["intervals"]
            evaluated += 1
            covered = True
            for dim in cm.DIMENSIONS:
                iv = intervals.get(dim)
                if iv and iv["low"] <= job["targets"][dim] <= iv["high"]:
                    hits[dim] += 1
                else:
                    covered = False
            if not covered:
                misses.append({
                    "job_no": job["job_no"], "level": level,
                    "actual": {d: round(job["targets"][d], 3) for d in cm.DIMENSIONS},
                    "intervals": intervals,
                })
    else:
        holdout_n = max(2, int(round(len(jobs) * holdout_ratio)))
        train, test = jobs[:-holdout_n], jobs[-holdout_n:]
        params = cm.train_operator(train, baselines_map(db))
        train_jobs = [j["job_no"] for j in train]
        test_jobs = [j["job_no"] for j in test]
        for job in test:
            pred = cm.predict_operator(_job_view(job), baselines_map(db), params)
            intervals = pred["intervals"]
            evaluated += 1
            covered = True
            for dim in cm.DIMENSIONS:
                iv = intervals.get(dim)
                if iv and iv["low"] <= job["targets"][dim] <= iv["high"]:
                    hits[dim] += 1
                else:
                    covered = False
            if not covered:
                misses.append({
                    "job_no": job["job_no"],
                    "actual": {d: round(job["targets"][d], 3) for d in cm.DIMENSIONS},
                    "intervals": intervals,
                })

    return {
        "route": route,
        "method": "leave-one-out（分层留一）" if route == "empirical" else "time-split（时间切分）",
        "train_jobs": train_jobs,
        "test_jobs": test_jobs,
        "excluded_censored": [j.job_no for j in censored],
        "nominal": 0.90,
        "evaluated": evaluated,
        "coverage": {d: (round(hits[d] / evaluated, 3) if evaluated else 0.0) for d in cm.DIMENSIONS},
        "misses": misses,
    }


def _all_stratum_nos(params: dict) -> list[list[str]]:
    out = []
    for groups in (params.get("strata") or {}).values():
        for st in groups.values():
            out.append(st.get("sample_nos") or [])
    return out
