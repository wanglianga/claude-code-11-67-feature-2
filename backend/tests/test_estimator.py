"""资源成本估计验收用例：区间覆盖、过期判定、右删失处理、负载越界、样本/模型回查、双路线。"""
import uuid

from tests.conftest import login


def _plans(client, headers):
    return {p["plan_no"]: p for p in client.get("/api/estimator/plans", headers=headers).json()}


def _estimate(client, headers, plan_id, **overrides):
    payload = {"route": "auto"}
    payload.update(overrides)
    return client.post(f"/api/estimator/plans/{plan_id}/estimate", json=payload, headers=headers)


def _new_plan(client, headers, **overrides):
    products = {p["code"]: p["id"] for p in client.get("/api/catalog/products", headers=headers).json()}
    body = {
        "title": "测试计划 " + uuid.uuid4().hex[:6],
        "product_ids": [products["P-LAB"]],
        "time_from": "2025-01-01", "time_to": "2025-12-31",
        "filters": [{"field": "test_item", "op": "eq", "value": "LDL-C"}],
        "joins": [], "output_granularity": "受试者级",
        "cache_expectation": "热", "load_hint": 1.0,
    }
    body.update(overrides)
    resp = client.post("/api/estimator/plans", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json(), products


# ---------- 1. 双路线模型训练与可回查 ----------

def test_models_trained_and_traceable(client):
    admin = login(client, "admin", "admin123")
    models = client.get("/api/estimator/models", headers=admin).json()
    routes = {m["route"] for m in models}
    assert {"empirical", "operator"} <= routes

    emp = next(m for m in models if m["route"] == "empirical")
    assert emp["training_sample_count"] > 0
    assert emp["excluded_censored_count"] >= 1  # 中止作业被排除
    detail = client.get(f"/api/estimator/models/{emp['id']}", headers=admin).json()
    # 训练样本可逐条回查，且不含删失样本
    assert len(detail["training_samples"]) == emp["training_sample_count"]
    assert all(s["training_eligible"] and not s["censored"] for s in detail["training_samples"])
    assert emp["training_job_ids"]
    # 样本内覆盖率已记录
    assert "cpu_seconds" in emp["metrics"]["coverage"]

    jobs = client.get("/api/estimator/jobs", headers=admin).json()
    censored = [j for j in jobs if j["censored"]]
    assert censored, "应存在中止（右删失）作业样例"
    assert all(not j["training_eligible"] for j in censored)
    assert all(j["end_reason"] in ("权限撤回", "用户取消", "资源不足") for j in censored)


# ---------- 2. 估计区间（而非伪精确点值）与区间合理性 ----------

def test_intervals_not_point_estimates(client):
    admin = login(client, "admin", "admin123")
    plans = _plans(client, admin)
    plan = next(p for p in plans.values() if "小而热" in p["title"])
    resp = _estimate(client, admin, plan["id"], route="both")
    assert resp.status_code == 200, resp.text
    est = resp.json()
    for dim in ("cpu_seconds", "wall_seconds", "scan_rows", "output_rows"):
        iv = est["intervals"][dim]
        assert iv["low"] <= iv["mid"] <= iv["high"], (dim, iv)
        assert iv["high"] > iv["low"], f"{dim} 不应为伪精确零宽区间"
    assert est["uncertainty_factors"]
    assert est["applicable_sample_range"]
    # 两条路线都给出结果，供对比
    assert est["route_recommendation"]["empirical"]["available"] is True
    assert est["route_recommendation"]["operator"]["available"] is True
    assert est["model"]["version_label"]


def test_estimate_traceability(client):
    admin = login(client, "admin", "admin123")
    plans = _plans(client, admin)
    plan = next(p for p in plans.values() if "窄联结" in p["title"])
    est = _estimate(client, admin, plan["id"], route="auto").json()
    detail = client.get(f"/api/estimator/estimates/{est['id']}", headers=admin).json()
    assert detail["applied_samples"], "估计必须能回查到使用的训练样本"
    assert detail["model"] and detail["model"]["version_label"]
    assert detail["plan_snapshot"]["plan_id"] == plan["id"]
    assert detail["snapshot_signature"]
    assert detail["disclaimer"]
    # 使用的算子基准（若走算子路线）
    if est["route"] == "operator":
        assert detail["baselines_used"]


# ---------- 3. 过期判定：产品版本 / 过滤条件变化 ----------

def test_staleness_on_filter_change(client):
    admin = login(client, "admin", "admin123")
    plan, _ = _new_plan(client, admin)
    est = _estimate(client, admin, plan["id"]).json()
    assert est["is_stale"] is False

    resp = client.put(f"/api/estimator/plans/{plan['id']}", headers=admin,
                      json={"filters": [{"field": "test_item", "op": "eq", "value": "CRP"}]})
    assert resp.status_code == 200
    old = client.get(f"/api/estimator/estimates/{est['id']}", headers=admin).json()
    assert old["is_stale"] is True
    assert "过滤条件" in old["stale_reason"]
    # 旧估计数值保留不变（未被悄悄改写），但被明确标记过期
    assert old["intervals"] == est["intervals"]


def test_staleness_on_catalog_version_bump(client):
    admin = login(client, "admin", "admin123")
    plan, products = _new_plan(client, admin)
    est = _estimate(client, admin, plan["id"]).json()
    assert est["is_stale"] is False

    detail = client.get(f"/api/catalog/products/{products['P-LAB']}", headers=admin).json()
    field = next(f for f in detail["fields"] if f["field_name"] == "test_result")
    client.put(f"/api/catalog/products/{products['P-LAB']}/fields/{field['id']}",
               headers=admin, json={"sensitivity_level": 4})

    old = client.get(f"/api/estimator/estimates/{est['id']}", headers=admin).json()
    assert old["is_stale"] is True
    assert "目录版本" in old["stale_reason"]

    # 重登产品版本后，计划采用新版本；旧估计仍然过期，新估计正常
    refreshed = client.post(f"/api/estimator/plans/{plan['id']}/refresh-version", headers=admin).json()
    assert refreshed["products"][0]["catalog_version"] == 2
    still_old = client.get(f"/api/estimator/estimates/{est['id']}", headers=admin).json()
    assert still_old["is_stale"] is True
    new_est = _estimate(client, admin, plan["id"]).json()
    assert new_est["is_stale"] is False


def test_staleness_on_time_and_granularity_change(client):
    admin = login(client, "admin", "admin123")
    plan, _ = _new_plan(client, admin)
    est = _estimate(client, admin, plan["id"]).json()

    resp = client.put(f"/api/estimator/plans/{plan['id']}", headers=admin, json={"time_from": "2024-06-01"})
    assert resp.status_code == 200
    old = client.get(f"/api/estimator/estimates/{est['id']}", headers=admin).json()
    assert old["is_stale"] is True and "时间范围" in old["stale_reason"]

    plan2, _ = _new_plan(client, admin)
    est2 = _estimate(client, admin, plan2["id"]).json()
    client.put(f"/api/estimator/plans/{plan2['id']}", headers=admin, json={"output_granularity": "明细行"})
    old2 = client.get(f"/api/estimator/estimates/{est2['id']}", headers=admin).json()
    assert old2["is_stale"] is True and "输出粒度" in old2["stale_reason"]


# ---------- 4. 并发负载远离校准范围 ----------

def test_load_extrapolation_widens_or_refuses(client):
    admin = login(client, "admin", "admin123")
    plans = _plans(client, admin)
    plan = next(p for p in plans.values() if "高并发" in p["title"])

    normal = _estimate(client, admin, plan["id"], concurrent_jobs=1.0).json()
    stressed = _estimate(client, admin, plan["id"], concurrent_jobs=20.0).json()

    assert normal["status"] == "ok"
    assert stressed["status"] == "refused"
    assert stressed["load_assessment"]["status"] == "refused"
    assert any(f["kind"] == "concurrency_load" for f in stressed["uncertainty_factors"])
    stressed_w = stressed["intervals"]["cpu_seconds"]["high"] - stressed["intervals"]["cpu_seconds"]["low"]
    normal_w = normal["intervals"]["cpu_seconds"]["high"] - normal["intervals"]["cpu_seconds"]["low"]
    assert stressed_w > normal_w, "负载越界后区间应扩大"


# ---------- 5. 冷数据范围画像覆盖不足 → 区间放大 ----------

def test_cold_data_coverage_widening(client):
    admin = login(client, "admin", "admin123")
    plans = _plans(client, admin)
    plan = next(p for p in plans.values() if "冷数据范围" in p["title"])
    est = _estimate(client, admin, plan["id"]).json()
    assert est["status"] in ("widened", "refused")
    kinds = {f["kind"] for f in est["uncertainty_factors"]}
    assert "cold_data_range" in kinds


# ---------- 6. 中止作业的右删失处理（API 登记） ----------

def test_aborted_job_is_censored_and_excluded(client):
    admin = login(client, "admin", "admin123")
    products = {p["code"]: p["id"] for p in client.get("/api/catalog/products", headers=admin).json()}

    # 中止作业未注明中止原因 → 拒绝登记
    bad = client.post("/api/estimator/jobs", headers=admin, json={
        "product_ids": [products["P-LAB"]], "time_from": "2025-01-01", "time_to": "2025-12-31",
        "scan_rows": 1000, "output_rows": 100, "cpu_seconds": 2.0, "wall_seconds": 1.0,
        "status": "中止", "end_reason": "完成",
    })
    assert bad.status_code == 400

    resp = client.post("/api/estimator/jobs", headers=admin, json={
        "product_ids": [products["P-LAB"]], "time_from": "2025-01-01", "time_to": "2025-12-31",
        "filters": [{"field": "test_item", "op": "eq", "value": "LDL-C"}],
        "scan_rows": 900, "output_rows": 90, "cpu_seconds": 1.5, "wall_seconds": 1.0,
        "cache_state": "冷", "env_load": 1.0, "status": "中止", "end_reason": "权限撤回",
    })
    assert resp.status_code == 200, resp.text
    job = resp.json()
    assert job["censored"] is True and job["training_eligible"] is False

    # 重训后该样本仍不进入训练集
    train = client.post("/api/estimator/models/train", headers=admin, json={"route": "both"})
    assert train.status_code == 200, train.text
    for model in train.json():
        assert job["id"] not in model["training_job_ids"]
        assert model["excluded_censored_count"] >= 1


# ---------- 7. 独立回测：覆盖率接近名义 90% ----------

def test_backtest_coverage(client):
    admin = login(client, "admin", "admin123")
    for route in ("empirical", "operator"):
        resp = client.get(f"/api/estimator/backtest?route={route}", headers=admin)
        assert resp.status_code == 200, resp.text
        result = resp.json()
        assert result["excluded_censored"]
        assert result["test_jobs"]
        assert result["evaluated"] > 0
        cov = result["coverage"]
        # 名义 90%：允许小样本抖动，但区间覆盖必须明显优于零宽/恒定区间
        assert sum(cov.values()) / 4 >= 0.7, cov
        assert all(v > 0 for v in cov.values()), cov


# ---------- 8. 显式路线不可用时给出明确原因，而非静默改道 ----------

def test_explicit_unavailable_route(client):
    admin = login(client, "admin", "admin123")
    plans = _plans(client, admin)
    plan = next(p for p in plans.values() if "新算子" in p["title"])
    resp = _estimate(client, admin, plan["id"], route="empirical")
    # 经验模型无该产品组合的分层 → 400 并说明原因（auto 模式会自动改走算子路线）
    assert resp.status_code == 400
    assert "经验" in resp.json()["detail"] or "分层" in resp.json()["detail"]
    auto = _estimate(client, admin, plan["id"], route="auto").json()
    assert auto["route"] == "operator"


# ---------- 9. 估计不触碰额度与调度 ----------

def test_estimate_does_not_touch_quota(client):
    admin = login(client, "admin", "admin123")
    before = client.get("/api/quota/accounts", headers=admin).json()
    plans = _plans(client, admin)
    plan = next(iter(plans.values()))
    _estimate(client, admin, plan["id"])
    after = client.get("/api/quota/accounts", headers=admin).json()
    assert before == after, "成本估计不得预留或扣减任何额度"


# ---------- 10. 错误输入 ----------

def test_plan_validation(client):
    admin = login(client, "admin", "admin123")
    products = {p["code"]: p["id"] for p in client.get("/api/catalog/products", headers=admin).json()}
    bad = client.post("/api/estimator/plans", headers=admin, json={
        "title": "非法时间", "product_ids": [products["P-LAB"]],
        "time_from": "2025-12-31", "time_to": "2025-01-01",
    })
    assert bad.status_code == 400
    bad2 = client.post("/api/estimator/plans", headers=admin, json={
        "title": "无画像范围", "product_ids": [products["P-BIO"]],
        "time_from": "2010-01-01", "time_to": "2010-12-31",
    })
    plan_id = bad2.json()["id"]
    est = _estimate(client, admin, plan_id)
    assert est.status_code == 400 and "画像" in est.json()["detail"]
