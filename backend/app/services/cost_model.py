"""研究查询资源成本估计的数值内核（无第三方依赖）。

包含两条路线的纯计算部分：

1. 分层经验模型（empirical）：按「产品组合 × 输出粒度 × 联结类型」分层，
   在每层内对 log1p(资源量) 拟合分位数回归（pinball-损失 + 迭代重加权最小二乘），
   输出 5% / 50% / 95% 分位曲线。能吸收真实执行中的系统开销，但层内样本不足
   或数据规模越界时需要放宽层级并放大区间。
2. 组合算子成本模型（operator）：按 scan / join / aggregate / output 算子累加，
   每个产品有一组可维护的算子基准；用历史作业的实际/预测比值分位数校准
   系统开销、缓存与并发偏差。能解释未见过（但结构可分解）的计划。
"""
import math


# --------------------------------------------------------------------------
# 极小线性代数
# --------------------------------------------------------------------------

def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """高斯消元（部分主元）。奇异返回 None。"""
    n = len(a)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        pv = m[col][col]
        for r in range(col + 1, n):
            factor = m[r][col] / pv
            if factor == 0:
                continue
            for c in range(col, n + 1):
                m[r][c] -= factor * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))
        x[r] = s / m[r][r]
    return x


def _wls(z: list[list[float]], y: list[float], w: list[float], ridge: float = 1e-6) -> list[float] | None:
    p = len(z[0])
    a = [[0.0] * p for _ in range(p)]
    b = [0.0] * p
    for row, target, weight in zip(z, y, w):
        for i in range(p):
            b[i] += weight * row[i] * target
            for j in range(p):
                a[i][j] += weight * row[i] * row[j]
    # 截距不惩罚，其余列加极小岭参数，避免共线/样本少时病态
    for i in range(1, p):
        a[i][i] += ridge
    return _solve(a, b)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def quantile(values: list[float], q: float) -> float:
    """线性插值分位数（q∈[0,1]）。"""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


# --------------------------------------------------------------------------
# 特征与分层
# --------------------------------------------------------------------------

# 特征：[截距, log1p(数据行量), 选择率, 联结数, 冷缓存标记, log1p(并发)]
FEATURE_NAMES = ["log_volume", "selectivity", "joins", "cold", "log_load"]
COLD_MAP = {"冷": 1.0, "热": 0.0, "混合": 0.5}

# 每个资源维度允许的最小相对半宽（避免出现伪精确的零宽区间）
MIN_REL_WIDTH = {
    "cpu_seconds": 0.08,
    "wall_seconds": 0.08,
    "scan_rows": 0.04,
    "output_rows": 0.06,
}


def cold_flag(cache_state: str) -> float:
    return COLD_MAP.get(cache_state, 0.6)


def class_key(codes: list[str], granularity: str, join_types: list[str]) -> str:
    return "|".join([
        "+".join(sorted(codes)),
        granularity,
        "+".join(sorted(join_types)) if join_types else "none",
    ])


def class_key_products(codes: list[str], granularity: str) -> str:
    return "+".join(sorted(codes)) + "|" + granularity


def class_key_products_only(codes: list[str]) -> str:
    return "+".join(sorted(codes))


def make_features(volume_rows: float, selectivity: float, joins: int,
                  cache_state: str, load: float) -> list[float]:
    return [
        1.0,
        math.log1p(max(volume_rows, 0.0)),
        max(min(selectivity, 1.0), 1e-4),
        float(joins),
        cold_flag(cache_state),
        math.log1p(max(load, 0.1)),
    ]


def _feature_subset(n: int) -> list[int]:
    """样本少时降维，避免分位数回归过拟合。返回使用的特征列下标。"""
    if n >= 12:
        return [0, 1, 2, 3, 4, 5]
    if n >= 7:
        return [0, 1, 4]
    return [0, 1]


def _standardize(x_rows: list[list[float]], idx: list[int]):
    means, stds = [], []
    for j in idx:
        vals = [r[j] for r in x_rows]
        mu = sum(vals) / len(vals)
        sd = math.sqrt(sum((v - mu) ** 2 for v in vals) / max(len(vals) - 1, 1)) or 1.0
        means.append(mu)
        stds.append(sd)
    z = [[1.0] + [(r[j] - mu) / sd for j, mu, sd in zip(idx, means, stds)] for r in x_rows]
    return z, means, stds


def fit_quantile_regression(x_rows: list[list[float]], y: list[float], q: float,
                            idx: list[int], iters: int = 60) -> dict | None:
    """pinball 损失分位数回归（IRLS 近似），返回可序列化的系数与标准化参数。"""
    if len(y) < 3 or len(set(round(v, 9) for v in y)) < 2:
        # 常量目标：用中位数与固定半宽表达
        base = _median(y)
        return {"constant": base, "idx": [], "means": [], "stds": [], "beta": []}
    z, means, stds = _standardize(x_rows, idx)
    beta = _wls(z, y, [1.0] * len(y))
    if beta is None:
        return None
    eps = 1e-3
    for _ in range(iters):
        weights = []
        for row, target in zip(z, y):
            r = target - sum(c * v for c, v in zip(beta, row))
            w = (q if r >= 0 else 1.0 - q) / max(abs(r), eps)
            weights.append(max(min(w, 1e3), 1e-2))
        new_beta = _wls(z, y, weights)
        if new_beta is None:
            break
        delta = sum(abs(a - b) for a, b in zip(beta, new_beta)) / (sum(abs(b) for b in beta) + 1e-9)
        beta = new_beta
        if delta < 1e-5:
            break
    return {"beta": beta, "idx": idx, "means": means, "stds": stds}


def predict_quantile(fit: dict, features: list[float]) -> float:
    if fit.get("constant") is not None:
        return float(fit["constant"])
    beta = fit["beta"]
    idx = fit["idx"]
    z = [1.0] + [(features[j] - mu) / sd for j, mu, sd in zip(idx, fit["means"], fit["stds"])]
    return sum(c * v for c, v in zip(beta, z))


DIMENSIONS = ["cpu_seconds", "wall_seconds", "scan_rows", "output_rows"]


def fit_stratum(jobs: list[dict]) -> dict:
    """对一层历史作业（已过滤删失样本）拟合 4 个维度的分位数曲线。

    分位数回归在样本很少时尾部不稳：用训练残差的 90% 分位强制最小半宽，
    使层内覆盖率接近名义 90%，而不是输出看似精确的窄区间。
    """
    x_rows = [j["features"] for j in jobs]
    n = len(jobs)
    idx = _feature_subset(n)
    dims: dict = {}
    residual_halfwidth: dict[str, float] = {}
    for dim in DIMENSIONS:
        y = [math.log1p(max(float(j["targets"][dim]), 0.0)) for j in jobs]
        fits = {}
        for qname, q in (("q05", 0.05), ("q50", 0.50), ("q95", 0.95)):
            fit = fit_quantile_regression(x_rows, y, q, idx)
            if fit is None:
                fits = {}
                break
            fits[qname] = fit
        dims[dim] = fits
        if fits:
            residuals = [abs(yi - predict_quantile(fits["q50"], row))
                         for yi, row in zip(y, x_rows)]
            # 95% 残差分位 + 参数估计不确定性（≈√(1+1/n)）使留一法覆盖率贴近名义 90%
            residual_halfwidth[dim] = quantile(residuals, 0.95) * math.sqrt(1.0 + 1.0 / n)
        else:
            residual_halfwidth[dim] = 0.2
    # 样本越少，回归本身的不确定性越大
    small_n_scale = 1.0 if n >= 12 else 1.0 + (12 - n) * 0.08
    residual_halfwidth = {k: v * small_n_scale for k, v in residual_halfwidth.items()}
    target_range = {
        dim: [min(j["targets"][dim] for j in jobs), max(j["targets"][dim] for j in jobs)]
        for dim in DIMENSIONS
    }
    median_volume = sorted(j["volume_rows"] for j in jobs)[n // 2]
    return {
        "residual_halfwidth": residual_halfwidth,
        "target_range": target_range,
        "median_volume": median_volume,
        "n": n,
        "feature_idx": idx,
        "dims": dims,
        "sample_ids": [j["job_id"] for j in jobs],
        "sample_nos": [j["job_no"] for j in jobs],
        "volume_range": [min(j["volume_rows"] for j in jobs), max(j["volume_rows"] for j in jobs)],
        "load_range": [min(j["load"] for j in jobs), max(j["load"] for j in jobs)],
        "cache_states": sorted({j["cache_state"] for j in jobs}),
        "granularity": jobs[0].get("granularity"),
        "join_types": jobs[0].get("join_types", []),
    }


def train_empirical(jobs: list[dict], min_samples: int = 8) -> dict:
    """构建三层分层经验模型：精确类 → 产品+粒度 → 仅产品组合。"""
    levels = {"exact": {}, "granularity": {}, "products": {}}
    for job in jobs:
        levels["exact"].setdefault(job["class_exact"], []).append(job)
        levels["granularity"].setdefault(job["class_gran"], []).append(job)
        levels["products"].setdefault(job["class_prod"], []).append(job)
    strata: dict = {}
    for level, groups in levels.items():
        kept = {}
        for key, group in groups.items():
            if len(group) < min_samples:
                continue
            kept[key] = fit_stratum(group)
        strata[level] = kept
    return {
        "min_samples": min_samples,
        "strata": strata,
        "league": ["exact", "granularity", "products"],
    }


def lookup_stratum(params: dict, codes: list[str], granularity: str,
                   join_types: list[str]) -> tuple[str | None, dict | None]:
    keys = {
        "exact": class_key(codes, granularity, join_types),
        "granularity": class_key_products(codes, granularity),
        "products": class_key_products_only(codes),
    }
    for level in params.get("league", ["exact", "granularity", "products"]):
        stratum = params.get("strata", {}).get(level, {}).get(keys[level])
        if stratum:
            return level, stratum
    return None, None


def _ordered_interval(low: float, mid: float, high: float, dim: str) -> dict:
    low = max(low, 0.0)
    mid = max(mid, 0.0)
    high = max(high, 0.0)
    if not (low <= mid <= high):
        ordered = sorted([low, mid, high])
        low, mid, high = ordered
    min_rel = MIN_REL_WIDTH.get(dim, 0.05)
    floor = max(mid * min_rel, 1e-9)
    low = min(low, mid - floor) if mid - floor > 0 else max(low, 0.0)
    high = max(high, mid + floor)
    return {"low": round(low, 4), "mid": round(mid, 4), "high": round(high, 4)}


def predict_empirical(stratum: dict, features: list[float],
                      volume_rows: float, load: float) -> dict:
    """在分层模型上预测四维区间，并返回外推/负载放大因素。"""
    intervals: dict = {}
    halfwidths = stratum.get("residual_halfwidth") or {}
    target_range = stratum.get("target_range") or {}
    ref_volume = max(float(stratum.get("median_volume") or 0.0), 1.0)
    scale = min(max(volume_rows / ref_volume, 0.05), 30.0)
    for dim in DIMENSIONS:
        fits = stratum["dims"].get(dim) or {}
        if not fits:
            intervals[dim] = None
            continue
        log_lo = predict_quantile(fits["q05"], features)
        log_mid = predict_quantile(fits["q50"], features)
        log_hi = predict_quantile(fits["q95"], features)
        # 残差半宽兜底：保证层内区间覆盖率接近名义水平
        hw = halfwidths.get(dim, 0.15)
        log_lo = min(log_lo, log_mid - hw)
        log_hi = max(log_hi, log_mid + hw)
        # 外推护栏：分位数回归在样本少 + 规模越界时可能给出数量级失真的值。
        # 按样本目标的量级 × 规模比例（有界）限制线性区间，宁可宽不可荒谬。
        band = target_range.get(dim)
        if band and band[1] > 0:
            lo_bound = max(band[0] * scale * 0.20, 0.0)
            hi_bound = band[1] * scale * 2.5
            log_lo = max(log_lo, math.log1p(lo_bound))
            log_mid = min(max(log_mid, math.log1p(lo_bound)), math.log1p(hi_bound))
            log_hi = min(log_hi, math.log1p(hi_bound))
            log_hi = max(log_hi, log_mid)
        intervals[dim] = _ordered_interval(
            math.expm1(log_lo), math.expm1(log_mid), math.expm1(log_hi), dim)

    widen_factors: list[dict] = []
    vmin, vmax = stratum["volume_range"]
    if volume_rows > vmax and vmax > 0:
        ratio = volume_rows / vmax
        factor = min(2.6, 1.0 + 0.5 * math.log(ratio))
        widen_factors.append({
            "kind": "volume_extrapolation", "ratio": round(ratio, 2),
            "factor": round(factor, 3),
            "detail": f"计划数据规模 {int(volume_rows):,} 行，超出训练样本上限 {int(vmax):,} 行的 {ratio:.1f} 倍",
        })
    elif volume_rows < vmin and volume_rows > 0:
        ratio = vmin / max(volume_rows, 1)
        factor = min(2.0, 1.0 + 0.35 * math.log(ratio))
        widen_factors.append({
            "kind": "volume_extrapolation_small", "ratio": round(ratio, 2),
            "factor": round(factor, 3),
            "detail": f"计划数据规模 {int(volume_rows):,} 行，小于训练样本下限 {int(vmin):,} 行",
        })
    lmin, lmax = stratum["load_range"]
    if load > lmax and lmax > 0:
        ratio = load / lmax
        factor = min(2.2, 1.0 + 0.45 * math.log(ratio))
        widen_factors.append({
            "kind": "load_extrapolation", "ratio": round(ratio, 2),
            "factor": round(factor, 3),
            "detail": f"计划并发负载 {load:g}，高于训练范围上限 {lmax:g}",
        })

    total_factor = 1.0
    for w in widen_factors:
        total_factor = max(total_factor, w["factor"])
        for dim in DIMENSIONS:
            if intervals.get(dim):
                iv = intervals[dim]
                iv["low"] = round(max(iv["low"] / total_factor, 0.0), 4)
                iv["high"] = round(iv["high"] * total_factor, 4)

    return {
        "intervals": intervals,
        "widen_factors_all": widen_factors,
        "widen_factor": round(total_factor, 3),
    }


# --------------------------------------------------------------------------
# 组合算子模型
# --------------------------------------------------------------------------

DEFAULT_SCAN = {
    "ms_per_row": 0.011, "hot_factor": 0.55, "mixed_factor": 0.78, "cold_factor": 1.0,
    "index_effect": 0.65, "missing_penalty": 0.25, "concurrency_sensitivity": 0.16,
}
DEFAULT_JOIN = {
    "ms_per_row": 0.0045, "match_rate": 0.86, "fanout_narrow": 1.0,
    "fanout_wide": 2.6, "wide_threshold": 1.6, "concurrency_sensitivity": 0.16,
}
DEFAULT_AGG = {
    "ms_per_row": 0.0032,
    "reduction": {"明细行": 1.0, "受试者级": 0.14, "月度汇总": 0.02, "窗口排名": 0.6},
    "concurrency_sensitivity": 0.20,
}
DEFAULT_OUTPUT = {"ms_per_row": 0.024, "startup_s": 0.9, "concurrency_sensitivity": 0.12}

KNOWN_JOINS = {"inner", "left", "full", "anti", "semi"}


def _baseline_params(baselines: dict, product_id: int, op: str, defaults: dict) -> dict:
    entry = baselines.get(f"{product_id}:{op}") or baselines.get((product_id, op))
    if entry is None:
        return dict(defaults)
    merged = dict(defaults)
    merged.update(entry.get("params") or {})
    return merged


def _cache_factor(scan_params: dict, cache_state: str) -> float:
    return {
        "热": scan_params.get("hot_factor", 0.55),
        "混合": scan_params.get("mixed_factor", 0.78),
        "冷": scan_params.get("cold_factor", 1.0),
    }.get(cache_state, 0.9)


def operator_cost(view: dict, baselines: dict) -> dict:
    """按算子累加组合成本。view 需含 rows_by_product / joins / granularity /
    cache_state / load / missing_by_product / index_hit_by_product。"""
    rows = {int(k): float(v) for k, v in (view.get("rows_by_product") or {}).items()}
    if not rows and view.get("scan_rows") is not None:
        # 无分产品明细时的退化视图
        first = (view.get("product_ids") or [0])[0]
        rows = {int(first): float(view["scan_rows"])}

    cache_state = view.get("cache_state", "冷")
    load = max(float(view.get("load", 1.0) or 1.0), 0.1)
    missing = view.get("missing_by_product") or {}
    index_hit = view.get("index_hit_by_product") or {}

    scan_detail: list[dict] = []
    scan_cpu = 0.0
    for pid, n in rows.items():
        sp = _baseline_params(baselines, pid, "scan", DEFAULT_SCAN)
        cf = _cache_factor(sp, cache_state)
        miss = float(missing.get(pid, missing.get(str(pid), 0.05)))
        idx = float(index_hit.get(pid, index_hit.get(str(pid), 0.6)))
        # 有筛选时索引命中显著降低扫描量；无筛选时索引收益有限
        has_filter = bool(view.get("has_filters"))
        idx_effect = (1.0 - sp.get("index_effect", 0.65) * idx) if has_filter else (1.0 - 0.15 * idx)
        rows_scanned = n * idx_effect
        cpu = rows_scanned * sp["ms_per_row"] / 1000.0 * cf * (1.0 + sp.get("missing_penalty", 0.25) * miss)
        scan_cpu += cpu
        scan_detail.append({
            "product_id": pid, "input_rows": round(n, 1),
            "scanned_rows": round(rows_scanned, 1), "cpu_seconds": round(cpu, 4),
            "cache_factor": round(cf, 3), "index_hit_rate": round(idx, 3),
            "baseline_version": (baselines.get(f"{pid}:scan") or {}).get("version"),
        })

    # 联结
    join_cpu = 0.0
    join_detail: list[dict] = []
    current_rows: dict[int, float] = dict(rows)
    joined_rows = float(sum(rows.values())) if rows else 0.0
    for j in view.get("joins") or []:
        left = int(j.get("left_product_id"))
        right = int(j.get("right_product_id"))
        jt = (j.get("join_type") or "inner").lower()
        known = jt in KNOWN_JOINS
        left_rows = current_rows.get(left, 0.0)
        right_rows = current_rows.get(right, 0.0)
        jp = _baseline_params(baselines, right, "join", DEFAULT_JOIN)
        fanout = jp.get("fanout_wide", 2.6) if jt in ("left", "full", "inner") else jp.get("fanout_narrow", 1.0)
        # 宽联结：右表每个键多行时扇出放大
        fan = float(jp.get("fanout", 1.0) or 1.0)
        wide = fan >= float(jp.get("wide_threshold", 1.6))
        if jt == "inner":
            out = left_rows * jp["match_rate"] * fan
        elif jt == "left":
            out = left_rows * jp["match_rate"] * fan + left_rows * (1 - jp["match_rate"])
        elif jt == "full":
            out = (left_rows * jp["match_rate"] * fan + left_rows * (1 - jp["match_rate"])
                   + right_rows * (1 - jp["match_rate"]))
        elif jt == "anti":
            out = left_rows * (1 - jp["match_rate"])
        elif jt == "semi":
            out = left_rows * jp["match_rate"]
        else:  # 未见联结类型：保守按 left 处理，后续放大区间
            out = left_rows * jp["match_rate"] * max(fan, 1.2)
        cpu = (left_rows + right_rows) * jp["ms_per_row"] / 1000.0
        join_cpu += cpu
        current_rows[left] = out
        joined_rows = out
        join_detail.append({
            "left_product_id": left, "right_product_id": right, "join_type": jt,
            "known_join_type": known, "left_rows": round(left_rows, 1),
            "right_rows": round(right_rows, 1), "output_rows": round(out, 1),
            "wide_join": wide, "cpu_seconds": round(cpu, 4),
            "baseline_version": (baselines.get(f"{right}:join") or {}).get("version"),
        })

    # 聚合 / 输出：主体为驱动表（第一个联结左表或第一个产品）。
    # 附加在驱动表上的算子（如窗口排名）不属于被联结表的既有算子族。
    granularity = view.get("granularity") or "明细行"
    joins_done = view.get("joins") or []
    if joins_done:
        agg_pid = int(joins_done[0].get("left_product_id"))
    elif rows:
        agg_pid = int(next(iter(rows)))
    else:
        agg_pid = 0
    agg = _baseline_params(baselines, agg_pid, "aggregate", DEFAULT_AGG) if agg_pid else dict(DEFAULT_AGG)
    reduction = float((agg.get("reduction") or {}).get(granularity, 0.7))
    rows_after = joined_rows * reduction
    agg_cpu = joined_rows * agg["ms_per_row"] / 1000.0

    # 输出
    out_pid = agg_pid
    out = _baseline_params(baselines, out_pid, "output", DEFAULT_OUTPUT) if out_pid else dict(DEFAULT_OUTPUT)
    expected = view.get("expected_export_rows")
    output_rows = float(expected) if expected else rows_after
    output_cpu = output_rows * out["ms_per_row"] / 1000.0 + out.get("startup_s", 0.9)

    sensitivity = max(
        (view.get("concurrency_sensitivity") or {}).get("scan", DEFAULT_SCAN["concurrency_sensitivity"]),
        (view.get("concurrency_sensitivity") or {}).get("join", DEFAULT_JOIN["concurrency_sensitivity"]),
    )
    load_factor = 1.0 + sensitivity * (load - 1.0)
    load_factor = max(load_factor, 0.6)

    cpu = (scan_cpu + join_cpu + agg_cpu + output_cpu) * load_factor
    wall = cpu / max(float((view.get("parallelism") or 4)), 1.0) + 0.8

    return {
        "point_costs": {
            "cpu_seconds": round(cpu, 4),
            "wall_seconds": round(wall, 4),
            "scan_rows": round(sum(d["scanned_rows"] for d in scan_detail), 1),
            "output_rows": round(output_rows, 1),
        },
        "operators": {
            "scan": {"cpu_seconds": round(scan_cpu, 4), "products": scan_detail},
            "join": {"cpu_seconds": round(join_cpu, 4), "joins": join_detail},
            "aggregate": {"cpu_seconds": round(agg_cpu, 4), "granularity": granularity,
                          "reduction": reduction, "input_rows": round(joined_rows, 1)},
            "output": {"cpu_seconds": round(output_cpu, 4), "rows": round(output_rows, 1)},
        },
        "load_factor": round(load_factor, 3),
        "has_unknown_join": any(not d["known_join_type"] for d in join_detail),
        "has_wide_join": any(d["wide_join"] for d in join_detail),
        "aggregation_reduction": reduction,
        "output_product_id": int(agg_pid) if agg_pid else None,
        "baseline_product_ids": sorted({d["product_id"] for d in scan_detail}),
    }


def train_operator(jobs: list[dict], baselines: dict, parallelism: float = 4.0) -> dict:
    """用历史作业的实际/预测比值校准算子模型的系统开销偏差。"""
    cpu_ratios, wall_ratios = [], []
    for job in jobs:
        view = {
            "rows_by_product": job["rows_by_product"],
            "joins": job["joins"],
            "granularity": job["granularity"],
            "cache_state": job["cache_state"],
            "load": job["load"],
            "missing_by_product": job.get("missing_by_product") or {},
            "index_hit_by_product": job.get("index_hit_by_product") or {},
            "has_filters": bool(job.get("filters")),
            "expected_export_rows": job.get("output_rows_override"),
            "parallelism": parallelism,
        }
        if not view["rows_by_product"]:
            continue
        pred = operator_cost(view, baselines)
        pc = pred["point_costs"]["cpu_seconds"]
        pw = pred["point_costs"]["wall_seconds"]
        if pc > 0 and job["targets"]["cpu_seconds"] > 0:
            cpu_ratios.append(job["targets"]["cpu_seconds"] / pc)
        if pw > 0 and job["targets"]["wall_seconds"] > 0:
            wall_ratios.append(job["targets"]["wall_seconds"] / pw)
    if not cpu_ratios:
        cpu_ratios = [1.0]
    if not wall_ratios:
        wall_ratios = [1.0]
    # 比值分位数用于区间；按样本量在 log 空间外扩，覆盖参数估计不确定性
    ratio_stats = {}
    for dim, vals in (("cpu_seconds", cpu_ratios), ("wall_seconds", wall_ratios)):
        guard = 0.75 / math.sqrt(max(len(vals), 1))
        q05, q50, q95 = (quantile(vals, q) for q in (0.05, 0.50, 0.95))
        ratio_stats[dim] = {
            "q05": round(math.exp(math.log(max(q05, 1e-6)) - guard), 4),
            "q50": round(q50, 4),
            "q95": round(math.exp(math.log(max(q95, 1e-6)) + guard), 4),
            "n": len(vals),
        }
    return {
        "ratio_stats": ratio_stats,
        "parallelism": parallelism,
        "calibration_load": [min(j["load"] for j in jobs), max(j["load"] for j in jobs)] if jobs else [1.0, 1.0],
        "calibration_volume": [min(j["volume_rows"] for j in jobs), max(j["volume_rows"] for j in jobs)] if jobs else [0, 0],
    }


def predict_operator(view: dict, baselines: dict, calibration: dict) -> dict:
    pred = operator_cost(view, baselines)
    point = pred["point_costs"]
    ratios = calibration.get("ratio_stats") or {}
    cpu_r = ratios.get("cpu_seconds") or {"q05": 0.8, "q50": 1.0, "q95": 1.25}
    wall_r = ratios.get("wall_seconds") or cpu_r

    # 数据画像本身的不确定性：缺失率、索引统计与选择率估计误差
    scan = point["scan_rows"]
    miss = max([float(v) for v in (view.get("missing_by_product") or {}).values()] + [0.05])
    idx = min([float(v) for v in (view.get("index_hit_by_product") or {}).values()] + [0.6])
    scan_u = min(0.45, 0.10 + 0.20 * miss + 0.10 * (1 - idx))
    out = point["output_rows"]
    join_n = len(view.get("joins") or [])
    out_u = min(0.60, 0.10 + 0.08 * join_n + (0.10 if pred["has_wide_join"] else 0.0)
                + (0.18 if pred["has_unknown_join"] else 0.0))
    if view.get("expected_export_rows"):
        out_u = 0.10  # 已显式指定导出行数

    intervals = {
        "cpu_seconds": _ordered_interval(point["cpu_seconds"] * cpu_r["q05"],
                                         point["cpu_seconds"] * cpu_r["q50"],
                                         point["cpu_seconds"] * cpu_r["q95"], "cpu_seconds"),
        "wall_seconds": _ordered_interval(point["wall_seconds"] * wall_r["q05"],
                                          point["wall_seconds"] * wall_r["q50"],
                                          point["wall_seconds"] * wall_r["q95"], "wall_seconds"),
        "scan_rows": _ordered_interval(scan * (1 - scan_u), scan, scan * (1 + scan_u), "scan_rows"),
        "output_rows": _ordered_interval(out * (1 - out_u), out, out * (1 + out_u), "output_rows"),
    }
    return {
        "intervals": intervals,
        "operators": pred["operators"],
        "load_factor": pred["load_factor"],
        "has_unknown_join": pred["has_unknown_join"],
        "has_wide_join": pred["has_wide_join"],
        "scan_uncertainty": round(scan_u, 3),
        "output_uncertainty": round(out_u, 3),
    }
