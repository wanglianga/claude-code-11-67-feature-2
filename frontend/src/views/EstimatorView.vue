<template>
  <div>
    <el-alert type="info" :closable="false" style="margin-bottom: 12px"
      title="提交前成本估计：只输出各资源维度的估计区间、主要不确定因素与适用样本范围，供课题负责人调整方案 / 申请额度；不代替额度预留与任务调度。" />

    <el-row :gutter="14">
      <el-col :span="8">
        <el-card shadow="never">
          <template #header>
            <div class="card-head">
              <b>分析计划</b>
              <el-button size="small" @click="loadAll">刷新</el-button>
            </div>
          </template>
          <el-table :data="plans" size="small" height="360" highlight-current-row
            @current-change="(row) => row && openPlan(row.id)">
            <el-table-column prop="plan_no" label="计划号" width="132" />
            <el-table-column prop="title" label="标题" min-width="150" show-overflow-tooltip />
            <el-table-column label="操作" width="70">
              <template #default="{ row }">
                <el-button size="small" text type="primary" @click.stop="openPlan(row.id)">查看</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card shadow="never" style="margin-top: 14px">
          <template #header><b>历史作业账本</b></template>
          <el-table :data="jobs" size="small" height="280">
            <el-table-column prop="job_no" label="作业号" width="120" />
            <el-table-column label="状态" width="86">
              <template #default="{ row }">
                <el-tag :type="row.censored ? 'warning' : 'success'" size="small">
                  {{ row.censored ? '中止(删失)' : row.status }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="CPU(s)" width="80">
              <template #default="{ row }">{{ row.cpu_seconds }}</template>
            </el-table-column>
            <el-table-column prop="env_load" label="负载" width="60" />
            <el-table-column prop="cache_state" label="缓存" width="56" />
            <el-table-column label="可训练" width="70">
              <template #default="{ row }">
                <span :class="row.training_eligible ? 'ok' : 'muted'">
                  {{ row.training_eligible ? '是' : '否' }}
                </span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>

      <el-col :span="16">
        <el-card shadow="never" v-if="plan">
          <template #header>
            <div class="card-head">
              <div>
                <b>{{ plan.plan_no }}｜{{ plan.title }}</b>
                <span class="sub">产品 {{ plan.products.map((p) => p.code + ' v' + p.catalog_version).join('、') }}</span>
              </div>
              <div class="actions" v-if="canEstimate">
                <el-select v-model="form.route" size="small" style="width: 132px">
                  <el-option value="auto" label="自动选择路线" />
                  <el-option value="empirical" label="经验模型" />
                  <el-option value="operator" label="算子模型" />
                  <el-option value="both" label="双路线对比" />
                </el-select>
                <el-select v-model="form.cache_state" size="small" style="width: 104px" clearable
                  placeholder="缓存预期">
                  <el-option v-for="c in ['冷', '热', '混合']" :key="c" :value="c" :label="c" />
                </el-select>
                <el-input-number v-model="form.concurrent_jobs" size="small" :min="0.1" :step="0.5"
                  style="width: 130px" controls-position="right" />
                <el-button type="primary" size="small" :loading="estimating" @click="runEstimate">估计</el-button>
              </div>
            </div>
          </template>
          <el-descriptions :column="3" size="small" border>
            <el-descriptions-item label="时间范围">{{ plan.time_from }} ~ {{ plan.time_to }}</el-descriptions-item>
            <el-descriptions-item label="输出粒度">{{ plan.output_granularity }}</el-descriptions-item>
            <el-descriptions-item label="缓存预期">{{ plan.cache_expectation }}</el-descriptions-item>
            <el-descriptions-item label="筛选条件" :span="2">
              {{ plan.filters.length ? plan.filters.map((f) => `${f.field} ${f.op} ${fmtVal(f.value)}`).join('；') : '无（全量扫描）' }}
            </el-descriptions-item>
            <el-descriptions-item label="并发预期">{{ plan.load_hint }}</el-descriptions-item>
            <el-descriptions-item label="联结" :span="3">
              {{ plan.joins.length ? plan.joins.map((j) => `${j.left_product_id} ${j.join_type} ${j.right_product_id}`).join('；') : '无联结' }}
            </el-descriptions-item>
          </el-descriptions>
        </el-card>

        <el-card shadow="never" style="margin-top: 14px" v-if="estimate" v-loading="estimating">
          <template #header>
            <div class="card-head">
              <div>
                <b>估计结果 {{ estimate.estimate_no }}</b>
                <el-tag size="small" style="margin-left: 8px" :type="routeType(estimate.route)">
                  {{ routeLabel(estimate.route) }}
                </el-tag>
                <el-tag size="small" style="margin-left: 6px" :type="statusType(estimate.status)">
                  {{ statusLabel(estimate.status) }}
                </el-tag>
                <el-tag size="small" style="margin-left: 6px" type="info">模型 v{{ estimate.model?.version_label }}</el-tag>
              </div>
              <el-button size="small" text type="primary" @click="detailDrawer = true">训练样本 / 模型回查</el-button>
            </div>
          </template>

          <el-alert v-if="estimate.is_stale" type="error" :closable="false" style="margin-bottom: 10px"
            :title="`该估计已过期，不得沿用原成本：${estimate.stale_reason}`" />
          <el-alert v-for="(w, i) in estimate.warnings" :key="i" type="warning" :closable="false"
            :title="w" style="margin-bottom: 8px" />

          <el-table :data="intervalRows" size="small" border>
            <el-table-column prop="label" label="资源维度" width="140" />
            <el-table-column prop="low" label="低（P5）" width="130" />
            <el-table-column prop="mid" label="中（P50）" width="130" />
            <el-table-column prop="high" label="高（P95）" width="130" />
            <el-table-column label="区间视图（各维度独立 0~P95 标尺）" min-width="200">
              <template #default="{ row }">
                <div class="bar">
                  <span class="bar-fill" :style="{ left: row.barLeft + '%', width: row.barWidth + '%' }" />
                  <span class="bar-mid" :style="{ left: row.midPos + '%' }" />
                </div>
              </template>
            </el-table-column>
          </el-table>

          <div class="two-col">
            <div>
              <h4>主要不确定因素</h4>
              <el-table :data="estimate.uncertainty_factors" size="small" border>
                <el-table-column prop="kind" label="因素" width="150" />
                <el-table-column label="放大" width="66">
                  <template #default="{ row }">×{{ row.factor }}</template>
                </el-table-column>
                <el-table-column prop="detail" label="说明" min-width="220" show-overflow-tooltip />
              </el-table>
            </div>
            <div>
              <h4>适用样本范围</h4>
              <el-descriptions :column="1" size="small" border>
                <el-descriptions-item label="样本数">{{ estimate.applicable_sample_range.sample_count ?? '—' }}</el-descriptions-item>
                <el-descriptions-item label="数据规模">
                  {{ rangeText(estimate.applicable_sample_range.volume_range, '行') }}
                </el-descriptions-item>
                <el-descriptions-item label="并发负载">
                  {{ rangeText(estimate.applicable_sample_range.load_range) }}
                </el-descriptions-item>
                <el-descriptions-item label="覆盖粒度">
                  {{ arrText(estimate.applicable_sample_range.granularity) }}
                </el-descriptions-item>
                <el-descriptions-item label="联结类型">
                  {{ arrText(estimate.applicable_sample_range.join_types) }}
                </el-descriptions-item>
                <el-descriptions-item label="负载判定">
                  {{ estimate.load_assessment.policy }}
                </el-descriptions-item>
              </el-descriptions>
            </div>
          </div>

          <h4>路线选择说明</h4>
          <el-alert type="info" :closable="false"
            :title="`${estimate.route_recommendation.reason}`" style="margin-bottom: 8px" />
          <el-table :data="routeCompareRows" size="small" border>
            <el-table-column prop="route" label="路线" width="120" />
            <el-table-column prop="available" label="可用" width="70" />
            <el-table-column prop="coverage" label="覆盖层级/口径" width="130" />
            <el-table-column prop="cpu" label="CPU 区间（秒）" min-width="160" />
            <el-table-column prop="note" label="说明" min-width="200" show-overflow-tooltip />
          </el-table>

          <h4>估计历史</h4>
          <el-table :data="estimates" size="small">
            <el-table-column prop="estimate_no" label="编号" width="140" />
            <el-table-column label="路线" width="100">
              <template #default="{ row }">{{ routeLabel(row.route) }}</template>
            </el-table-column>
            <el-table-column label="CPU 区间" min-width="180">
              <template #default="{ row }">{{ cpuText(row) }}</template>
            </el-table-column>
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <el-tag size="small" :type="row.is_stale ? 'danger' : statusType(row.status)">
                  {{ row.is_stale ? '已过期' : statusLabel(row.status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="created_at" label="时间" width="160" />
            <el-table-column label="操作" width="72">
              <template #default="{ row }">
                <el-button size="small" text type="primary" @click="loadEstimate(row.id)">回查</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" style="margin-top: 14px">
      <template #header>
        <div class="card-head">
          <b>数据规模画像（分区记录量 / 缺失率 / 索引统计 / 缓存状态）</b>
          <el-select v-model="profileProduct" size="small" style="width: 170px" clearable placeholder="按产品筛选">
            <el-option v-for="p in products" :key="p.id" :value="p.id" :label="`${p.code} ${p.name}`" />
          </el-select>
        </div>
      </template>
      <el-table :data="filteredProfiles" size="small" max-height="280">
        <el-table-column label="产品" width="90">
          <template #default="{ row }">{{ productCode(row.product_id) }}</template>
        </el-table-column>
        <el-table-column prop="partition" label="分区" width="80" />
        <el-table-column label="时间范围" width="190">
          <template #default="{ row }">{{ row.time_from }} ~ {{ row.time_to }}</template>
        </el-table-column>
        <el-table-column prop="record_count" label="记录量" width="110" />
        <el-table-column label="缺失率" width="80">
          <template #default="{ row }">{{ (row.missing_rate * 100).toFixed(1) }}%</template>
        </el-table-column>
        <el-table-column label="索引命中" width="90">
          <template #default="{ row }">{{ (row.index_stats.index_hit_rate * 100).toFixed(0) }}%</template>
        </el-table-column>
        <el-table-column label="缓存" width="70">
          <template #default="{ row }">
            <el-tag size="small" :type="row.cache_state === '热' ? 'success' : row.cache_state === '冷' ? 'info' : 'warning'">
              {{ row.cache_state }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="note" label="备注" min-width="200" />
      </el-table>
    </el-card>

    <el-drawer v-model="detailDrawer" title="训练样本与模型版本回查" size="62%">
      <template v-if="estimate">
        <h4>模型版本</h4>
        <el-descriptions :column="2" size="small" border v-if="estimate.model">
          <el-descriptions-item label="版本">{{ estimate.model.version_label }}</el-descriptions-item>
          <el-descriptions-item label="路线">{{ routeLabel(estimate.model.route) }}</el-descriptions-item>
          <el-descriptions-item label="训练时间">{{ estimate.model.trained_at }}</el-descriptions-item>
          <el-descriptions-item label="训练样本数">{{ estimate.model.training_sample_count }}</el-descriptions-item>
          <el-descriptions-item label="排除删失样本">{{ estimate.model.excluded_censored_count }}</el-descriptions-item>
          <el-descriptions-item label="样本内 90% 区间覆盖">
            CPU {{ pct(estimate.model.metrics?.coverage?.cpu_seconds) }}｜扫描 {{ pct(estimate.model.metrics?.coverage?.scan_rows) }}｜导出 {{ pct(estimate.model.metrics?.coverage?.output_rows) }}
          </el-descriptions-item>
        </el-descriptions>
        <el-alert type="info" :closable="false" style="margin: 8px 0"
          :title="estimate.model?.metrics?.note || ''" />

        <h4>本次估计使用的历史样本（{{ estimate.applied_samples?.length || 0 }} 条）</h4>
        <el-table :data="estimate.applied_samples || []" size="small" border max-height="300">
          <el-table-column prop="job_no" label="作业号" width="120" />
          <el-table-column prop="role" label="作用" width="180" />
          <el-table-column prop="cpu_seconds" label="实际 CPU(s)" width="100" />
          <el-table-column prop="scan_rows" label="扫描行" width="100" />
          <el-table-column prop="output_rows" label="导出行" width="100" />
          <el-table-column prop="env_load" label="负载" width="60" />
          <el-table-column prop="cache_state" label="缓存" width="60" />
        </el-table>

        <h4>组合算子基准（算子模型路线）</h4>
        <el-table :data="estimate.baselines_used || []" size="small" border>
          <el-table-column prop="product_id" label="产品ID" width="80" />
          <el-table-column prop="op" label="算子" width="90" />
          <el-table-column prop="baseline_version" label="基准版本" width="100" />
          <el-table-column label="已校准" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.calibrated ? 'success' : 'warning'">
                {{ row.calibrated ? '是' : '默认基准' }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>

        <h4>估计时计划快照</h4>
        <pre class="snapshot">{{ JSON.stringify(estimate.plan_snapshot, null, 2) }}</pre>
        <el-alert type="warning" :closable="false" :title="estimate.stale_rule" style="margin-top: 8px" />
        <el-alert type="info" :closable="false" :title="estimate.disclaimer" style="margin-top: 8px" />
      </template>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api, { currentUser } from '../api'

const plans = ref([])
const jobs = ref([])
const profiles = ref([])
const products = ref([])
const plan = ref(null)
const estimates = ref([])
const estimate = ref(null)
const estimating = ref(false)
const detailDrawer = ref(false)
const profileProduct = ref(null)
const form = reactive({ route: 'auto', cache_state: null, concurrent_jobs: 1 })
const canEstimate = computed(() => ['ADMIN', 'PI', 'ANALYST'].includes(currentUser()?.role))

const DIMS = [
  ['cpu_seconds', 'CPU 时间（秒）'],
  ['wall_seconds', '实际耗时（秒）'],
  ['scan_rows', '扫描行数'],
  ['output_rows', '导出行数'],
]

const filteredProfiles = computed(() =>
  profileProduct.value ? profiles.value.filter((p) => p.product_id === profileProduct.value) : profiles.value)

const intervalRows = computed(() => {
  if (!estimate.value?.intervals) return []
  return DIMS.map(([key, label]) => {
    const iv = estimate.value.intervals[key] || { low: 0, mid: 0, high: 0 }
    const scale = Math.max(iv.high, 1e-9)
    return {
      label,
      low: fmtNum(iv.low),
      mid: fmtNum(iv.mid),
      high: fmtNum(iv.high),
      barLeft: ((iv.low / scale) * 100).toFixed(1),
      barWidth: (((iv.high - iv.low) / scale) * 100).toFixed(1),
      midPos: ((iv.mid / scale) * 100).toFixed(1),
    }
  })
})

const routeCompareRows = computed(() => {
  const rec = estimate.value?.route_recommendation
  if (!rec) return []
  return ['empirical', 'operator'].map((r) => {
    const item = rec[r] || {}
    return {
      route: routeLabel(r) + (rec.recommended === r ? '（推荐）' : ''),
      available: item.available ? '是' : '否',
      coverage: item.coverage_level || '—',
      cpu: item.intervals?.cpu_seconds
        ? `${fmtNum(item.intervals.cpu_seconds.low)} ~ ${fmtNum(item.intervals.cpu_seconds.high)}`
        : '—',
      note: item.reason || '',
    }
  })
})

function fmtNum(v) {
  if (v === null || v === undefined) return '—'
  if (Math.abs(v) >= 1000) return Math.round(v).toLocaleString()
  return Number(v).toFixed(2)
}
function fmtVal(v) {
  if (v === null || v === undefined) return ''
  return Array.isArray(v) ? v.join(',') : String(v)
}
function rangeText(range, unit = '') {
  if (!range || range.length !== 2) return '—'
  return `${fmtNum(range[0])} ~ ${fmtNum(range[1])}${unit}`
}
function arrText(v) {
  if (!v) return '—'
  return Array.isArray(v) ? (v.length ? v.join('、') : '—') : String(v)
}
function pct(v) {
  return v === undefined || v === null ? '—' : `${(v * 100).toFixed(0)}%`
}
function routeLabel(r) {
  return { empirical: '经验模型', operator: '算子模型' }[r] || r
}
function routeType(r) {
  return r === 'empirical' ? 'success' : 'warning'
}
function statusLabel(s) {
  return { ok: '常规估计', widened: '已扩大区间', refused: '拒绝常规估计', insufficient_samples: '样本不足' }[s] || s
}
function statusType(s) {
  return { ok: 'success', widened: 'warning', refused: 'danger' }[s] || 'info'
}
function productCode(id) {
  return products.value.find((p) => p.id === id)?.code || id
}
function cpuText(row) {
  const iv = row.intervals?.cpu_seconds
  return iv ? `${fmtNum(iv.low)} ~ ${fmtNum(iv.high)}` : '—'
}

async function loadAll() {
  const [pl, jb, pf, pd] = await Promise.all([
    api.get('/estimator/plans'), api.get('/estimator/jobs'),
    api.get('/estimator/profiles'), api.get('/catalog/products'),
  ])
  plans.value = pl.data
  jobs.value = jb.data
  profiles.value = pf.data
  products.value = pd.data
}

async function openPlan(id) {
  const { data } = await api.get(`/estimator/plans/${id}`)
  plan.value = data.plan
  estimates.value = data.estimates
  form.concurrent_jobs = data.plan.load_hint || 1
  if (data.estimates.length) await loadEstimate(data.estimates[0].id)
  else estimate.value = null
}

async function loadEstimate(id) {
  const { data } = await api.get(`/estimator/estimates/${id}`)
  estimate.value = data
}

async function runEstimate() {
  if (!plan.value) return
  estimating.value = true
  try {
    const { data } = await api.post(`/estimator/plans/${plan.value.id}/estimate`, {
      route: form.route, cache_state: form.cache_state || null,
      concurrent_jobs: form.concurrent_jobs,
    })
    estimate.value = data
    const { data: detail } = await api.get(`/estimator/plans/${plan.value.id}`)
    estimates.value = detail.estimates
    if (data.status === 'refused') ElMessage.warning('并发负载远离校准范围：仅返回参考区间')
    else if (data.status === 'widened') ElMessage.warning('区间已扩大：存在外推或负载偏差')
    else ElMessage.success('估计完成（请按区间而非点值使用）')
  } catch (err) {
    ElMessage.error(err.response?.data?.detail || '估计失败')
  } finally {
    estimating.value = false
  }
}

onMounted(async () => {
  await loadAll()
  if (plans.value.length) await openPlan(plans.value[0].id)
})
</script>

<style scoped>
.card-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.actions { display: flex; gap: 6px; align-items: center; }
.sub { margin-left: 10px; font-size: 12px; color: #7a8899; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
h4 { margin: 14px 0 8px; }
.bar { position: relative; height: 14px; background: #eef2f7; border-radius: 7px; overflow: hidden; }
.bar-fill { position: absolute; top: 0; bottom: 0; background: #8fb8e8; border-radius: 7px; }
.bar-mid { position: absolute; top: -2px; bottom: -2px; width: 2px; background: #1f5fa8; }
.ok { color: #2e7d32; }
.muted { color: #b23b3b; }
.snapshot { background: #f6f8fb; padding: 10px; border-radius: 6px; font-size: 12px; max-height: 260px; overflow: auto; }
</style>
