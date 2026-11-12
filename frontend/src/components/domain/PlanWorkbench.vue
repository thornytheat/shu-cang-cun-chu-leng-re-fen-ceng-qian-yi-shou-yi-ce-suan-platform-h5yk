<script setup lang="ts">
import { computed } from 'vue'
import type { Plan, PlanItem } from '../../types/domain'
import { PLAN_STATUS_LABELS, TIER_LABELS } from '../../constants/enums'
import { gb, money, months, shortTime } from '../../utils/format'
import RiskBadge from '../common/RiskBadge.vue'

const props = defineProps<{ plan: Plan; busy: boolean; canEdit: boolean; canExport: boolean }>()
const emit = defineEmits<{
  (event: 'remove', itemId: string): void
  (event: 'recheck'): void
  (event: 'submit'): void
  (event: 'export'): void
}>()

const isDraft = computed(() => props.plan.status === 'draft')
const blockers = computed(() => props.plan.items.filter((item) => item.risk === 'block'))
const warns = computed(() => props.plan.items.filter((item) => item.risk === 'warn'))
const submitted = computed(() => props.plan.status === 'submitted')

const basis = computed(() => props.plan.basis)
const isStale = computed(() => basis.value.is_stale)
const isFrozen = computed(() => basis.value.state === 'frozen')
const reasons = computed(() => basis.value.reasons ?? [])
const previous = computed(() => basis.value.previous)
const submittedLocked = computed(
  () => props.busy || !props.canEdit || !props.plan.allow_submit,
)
const exportLocked = computed(() => props.busy || !props.canExport || !props.plan.allow_export)

function submitTitle(): string {
  if (!props.plan.allow_submit) {
    if (isStale.value) return '测算依据已失效，先风险复检'
    if (blockers.value.length) return '先移除被阻断的表'
  }
  return ''
}

function exportTitle(): string {
  if (!props.plan.allow_export) {
    return isStale.value ? '测算依据已失效，先风险复检再导出' : '计划里还没有可导出的表'
  }
  return ''
}

/** 逐表变化：复检后与上次结果的差异（有变化才显示「上次」脚注）。 */
function changes(item: PlanItem): string[] {
  const prev = item.previous
  if (!prev) return []
  const notes: string[] = []
  if (prev.save_month !== null && prev.save_month !== item.save_month) {
    notes.push(`月省 ¥${money(prev.save_month)} → ¥${money(item.save_month)}`)
  }
  if (prev.hot_score !== null && prev.hot_score !== item.hot_score) {
    notes.push(`热度 ${prev.hot_score.toFixed(1)} → ${item.hot_score.toFixed(1)}`)
  }
  if (prev.size_gb !== null && prev.size_gb !== item.size_gb) {
    notes.push(`体积 ${gb(prev.size_gb)} → ${gb(item.size_gb)}`)
  }
  if (prev.risk !== item.risk) {
    notes.push(`风险 ${prev.risk ?? '—'} → ${item.risk}`)
  }
  return notes
}

function previousDelta(): string {
  const prev = previous.value
  if (!prev) return ''
  const delta = props.plan.net_save_year - prev.summary.net_save_year
  if (Math.abs(delta) < 0.005) return '净年省与上次一致'
  return `净年省 ${delta >= 0 ? '+' : '−'}¥${money(Math.abs(delta))}`
}
</script>

<template>
  <section class="wb">
    <header class="head">
      <div class="title-block">
        <div class="meta">
          <span class="tag">{{ plan.code }}</span>
          <span class="status mono" :class="`st-${plan.status}`">{{ PLAN_STATUS_LABELS[plan.status] }}</span>
          <span class="mono small">策略 v{{ plan.policy_version }} · 阈值 {{ plan.threshold }}</span>
          <span
            v-if="basis.current_policy_version !== null && basis.current_policy_version !== plan.policy_version"
            class="mono small now"
          >当前策略 v{{ basis.current_policy_version }}</span>
          <span class="mono small">测算依据 {{ basis.as_of ?? '—' }}</span>
          <span class="basis-pill mono" :class="`bs-${basis.state}`">
            {{
              !basis.fingerprint
                ? '依据未记录'
                : isStale
                  ? '依据失效'
                  : isFrozen
                    ? '口径已冻结'
                    : '依据有效'
            }}
          </span>
        </div>
        <h2 class="title">{{ plan.title }}</h2>
        <p class="sub mono">
          {{ plan.created_by }}
          <template v-if="plan.rechecked_at"> · 复检 {{ shortTime(plan.rechecked_at) }}</template>
          <template v-if="plan.submitted_at"> · 提交 {{ shortTime(plan.submitted_at) }}（{{ plan.submitted_by }}）</template>
          <template v-if="isFrozen"> · 提交时口径已固化，后续数据变化不改写本计划</template>
        </p>
      </div>
      <div class="actions">
        <button
          class="btn btn-sm"
          type="button"
          :disabled="busy || !canEdit || !isDraft"
          :title="isDraft ? '' : '已提交计划口径已冻结，不再复检'"
          @click="emit('recheck')"
        >
          风险复检
        </button>
        <button
          class="btn btn-primary btn-sm"
          type="button"
          :disabled="submittedLocked"
          :title="submitTitle()"
          @click="emit('submit')"
        >
          提交计划
        </button>
        <button
          class="btn btn-sm"
          type="button"
          :disabled="exportLocked"
          :title="exportTitle()"
          @click="emit('export')"
        >
          导出工单 CSV
        </button>
      </div>
    </header>

    <p v-if="isStale" class="banner basis-stale mono">
      ■ 测算依据已失效，上次的省额与风险不再代表当前口径 —— 提交与导出已锁定，请先「风险复检」。
      <span class="basis-why">{{ reasons.join('；') }}</span>
    </p>
    <p v-else-if="isFrozen && basis.drift" class="banner basis-frozen mono">
      ▲ 计划已提交，工单口径冻结在 {{ basis.as_of ?? '—' }}（策略 v{{ basis.policy_version }}）。
      当前依据已不同（{{ reasons.join('；') }}），但历史迁移清单不再改写。
    </p>

    <div v-if="previous" class="compare mono">
      <span class="cmp-title">上次结果</span>
      <span>
        {{ previous.items.length }} 张 · 月省 ¥{{ money(previous.summary.save_month) }} · 净年省 ¥{{
          money(previous.summary.net_save_year)
        }} · 回收期 {{ months(previous.summary.avg_payback) }} · 测算依据
        {{ previous.basis_as_of ?? '—' }}（策略 v{{ previous.policy_version }}）
      </span>
      <span class="cmp-delta">{{ previousDelta() }}</span>
    </div>

    <div class="summary">
      <div class="cell">
        <span class="label">计划表数</span>
        <strong class="mono">{{ plan.item_count }}</strong>
      </div>
      <div class="cell">
        <span class="label">总体积</span>
        <strong class="mono">{{ gb(plan.total_size_gb) }}</strong>
      </div>
      <div class="cell">
        <span class="label">月省</span>
        <strong class="mono">¥{{ money(plan.save_month) }}</strong>
      </div>
      <div class="cell">
        <span class="label">年省</span>
        <strong class="mono">¥{{ money(plan.save_year) }}</strong>
      </div>
      <div class="cell">
        <span class="label">迁移成本</span>
        <strong class="mono">¥{{ money(plan.migration_cost) }}</strong>
      </div>
      <div class="cell emph">
        <span class="label">净年省（首年）</span>
        <strong class="mono">¥{{ money(plan.net_save_year) }}</strong>
      </div>
      <div class="cell emph">
        <span class="label">平均回收期</span>
        <strong class="mono">{{ months(plan.avg_payback) }}</strong>
      </div>
      <div class="cell">
        <span class="label">风险</span>
        <strong class="mono">
          <span class="blk">{{ plan.risk_block_count }} 阻断</span> ·
          <span class="wn">{{ plan.risk_warn_count }} 需确认</span>
        </strong>
      </div>
    </div>

    <p v-if="blockers.length" class="banner block mono">
      ■ {{ blockers.length }} 张被阻断：{{ blockers.map((item) => item.table_name).join('、') }} —— 冷存要求近 30
      天零访问，逐张移除后才能提交
    </p>
    <p v-else-if="warns.length" class="banner warn mono">
      ▲ {{ warns.length }} 张需报表 owner 书面确认：{{ warns.map((item) => item.table_name).join('、') }}
    </p>
    <p v-else-if="plan.items.length" class="banner ok mono">○ 风险闸门全部通过，可提交</p>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th class="w-i">#</th>
            <th>库表</th>
            <th>温区</th>
            <th class="num">体积</th>
            <th class="num">热度</th>
            <th class="num">月省</th>
            <th class="num">回收期</th>
            <th class="num">净年省</th>
            <th>风险 / 原因</th>
            <th class="w-act"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(item, index) in plan.items" :key="item.id" :class="{ blocked: item.risk === 'block' }">
            <td class="mono idx">{{ String(index + 1).padStart(2, '0') }}</td>
            <td class="mono name">
              {{ item.table_name }}
              <span class="iname-cost mono">迁移成本 ¥{{ money(item.migration_cost) }}</span>
            </td>
            <td class="mono tiercell">
              {{ item.from_tier === 'hot' ? 'H' : item.from_tier === 'warm' ? 'W' : 'C' }} →
              {{ item.suggested_tier === 'hot' ? 'H' : item.suggested_tier === 'warm' ? 'W' : 'C' }}
            </td>
            <td class="num mono">{{ gb(item.size_gb) }}</td>
            <td class="num mono">{{ item.hot_score.toFixed(1) }}</td>
            <td class="num mono">¥{{ money(item.save_month) }}</td>
            <td class="num mono">{{ months(item.payback_months) }}</td>
            <td class="num mono">¥{{ money(item.net_save_year) }}</td>
            <td class="riskcell">
              <RiskBadge :risk="item.risk" :reason="item.reasons" />
              <span class="reason">{{ item.reasons }}</span>
              <span v-if="changes(item).length" class="prev-note mono">
                上次（{{ item.previous?.basis_as_of ?? '—' }}）：{{ changes(item).join(' · ') }}
              </span>
            </td>
            <td class="w-act">
              <button
                v-if="isDraft && canEdit"
                class="btn btn-danger btn-sm"
                type="button"
                :disabled="busy"
                @click="emit('remove', item.id)"
              >
                移除
              </button>
              <span v-else-if="submitted" class="mono done">已提交</span>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="!plan.items.length" class="none mono">
        计划里还没有表 —— 回到「策略与试算」勾选候选表后再建一次计划。
      </p>
    </div>
    <p v-if="plan.note" class="note mono">备注：{{ plan.note }}</p>
  </section>
</template>

<style scoped>
.wb {
  border: 1px solid var(--line);
  background: var(--frost-1);
}
.head {
  display: flex;
  justify-content: space-between;
  gap: 14px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--line-strong);
  background: var(--frost-2);
  flex-wrap: wrap;
}
.meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.status {
  font-size: 10.5px;
  padding: 1px 7px;
  border: 1px solid currentColor;
  letter-spacing: 0.06em;
}
.st-draft {
  color: var(--warm);
}
.st-submitted {
  color: var(--ok);
}
.small {
  font-size: 10px;
  color: var(--slate-faint);
}
.small.now {
  color: var(--wood-deep);
}
.title {
  font-size: var(--step-2);
  font-weight: 700;
  margin-top: 4px;
}
.sub {
  font-size: 10.5px;
  color: var(--slate-faint);
  margin-top: 2px;
}
.actions {
  display: flex;
  gap: 6px;
  align-items: flex-start;
  flex-wrap: wrap;
}
.summary {
  display: grid;
  grid-template-columns: repeat(8, minmax(0, 1fr));
  border-bottom: var(--hair);
}
.cell {
  padding: 9px 12px;
  border-right: var(--hair);
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.cell .label {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.cell:last-child {
  border-right: 0;
}
.cell strong {
  font-size: 15px;
  font-weight: 700;
  letter-spacing: -0.01em;
}
.cell.emph {
  background: var(--frost-0);
}
.cell.emph strong {
  color: var(--wood-deep);
}
.blk {
  color: var(--block);
}
.wn {
  color: var(--warn);
}
.banner {
  margin: 0;
  padding: 8px 14px;
  font-size: 10.8px;
  border-bottom: var(--hair);
  line-height: 1.5;
}
.banner.block {
  background: var(--hot-wash);
  color: var(--block);
}
.banner.warn {
  background: var(--warm-wash);
  color: var(--warm);
}
.banner.ok {
  background: var(--ok-wash);
  color: var(--ok);
}
.banner.basis-stale {
  background: var(--hot-wash);
  color: var(--block);
  border-left: 3px solid var(--block);
}
.banner.basis-frozen {
  background: var(--frost-2);
  color: var(--slate-soft);
  border-left: 3px solid var(--wood-pale);
}
.basis-why {
  display: block;
  margin-top: 2px;
  font-size: 10px;
  color: var(--slate-soft);
  word-break: break-word;
}
.basis-pill {
  font-size: 9.5px;
  padding: 1px 6px;
  border: 1px solid currentColor;
  letter-spacing: 0.05em;
}
.bs-fresh {
  color: var(--ok);
}
.bs-stale {
  color: var(--block);
}
.bs-frozen {
  color: var(--wood-deep);
}
.bs-unknown {
  color: var(--slate-faint);
}
.compare {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 12px;
  padding: 7px 14px;
  border-bottom: var(--hair);
  background: var(--frost-0);
  font-size: 10.3px;
  color: var(--slate-soft);
}
.cmp-title {
  font-size: 9.5px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--slate-faint);
}
.cmp-delta {
  color: var(--wood-deep);
  font-weight: 600;
}
.prev-note {
  display: block;
  margin-top: 3px;
  padding-top: 3px;
  border-top: 1px dashed var(--line);
  font-size: 9.6px;
  color: var(--slate-faint);
  line-height: 1.5;
}
.table-wrap {
  overflow-x: auto;
}
table {
  min-width: 860px;
}
thead th {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--slate-faint);
  text-align: left;
  padding: 7px 9px;
  border-bottom: var(--hair);
  font-weight: 500;
  white-space: nowrap;
}
tbody td {
  padding: 6px 9px;
  border-bottom: var(--hair);
  font-size: 12px;
  vertical-align: top;
}
tbody tr.blocked {
  background: var(--hot-wash);
}
.idx {
  color: var(--slate-faint);
  font-size: 10.5px;
}
.name {
  min-width: 216px;
}
.iname-cost {
  display: block;
  font-size: 9.5px;
  color: var(--slate-faint);
  margin-top: 1px;
}
.tiercell {
  font-size: 10.5px;
  color: var(--slate-soft);
  white-space: nowrap;
}
.num {
  text-align: right;
  font-size: 11.5px;
  white-space: nowrap;
}
.riskcell {
  min-width: 210px;
}
.reason {
  display: block;
  font-size: 10px;
  color: var(--slate-soft);
  margin-top: 2px;
  line-height: 1.45;
}
.done {
  font-size: 10px;
  color: var(--ok);
}
.none {
  padding: 16px;
  font-size: 11.5px;
  color: var(--slate-faint);
}
.note {
  padding: 7px 14px;
  font-size: 10.5px;
  color: var(--slate-faint);
  border-top: var(--hair);
}
.w-i { width: 34px; }
.w-act { width: 74px; }
@media (max-width: 1200px) {
  .summary {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }
}
@media (max-width: 700px) {
  .summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
