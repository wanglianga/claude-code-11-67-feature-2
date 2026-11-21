"""研究查询资源成本估计的演示种子数据（全部虚构）。

覆盖题目要求验证的场景：不同数据规模、冷热缓存、宽窄联结、新算子计划与
中止任务（右删失）样例。作业账本中的数值由演示用的算子基准合成（带噪声），
不代表真实生产数据。
"""
import random
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from .models import (
    AnalysisPlan, DataProduct, EstimatorModel, JobLedger, OperatorBaseline,
    PartitionProfile, Project,
)
from .services import cost_model as cm
from .services import estimator as svc

TODAY = date.today()

# 每个产品 × 分区的演示画像参数：记录量、缺失率、索引命中率、缓存状态
PROFILE_SPECS = {
    "P-LAB": [
        ("2022", date(2022, 1, 1), date(2022, 12, 31), 860_000, 0.07, 0.58, "冷"),
        ("2023", date(2023, 1, 1), date(2023, 12, 31), 980_000, 0.06, 0.63, "冷"),
        ("2024", date(2024, 1, 1), date(2024, 12, 31), 1_110_000, 0.05, 0.71, "混合"),
        ("2025", date(2025, 1, 1), date(2025, 12, 31), 1_260_000, 0.04, 0.86, "热"),
        ("2026", date(2026, 1, 1), date(2026, 12, 31), 1_180_000, 0.04, 0.88, "热"),
    ],
    "P-IMG": [
        ("2022", date(2022, 1, 1), date(2022, 12, 31), 300_000, 0.12, 0.49, "冷"),
        ("2023", date(2023, 1, 1), date(2023, 12, 31), 340_000, 0.10, 0.54, "冷"),
        ("2024", date(2024, 1, 1), date(2024, 12, 31), 410_000, 0.08, 0.62, "混合"),
        ("2025", date(2025, 1, 1), date(2025, 12, 31), 480_000, 0.06, 0.83, "热"),
        ("2026", date(2026, 1, 1), date(2026, 12, 31), 455_000, 0.06, 0.85, "热"),
    ],
    "P-FU": [
        # 历史归档只覆盖 2019 上半年：2019-2020 计划将出现画像覆盖不足（冷数据范围）
        ("2019H1", date(2019, 1, 1), date(2019, 6, 30), 74_000, 0.26, 0.41, "冷"),
        ("2022", date(2022, 1, 1), date(2022, 12, 31), 150_000, 0.18, 0.52, "冷"),
        ("2023", date(2023, 1, 1), date(2023, 12, 31), 168_000, 0.15, 0.57, "冷"),
        ("2024", date(2024, 1, 1), date(2024, 12, 31), 186_000, 0.13, 0.66, "混合"),
        ("2025", date(2025, 1, 1), date(2025, 12, 31), 210_000, 0.10, 0.82, "热"),
        ("2026", date(2026, 1, 1), date(2026, 12, 31), 198_000, 0.10, 0.84, "热"),
    ],
    "P-BIO": [
        ("2022", date(2022, 1, 1), date(2022, 12, 31), 62_000, 0.22, 0.55, "冷"),
        ("2023", date(2023, 1, 1), date(2023, 12, 31), 71_000, 0.19, 0.58, "冷"),
        ("2024", date(2024, 1, 1), date(2024, 12, 31), 82_000, 0.16, 0.67, "混合"),
        ("2025", date(2025, 1, 1), date(2025, 12, 31), 95_000, 0.13, 0.80, "热"),
        ("2026", date(2026, 1, 1), date(2026, 12, 31), 90_000, 0.12, 0.83, "热"),
    ],
}

DISTINCT_SPECS = {
    "P-LAB": {"test_item": 180, "abnormal_flag": 3, "test_unit": 25, "age_at_test": 90},
    "P-IMG": {"modality": 4, "body_part": 12, "exam_id": 400_000},
    "P-FU": {"survival_status": 5, "recurrence_flag": 2, "followup_no": 200_000},
    "P-BIO": {"sample_type": 6, "storage_location": 48, "quality_score": 60},
}

# 各产品的算子基准（可维护的校准值；P-BIO 的 join 基准故意缺失以演示未校准）
BASELINE_SPECS = {
    "P-LAB": {
        "scan": {"ms_per_row": 0.0105, "missing_penalty": 0.22},
        "join": {"ms_per_row": 0.0042, "match_rate": 0.88, "fanout": 1.0},
        "aggregate": {"ms_per_row": 0.0030,
                      "reduction": {"明细行": 1.0, "受试者级": 0.14, "月度汇总": 0.02, "窗口排名": 0.6}},
        "output": {"ms_per_row": 0.023, "startup_s": 0.9},
    },
    "P-IMG": {
        "scan": {"ms_per_row": 0.0145, "missing_penalty": 0.30},
        "join": {"ms_per_row": 0.0050, "match_rate": 0.84, "fanout": 1.0},
        "aggregate": {"ms_per_row": 0.0040,
                      "reduction": {"明细行": 1.0, "受试者级": 0.16, "月度汇总": 0.02, "窗口排名": 0.6}},
        "output": {"ms_per_row": 0.026, "startup_s": 1.0},
    },
    "P-BIO": {
        # 样本库：一个受试者多个样本 → 宽扇出联结
        "scan": {"ms_per_row": 0.0160, "missing_penalty": 0.40},
        "join": {"ms_per_row": 0.0062, "match_rate": 0.79, "fanout": 2.4},
        # aggregate / output 基准尚未维护（新算子未校准：影响区间与可解释性）。
    },
    # 随访：有 scan/join/aggregate，但「窗口排名」这一输出算子尚无基准
    "P-FU": {
        "scan": {"ms_per_row": 0.0090, "missing_penalty": 0.35},
        "join": {"ms_per_row": 0.0035, "match_rate": 0.86, "fanout": 1.15},  # 窄联结
        "aggregate": {"ms_per_row": 0.0028,
                      "reduction": {"明细行": 1.0, "受试者级": 0.12, "月度汇总": 0.03}},
    },
}


def seed_estimator_profiles(db: Session, products_by_code: dict | None = None) -> int:
    """补齐数据规模画像与算子基准（幂等）。返回新建画像数。"""
    if products_by_code is None:
        products_by_code = {p.code: p for p in db.query(DataProduct).all()}
    existing = {(p.product_id, p.partition) for p in db.query(PartitionProfile).all()}
    created = 0
    for code, specs in PROFILE_SPECS.items():
        product = products_by_code.get(code)
        if product is None:
            continue
        distinct = DISTINCT_SPECS.get(code, {})
        for partition, start, end, count, missing, hit, cache in specs:
            if (product.id, partition) in existing:
                continue
            db.add(PartitionProfile(
                product_id=product.id, partition=partition,
                time_from=start, time_to=end, record_count=count,
                missing_rate=missing,
                index_stats={
                    "indexed_fields": list(distinct.keys())[:2] + ["subject_code"],
                    "index_hit_rate": hit, "index_count": 3,
                    "distinct_values": dict(distinct, subject_code=120_000),
                },
                cache_state=cache, profile_load=1.0,
                note=f"{code} {partition} 年分区画像（演示采样）",
            ))
            created += 1

    existing_baselines = {(b.product_id, b.op) for b in db.query(OperatorBaseline).all()}
    for code, ops in BASELINE_SPECS.items():
        product = products_by_code.get(code)
        if product is None:
            continue
        for op, params in ops.items():
            if (product.id, op) in existing_baselines:
                continue
            merged = dict({
                "hot_factor": 0.55, "mixed_factor": 0.78, "cold_factor": 1.0,
                "index_effect": 0.65, "concurrency_sensitivity": 0.16,
            }, **params)
            db.add(OperatorBaseline(
                product_id=product.id, op=op, baseline_version=1, params=merged,
                calibration_min_load=1.0, calibration_max_load=4.0,
                sample_count=12, updated_at=datetime.utcnow(),
            ))
    db.flush()
    return created


def _seed_plans(db: Session, products: dict, projects: dict) -> list[AnalysisPlan]:
    """创建覆盖不同规模/缓存/联结/新算子的演示分析计划。"""
    prj_a = projects.get("PRJ-A")
    prj_b = projects.get("PRJ-B")
    specs = [
        dict(title="小而热：检验指标受试者级汇总", product_ids=["P-LAB"],
             time_from="2025-01-01", time_to="2025-12-31",
             filters=[{"field": "test_item", "op": "eq", "value": "LDL-C"}],
             joins=[], granularity="受试者级", cache="热", load=1.0,
             project_id=prj_a.id if prj_a else None, purpose="TRAIN"),
        dict(title="大而冷：检验指标全量明细导出", product_ids=["P-LAB"],
             time_from="2022-01-01", time_to="2025-12-31",
             filters=[{"field": "test_date", "op": "range", "value": None}],
             joins=[], granularity="明细行", cache="冷", load=3.0,
             project_id=prj_a.id if prj_a else None, purpose="TRAIN",
             expected_export_rows=5000),
        dict(title="窄联结：检验 + 随访内连接", product_ids=["P-LAB", "P-FU"],
             time_from="2024-01-01", time_to="2025-12-31",
             filters=[{"field": "test_item", "op": "in", "value": ["LDL-C", "CRP", "HbA1c"]}],
             joins=[{"left": "P-LAB", "right": "P-FU", "type": "left"}],
             granularity="受试者级", cache="混合", load=1.5,
             project_id=prj_a.id if prj_a else None, purpose="TRAIN"),
        dict(title="宽联结：影像 + 样本库扇出明细", product_ids=["P-IMG", "P-BIO"],
             time_from="2024-01-01", time_to="2025-12-31",
             filters=[{"field": "modality", "op": "eq", "value": "CT"}],
             joins=[{"left": "P-IMG", "right": "P-BIO", "type": "left"}],
             granularity="明细行", cache="冷", load=2.0,
             project_id=prj_b.id if prj_b else None, purpose="EXTRACT"),
        # 新算子 + 未见联结类型：P-BIO 的 aggregate/output 基准未维护，且 anti 联结无历史
        dict(title="新算子：随访反连接样本库（窗口排名）", product_ids=["P-FU", "P-BIO"],
             time_from="2025-01-01", time_to="2025-12-31",
             filters=[{"field": "sample_type", "op": "eq", "value": "血清"}],
             joins=[{"left": "P-FU", "right": "P-BIO", "type": "anti"}],
             granularity="窗口排名", cache="热", load=1.2,
             project_id=prj_b.id if prj_b else None, purpose="EXTRACT"),
        dict(title="冷数据范围：随访 2019-2020 历史归档", product_ids=["P-FU"],
             time_from="2019-01-01", time_to="2020-12-31",
             filters=[], joins=[], granularity="月度汇总", cache="冷", load=1.0,
             project_id=prj_a.id if prj_a else None, purpose="TRAIN"),
        dict(title="高并发：影像全量明细（超出校准负载）", product_ids=["P-IMG"],
             time_from="2022-01-01", time_to="2025-12-31",
             filters=[{"field": "body_part", "op": "eq", "value": "胸部"}],
             joins=[], granularity="明细行", cache="冷", load=14.0,
             project_id=prj_b.id if prj_b else None, purpose="EXTRACT"),
    ]
    plans = []
    for spec in specs:
        pids = [products[c].id for c in spec["product_ids"]]
        prod_payload = []
        for c in spec["product_ids"]:
            p = products[c]
            prod_payload.append({"product_id": p.id, "code": p.code,
                                 "catalog_version": p.catalog_version})
        joins = []
        for j in spec["joins"]:
            joins.append({"left_product_id": products[j["left"]].id,
                          "right_product_id": products[j["right"]].id,
                          "join_type": j["type"]})
        plan = AnalysisPlan(
            plan_no="PLAN-SEED-" + str(len(plans) + 1).zfill(2),
            title=spec["title"], project_id=spec["project_id"],
            purpose_code=spec.get("purpose"), products=prod_payload,
            time_from=date.fromisoformat(spec["time_from"]),
            time_to=date.fromisoformat(spec["time_to"]),
            filters=spec["filters"], joins=joins,
            output_granularity=spec["granularity"],
            cache_expectation=spec["cache"], load_hint=spec["load"],
            expected_export_rows=spec.get("expected_export_rows"),
            plan_signature=svc.build_signature(
                prod_payload, date.fromisoformat(spec["time_from"]),
                date.fromisoformat(spec["time_to"]), spec["filters"], joins,
                spec["granularity"]),
            created_by="seed",
        )
        db.add(plan)
        plans.append(plan)
    db.flush()
    return plans


def _profile_rows(profiles: list[PartitionProfile], product_id: int,
                  start: date, end: date) -> tuple[float, float, float]:
    """时间切片后的记录量、缺失率、索引命中率。"""
    total_days = max((end - start).days + 1, 1)
    rows = miss_w = idx_w = covered = 0.0
    for p in profiles:
        if p.product_id != product_id:
            continue
        overlap = max((min(end, p.time_to) - max(start, p.time_from)).days + 1, 0)
        if overlap <= 0:
            continue
        part_days = max((p.time_to - p.time_from).days + 1, 1)
        rows += p.record_count * overlap / part_days
        miss_w += p.missing_rate * overlap
        idx_w += float((p.index_stats or {}).get("index_hit_rate", 0.6)) * overlap
        covered += overlap
    if covered == 0:
        return 0.0, 0.15, 0.55
    return rows, miss_w / covered, idx_w / covered


def _make_jobs(db: Session, products: dict, baselines: dict, profiles: list[PartitionProfile]):
    """合成历史作业账本：不同规模/冷热缓存/宽窄联结 + 中止（右删失）样例。"""
    rng = random.Random(20261121)
    rows: list[dict] = []

    def add(codes, start, end, filters, joins, granularity, cache, load, status="成功",
            end_reason="完成", note="", noise=0.14):
        product_objs = [products[c] for c in codes]
        raw_by = {}
        miss_by = {}
        idx_by = {}
        for p in product_objs:
            r, m, i = _profile_rows(profiles, p.id, start, end)
            raw_by[str(p.id)] = r
            miss_by[str(p.id)] = round(m, 4)
            idx_by[str(p.id)] = round(i, 4)
        distinct = {}
        for c in codes:
            distinct.update(DISTINCT_SPECS.get(c, {}))
        missing_avg = sum(miss_by.values()) / len(miss_by) if miss_by else 0.1
        selectivity, _ = svc.estimate_selectivity(filters, distinct, missing_avg)
        view = {
            "rows_by_product": {pid: r * selectivity for pid, r in raw_by.items()},
            "joins": joins, "granularity": granularity, "cache_state": cache,
            "load": load, "missing_by_product": miss_by,
            "index_hit_by_product": idx_by, "has_filters": bool(filters),
            "expected_export_rows": None, "parallelism": 4.0,
        }
        pred = cm.operator_cost(view, baselines)
        factor = 1.0 + rng.uniform(-noise, noise)
        cpu = pred["point_costs"]["cpu_seconds"] * factor
        wall = pred["point_costs"]["wall_seconds"] * (1.0 + rng.uniform(-noise, noise))
        # 高并发样本额外叠加排队/抖动（校准范围上端应反映真实队列效应）
        if load >= 3.0:
            queue = 1.0 + 0.04 * (load - 3.0) + rng.uniform(0.0, 0.06)
            cpu *= queue
            wall *= queue
        scan = int(pred["point_costs"]["scan_rows"] * (1.0 + rng.uniform(-0.08, 0.08)))
        out = int(pred["point_costs"]["output_rows"] * (1.0 + rng.uniform(-0.10, 0.10)))
        raw_total = sum(raw_by.values())
        rows.append({
            "codes": codes, "products": product_objs, "raw_by": raw_by,
            "sel_by": {pid: r * selectivity for pid, r in raw_by.items()},
            "start": start, "end": end,
            "miss_by": miss_by, "idx_by": idx_by, "raw_total": raw_total,
            "selectivity": selectivity, "filters": filters, "joins": joins,
            "granularity": granularity, "cache": cache, "load": load,
            "scan": max(scan, 1), "out": max(out, 1),
            "cpu": round(max(cpu, 0.05), 3), "wall": round(max(wall, 0.05), 3),
            "status": status, "end_reason": end_reason, "note": note,
        })

    lab, img, fu, bio = products["P-LAB"], products["P-IMG"], products["P-FU"], products["P-BIO"]
    join_lab_fu = [{"left_product_id": lab.id, "right_product_id": fu.id, "join_type": "left"}]
    join_img_bio = [{"left_product_id": img.id, "right_product_id": bio.id, "join_type": "left"}]
    join_lab_img = [{"left_product_id": lab.id, "right_product_id": img.id, "join_type": "inner"}]

    # 1) P-LAB 单产品，受试者级，不同规模（2022-2026 逐年切片）
    for year in (2022, 2023, 2024, 2025, 2026):
        add(["P-LAB"], date(year, 1, 1), date(year, 12, 31),
            [{"field": "test_item", "op": "in", "value": ["LDL-C", "CRP"]}],
            [], "受试者级", "热" if year >= 2025 else "冷", 1.0 + (year - 2022) * 0.2,
            note=f"检验受试者级 {year}")
    # 2) P-LAB 明细行，小样本 & 大样本
    for year in (2023, 2024, 2025):
        add(["P-LAB"], date(year, 1, 1), date(year, 12, 31),
            [{"field": "test_item", "op": "eq", "value": "LDL-C"}],
            [], "明细行", "混合", 1.0, note=f"检验明细 {year}")
    for year in (2023, 2024, 2025, 2026):
        add(["P-LAB"], date(year, 1, 1), date(year, 12, 31),
            [{"field": "test_item", "op": "in", "value": ["LDL-C", "CRP"]}],
            [], "受试者级", "热" if year >= 2025 else "冷", 1.3,
            note=f"检验受试者级（季度批量）{year}")
    # 3) P-LAB + P-FU 窄联结
    for year in (2022, 2023, 2024, 2025, 2026):
        add(["P-LAB", "P-FU"], date(year, 1, 1), date(year, 12, 31),
            [{"field": "test_item", "op": "in", "value": ["LDL-C", "CRP"]}],
            join_lab_fu, "明细行", "冷" if year < 2024 else "混合",
            1.2 + (year - 2022) * 0.15, note=f"检验+随访 {year}")
    # 窄联结的补充样本（半年度切片，覆盖 2024 下半年与 2025 上半年）
    for y0, m0 in ((2024, 7), (2025, 1), (2025, 7)):
        add(["P-LAB", "P-FU"], date(y0, m0, 1),
            date(y0, m0 + 5, 1) - timedelta(days=1),
            [{"field": "test_item", "op": "in", "value": ["LDL-C", "CRP"]}],
            join_lab_fu, "明细行", "混合", 1.4, note="检验+随访半年度补充样本")
    # 4) P-IMG 单产品
    for year in (2022, 2023, 2024, 2025, 2026):
        add(["P-IMG"], date(year, 1, 1), date(year, 12, 31),
            [{"field": "modality", "op": "eq", "value": "CT"}],
            [], "明细行", "热" if year >= 2025 else "冷", 1.0,
            note=f"影像明细 {year}")
    for year in (2023, 2024, 2025):
        add(["P-IMG"], date(year, 1, 1), date(year, 12, 31),
            [{"field": "body_part", "op": "eq", "value": "胸部"}],
            [], "明细行", "冷", 1.1, note=f"影像胸部明细补充样本 {year}")
    # 5) P-IMG + P-BIO 宽联结（样本库一受试者多样本，扇出）
    for year in (2023, 2024, 2025, 2026):
        add(["P-IMG", "P-BIO"], date(year, 1, 1), date(year, 12, 31),
            [{"field": "modality", "op": "eq", "value": "CT"}],
            join_img_bio, "明细行", "冷" if year < 2025 else "混合", 1.5,
            note=f"影像+样本库 {year}")
    # 6) P-LAB + P-IMG 中等联结
    for year in (2024, 2025):
        add(["P-LAB", "P-IMG"], date(year, 1, 1), date(year, 12, 31),
            [{"field": "modality", "op": "eq", "value": "CT"}],
            join_lab_img, "受试者级", "混合", 1.0, note=f"检验+影像 {year}")
    # 7) P-FU 月度汇总
    for year in (2022, 2023, 2024, 2025):
        add(["P-FU"], date(year, 1, 1), date(year, 12, 31), [],
            [], "月度汇总", "冷" if year < 2024 else "混合", 1.0,
            note=f"随访月度汇总 {year}")
    # 8) 高负载运行样本（校准并发范围至 ~4）
    add(["P-LAB"], date(2025, 1, 1), date(2025, 12, 31),
        [{"field": "test_item", "op": "eq", "value": "CRP"}],
        [], "受试者级", "热", 3.8, note="高并发批处理")
    add(["P-IMG"], date(2025, 1, 1), date(2025, 12, 31),
        [{"field": "body_part", "op": "eq", "value": "胸部"}],
        [], "受试者级", "热", 3.2, note="高并发批处理")
    # 9) 中止作业（右删失）：观测耗用只是完整成本的下界
    add(["P-LAB"], date(2025, 1, 1), date(2025, 12, 31),
        [{"field": "test_item", "op": "eq", "value": "LDL-C"}],
        [], "明细行", "冷", 1.0, status="中止", end_reason="权限撤回",
        note="执行 18% 后资格被撤回，耗用为右删失观测")
    add(["P-LAB", "P-FU"], date(2024, 1, 1), date(2025, 12, 31),
        [{"field": "test_item", "op": "eq", "value": "CRP"}],
        join_lab_fu, "明细行", "冷", 1.0, status="中止", end_reason="用户取消",
        note="用户主动取消，未完成全量扫描")
    add(["P-IMG", "P-BIO"], date(2024, 1, 1), date(2025, 12, 31),
        [{"field": "modality", "op": "eq", "value": "CT"}],
        join_img_bio, "明细行", "冷", 1.0, status="中止", end_reason="资源不足",
        note="内存不足被中止，实际耗用不等于完整成本")
    add(["P-BIO"], date(2025, 1, 1), date(2025, 12, 31),
        [{"field": "sample_type", "op": "eq", "value": "血清"}],
        [], "明细行", "热", 1.0, status="中止", end_reason="权限撤回",
        note="伦理批件撤回导致中止")

    for i, spec in enumerate(rows, 1):
        censored = spec["status"] == "中止"
        job = JobLedger(
            job_no=f"JOB-SEED-{i:03d}",
            project_id=None, plan_id=None,
            products=[{"product_id": p.id, "code": p.code} for p in spec["products"]],
            time_from=spec["start"], time_to=spec["end"],
            filters=spec["filters"], joins=spec["joins"],
            join_types=sorted({j.get("join_type", "inner") for j in spec["joins"]}),
            output_granularity=spec["granularity"],
            plan_signature=svc.build_signature(
                [{"product_id": p.id, "code": p.code, "catalog_version": p.catalog_version}
                 for p in spec["products"]],
                spec["start"], spec["end"], spec["filters"], spec["joins"],
                spec["granularity"]),
            raw_rows=int(spec["raw_total"]), input_rows=int(spec["raw_total"]),
            selectivity=spec["selectivity"],
            rows_by_product={k: round(v, 1) for k, v in spec["sel_by"].items()},
            missing_by_product=spec["miss_by"], index_hit_by_product=spec["idx_by"],
            scan_rows=spec["scan"], output_rows=spec["out"],
            cpu_seconds=spec["cpu"], wall_seconds=spec["wall"],
            cache_state=spec["cache"], env_load=spec["load"],
            status=spec["status"], end_reason=spec["end_reason"],
            censored=censored, training_eligible=not censored,
            note=spec["note"],
        )
        db.add(job)
    db.flush()


def seed_estimator(db: Session) -> None:
    """在基础种子之后注入估计器演示数据，并训练两套模型。"""
    if db.query(EstimatorModel).count() > 0:
        return
    products = {p.code: p for p in db.query(DataProduct).all()}
    projects = {p.code: p for p in db.query(Project).all()}
    if not products:
        return
    seed_estimator_profiles(db, products)
    profiles = db.query(PartitionProfile).all()
    baselines = svc.baselines_map(db)
    _make_jobs(db, products, baselines, profiles)
    plans = _seed_plans(db, products, projects)
    # 先落盘画像/作业/计划，训练失败时不至于丢失演示数据（可手动触发重训）
    db.commit()
    try:
        svc.train_models(db, "both", note="种子演示数据训练（分位数经验模型 + 组合算子模型）")
    except Exception:
        db.rollback()
        return
    # 为前两个计划预生成一次估计，展示区间/因素/样本回查
    for plan in plans[:2]:
        try:
            svc.run_estimate(db, plan, route="auto", actor="seed")
        except Exception:
            pass
    db.commit()
