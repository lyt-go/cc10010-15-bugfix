<template>
  <section class="page" data-module="runtime">
    <header class="page-head">
      <div>
        <h2>运行实例管理</h2>
        <p class="page-desc">记录每次ETL作业的运行实例，并展示由唯一重跑区间记录同步回来的区间与重跑状态。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="toggleCreate">登记运行记录</button>
        <button class="btn" type="button" @click="exportRows">导出运行实例清单</button>
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
        <input v-model="filters.keyword" placeholder="记录编号 / 作业名称 / 原始运行 / 区间" />
      </label>
      <label class="filter-item">
        <span>执行状态</span>
        <select v-model="filters.status">
          <option value="">全部状态</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
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
          <td :colspan="columns.length + 1" class="empty-state">暂无运行实例数据，可先登记运行记录</td>
        </tr>
      </tbody>
    </table>

    <section v-if="selected" class="detail-panel">
      <header class="detail-head">
        <strong>运行记录详情：{{ selected.记录编号 }}</strong>
        <button class="link" type="button" @click="selected = null">关闭</button>
      </header>
      <dl class="detail-grid">
        <template v-for="field in detailFields" :key="field">
          <dt>{{ field }}</dt>
          <dd>{{ selected[field] ?? '—' }}</dd>
        </template>
      </dl>
      <div class="interval-block">
        <strong>关联重跑区间</strong>
        <table class="data-table">
          <thead>
            <tr><th>重跑区间编号</th><th>任务编号</th><th>重跑区间开始</th><th>重跑区间结束</th><th>重跑状态</th><th>是否当前</th></tr>
          </thead>
          <tbody>
            <tr v-for="interval in intervals" :key="String(interval.id)">
              <td>{{ interval["重跑区间编号"] ?? '—' }}</td>
              <td>{{ interval["任务编号"] ?? '—' }}</td>
              <td>{{ interval["重跑区间开始"] ?? '—' }}</td>
              <td>{{ interval["重跑区间结束"] ?? '—' }}</td>
              <td>{{ interval["重跑状态"] ?? '—' }}</td>
              <td>{{ interval["是否当前重跑区间"] ? '是' : '否' }}</td>
            </tr>
            <tr v-if="!intervals.length">
              <td colspan="6" class="empty-state">该运行暂无重跑区间</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条运行实例记录，第 {{ pagination.page }} / {{ pageCount }} 页</span>
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

const ENDPOINT = '/api/runtime'
// 列顺序与后端投影字段一致：原始运行、区间、状态一一对应。
const columns = ["记录编号", "作业名称", "开始时间", "结束时间", "执行状态", "重跑区间编号", "重跑区间开始", "重跑区间结束", "重跑状态", "读取行数", "写入行数", "错误信息"]
const actions = ["启动作业", "终止作业", "标记成功"]
const statuses = ["草稿", "运行中", "成功", "失败", "已终止"]
const detailFields = ["记录编号", "作业名称", "开始时间", "结束时间", "执行状态", "原始运行编号", "重跑区间编号", "重跑区间开始", "重跑区间结束", "重跑状态", "读取行数", "写入行数", "错误信息"]

const createFields = [
  { key: '记录编号', label: '记录编号', placeholder: '例如 RUNT-0004', required: true },
  { key: '作业名称', label: '作业名称', placeholder: '例如 订单明细同步', required: true },
  { key: '开始时间', label: '开始时间', placeholder: 'YYYY-MM-DD HH:MM:SS', required: true },
  { key: '结束时间', label: '结束时间', placeholder: '选填', required: false },
]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const selected = ref<Row | null>(null)
const intervals = ref<Row[]>([])
const creating = ref(false)
const draft = reactive<Record<string, string>>({})
const filters = reactive({ keyword: '', status: '' })
const pagination = reactive({ page: 1, size: 20 })

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pagination.size)))
const stats = computed(() => [
  { label: "运行记录总数", value: total.value },
  { label: "运行中记录", value: rows.value.filter((row) => row["执行状态"] === "运行中").length },
  { label: "重跑区间数", value: rows.value.filter((row) => Boolean(row["重跑区间编号"])).length },
])

function cellValue(row: Row, column: string) {
  const value = row[column]
  if (value === null || value === undefined || value === '') return '—'
  return typeof value === 'boolean' ? (value ? '是' : '否') : value
}

/** 运行实例的动作受重跑状态约束：终态重跑的记录不再提供启动/终止。 */
function actionsFor(row: Row): string[] {
  const rerunStatus = String(row["重跑状态"] ?? '')
  if (["已完成", "已取消"].includes(rerunStatus)) return []
  return actions
}

function toggleCreate() {
  creating.value = !creating.value
}

function resetFilters() {
  filters.keyword = ''
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
      throw new Error('运行记录详情读取失败')
    }
    selected.value = await response.json()
    const recordNo = String(row["记录编号"] ?? '')
    const intervalResponse = await request(`${ENDPOINT}/${recordNo}/intervals`)
    if (intervalResponse.ok) {
      const payload = await intervalResponse.json()
      intervals.value = payload.items ?? []
    } else {
      intervals.value = []
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '运行记录详情读取失败'
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
      throw new Error(payload.message || '运行记录登记失败')
    }
    creating.value = false
    for (const field of createFields) draft[field.key] = ''
    pagination.page = 1
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '运行记录登记失败'
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
      throw new Error(payload.message || '运行实例动作未生效，请稍后重试')
    }
    if (selected.value?.id === row.id) selected.value = payload.entry ?? null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '运行实例操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams()
  if (filters.keyword.trim()) params.set('keyword', filters.keyword.trim())
  if (filters.status) params.set('status', filters.status)
  params.set('page', String(pagination.page))
  params.set('size', String(pagination.size))
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    if (!response.ok) {
      throw new Error('运行记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    if (pagination.page > pageCount.value) {
      pagination.page = pageCount.value
      await reload()
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '运行实例列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.create-panel { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 12px; }
.create-actions { display: flex; gap: 8px; }
.detail-panel { margin-top: 12px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.detail-head { display: flex; justify-content: space-between; margin-bottom: 8px; }
.detail-grid { display: grid; grid-template-columns: 140px 1fr; gap: 6px 12px; margin: 0 0 12px; font-size: 13px; }
.detail-grid dt { color: var(--muted); }
.detail-grid dd { margin: 0; }
.interval-block { font-size: 13px; }
.row-active { background: #f0f6ff; }
.pager { display: inline-flex; gap: 8px; }
.pager .btn:disabled { opacity: 0.5; cursor: not-allowed; }
</style>
