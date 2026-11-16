<template>
  <section class="page" data-module="rerun">
    <header class="page-head">
      <div>
        <h2>异常重跑管理</h2>
        <p class="page-desc">管理ETL作业的异常重跑任务，指定重跑数据区间和级联策略。每个原始运行只展示当前生效的重跑区间。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="toggleCreate">登记重跑任务</button>
        <button class="btn" type="button" @click="exportRows">导出异常重跑清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form v-if="creating" class="create-panel" @submit.prevent="submitCreate">
      <label v-for="field in createFields" :key="field.key" class="filter-item">
        <span>{{ field.label }}{{ field.required ? ' *' : '' }}</span>
        <input v-model="draft[field.key]" :placeholder="field.placeholder" :required="field.required" />
      </label>
      <div class="create-actions">
        <button class="btn primary" type="submit">提交登记</button>
        <button class="btn ghost" type="button" @click="creating = false">取消</button>
      </div>
    </form>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>关键词</span>
        <input v-model="filters.keyword" placeholder="任务编号 / 原始运行编号 / 区间编号" />
      </label>
      <label class="filter-item">
        <span>原始运行编号</span>
        <input v-model="filters.original_run" placeholder="按原始运行编号检索" />
      </label>
      <label class="filter-item">
        <span>重跑状态</span>
        <select v-model="filters.status">
          <option value="">全部状态</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
          <option value="历史">仅历史数据</option>
        </select>
      </label>
      <label class="filter-item">
        <span>每页条数</span>
        <select v-model.number="pagination.size">
          <option :value="10">10</option>
          <option :value="20">20</option>
          <option :value="50">50</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :class="{ 'row-active': selected?.id === row.id }">
          <td v-for="column in columns" :key="column">{{ cellValue(row, column) }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="showDetail(row)">详情</button>
            <button
              v-for="action in actionsFor(row)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无异常重跑数据，可先登记重跑任务</td>
        </tr>
      </tbody>
    </table>

    <section v-if="selected" class="detail-panel">
      <header class="detail-head">
        <strong>重跑任务详情：{{ selected.任务编号 }}</strong>
        <button class="link" type="button" @click="selected = null">关闭</button>
      </header>
      <dl class="detail-grid">
        <template v-for="field in detailFields" :key="field">
          <dt>{{ field }}</dt>
          <dd>{{ selected[field] ?? '—' }}</dd>
        </template>
      </dl>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条异常重跑记录，第 {{ pagination.page }} / {{ pageCount }} 页</span>
      <span class="pager">
        <button class="btn" type="button" :disabled="pagination.page <= 1" @click="turnPage(-1)">上一页</button>
        <button class="btn" type="button" :disabled="pagination.page >= pageCount" @click="turnPage(1)">下一页</button>
      </span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>

const ENDPOINT = '/api/rerun'
// 列顺序与接口返回的区间字段严格对齐，避免“原始运行对不上、区间字段错位”。
const columns = ["任务编号", "原始运行编号", "重跑区间编号", "重跑区间开始", "重跑区间结束", "重跑原因", "覆盖策略", "依赖级联", "重跑状态"]
const actions = ["提交重跑审批", "启动重跑", "完成重跑", "取消重跑"]
const statuses = ["草稿", "重跑审批中", "执行中", "已完成", "已取消"]
const detailFields = ["任务编号", "原始运行编号", "作业名称", "重跑区间编号", "重跑区间开始", "重跑区间结束", "重跑原因", "覆盖策略", "依赖级联", "重跑状态", "运行执行状态"]

const createFields = [
  { key: '原始运行编号', label: '原始运行编号', placeholder: '例如 RUNT-0001', required: true },
  { key: '重跑区间开始', label: '重跑区间开始', placeholder: 'YYYY-MM-DD HH:MM:SS', required: true },
  { key: '重跑区间结束', label: '重跑区间结束', placeholder: 'YYYY-MM-DD HH:MM:SS', required: true },
  { key: '重跑原因', label: '重跑原因', placeholder: '选填', required: false },
  { key: '覆盖策略', label: '覆盖策略', placeholder: '覆盖重写 / 追加写入', required: false },
  { key: '依赖级联', label: '依赖级联', placeholder: '级联下游 / 不级联', required: false },
]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const selected = ref<Row | null>(null)
const creating = ref(false)
const draft = reactive<Record<string, string>>({})
const filters = reactive({ keyword: '', original_run: '', status: '' })
const pagination = reactive({ page: 1, size: 20 })

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pagination.size)))
const stats = computed(() => [
  { label: "重跑任务总数", value: total.value },
  { label: "执行中任务", value: rows.value.filter((row) => row["重跑状态"] === "执行中").length },
  { label: "已完成任务", value: rows.value.filter((row) => row["重跑状态"] === "已完成").length },
])

function cellValue(row: Row, column: string) {
  const value = row[column]
  if (value === null || value === undefined || value === '') return '—'
  return typeof value === 'boolean' ? (value ? '是' : '否') : value
}

/** 按当前状态给出可执行动作，终态任务不再显示启动/取消。 */
function actionsFor(row: Row): string[] {
  const status = String(row["重跑状态"] ?? '')
  const allowed: Record<string, string[]> = {
    草稿: ['提交重跑审批', '取消重跑'],
    重跑审批中: ['启动重跑', '取消重跑'],
    执行中: ['完成重跑', '取消重跑'],
  }
  return (allowed[status] ?? []).filter((action) => actions.includes(action))
}

function toggleCreate() {
  creating.value = !creating.value
}

function resetFilters() {
  filters.keyword = ''
  filters.original_run = ''
  filters.status = ''
  pagination.page = 1
  void reload()
}

function turnPage(delta: number) {
  const next = pagination.page + delta
  if (next < 1 || next > pageCount.value) return
  pagination.page = next
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function showDetail(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) {
      throw new Error('重跑任务详情读取失败')
    }
    selected.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重跑任务详情读取失败'
  }
}

async function submitCreate() {
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: { ...draft } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '重跑任务登记失败')
    }
    creating.value = false
    for (const field of createFields) draft[field.key] = ''
    pagination.page = 1
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重跑任务登记失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.message || '异常重跑动作未生效，请稍后重试')
    }
    if (selected.value?.id === row.id) selected.value = payload.entry ?? null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '异常重跑操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams()
  if (filters.keyword.trim()) params.set('keyword', filters.keyword.trim())
  if (filters.original_run.trim()) params.set('original_run', filters.original_run.trim())
  if (filters.status) params.set('status', filters.status)
  params.set('page', String(pagination.page))
  params.set('size', String(pagination.size))
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    if (!response.ok) {
      throw new Error('重跑任务列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    if (pagination.page > pageCount.value) {
      pagination.page = pageCount.value
      await reload()
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '异常重跑列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.create-panel { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 12px; }
.create-actions { display: flex; gap: 8px; }
.detail-panel { margin-top: 12px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.detail-head { display: flex; justify-content: space-between; margin-bottom: 8px; }
.detail-grid { display: grid; grid-template-columns: 140px 1fr; gap: 6px 12px; margin: 0; font-size: 13px; }
.detail-grid dt { color: var(--muted); }
.detail-grid dd { margin: 0; }
.row-active { background: #f0f6ff; }
.pager { display: inline-flex; gap: 8px; }
.pager .btn:disabled { opacity: 0.5; cursor: not-allowed; }
</style>
