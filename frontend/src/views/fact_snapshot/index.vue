<template>
  <section class="page" data-module="fact_snapshot">
    <header class="page-head">
      <div>
        <h2>事实快照加工</h2>
        <p class="page-desc">
          接收带订单号、行号、金额的事实记录，按行粒度归并修订并关联维度，
          生成带来源的事实表快照与待核对记录；同刻修订冲突时暂留原确认行，不混取字段。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="reloadAll">刷新数据</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">已发布快照</span>
        <strong class="stat-value">{{ snapshots.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已接收事实记录</span>
        <strong class="stat-value">{{ facts.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">维表记录</span>
        <strong class="stat-value">{{ dimensions.length }}</strong>
      </article>
      <article class="stat-card" v-if="olap">
        <span class="stat-label">当前快照总体金额（含待核对）</span>
        <strong class="stat-value">{{ olap.总体金额 }}</strong>
      </article>
    </div>

    <div class="workbench-grid">
      <!-- 事实记录接收 -->
      <article class="panel">
        <h3>接收事实记录</h3>
        <form class="modal-body" @submit.prevent="submitFact">
          <label class="modal-field"><span>订单号</span><input v-model="factForm.订单号" placeholder="如 SO-1" required /></label>
          <label class="modal-field"><span>行号</span><input v-model="factForm.行号" placeholder="如 L1" required /></label>
          <label class="modal-field"><span>金额</span><input v-model="factForm.金额" type="number" step="0.01" required /></label>
          <label class="modal-field"><span>客户键</span><input v-model="factForm.客户键" placeholder="如 C-OLD" /></label>
          <label class="modal-field">
            <span>记录类型</span>
            <select v-model="factForm.记录类型">
              <option value="确认">确认</option>
              <option value="修订">修订</option>
            </select>
          </label>
          <label class="modal-field"><span>来源版本</span><input v-model="factForm.来源版本" placeholder="如 V2，唯一不可重复" required /></label>
          <label class="modal-field"><span>业务生效时刻</span><input v-model="factForm.业务生效时刻" placeholder="如 2026-11-20T10:00" /></label>
          <footer class="modal-foot">
            <button class="btn primary" type="submit">接收事实记录</button>
          </footer>
        </form>
      </article>

      <!-- 维表登记 -->
      <article class="panel">
        <h3>登记维度记录</h3>
        <form class="modal-body" @submit.prevent="submitDimension">
          <label class="modal-field">
            <span>维度类型</span>
            <select v-model="dimForm.维度类型">
              <option value="客户">客户</option>
              <option value="地区">地区</option>
            </select>
          </label>
          <label class="modal-field"><span>维度键</span><input v-model="dimForm.维度键" placeholder="如 C-OLD / R-EAST" required /></label>
          <label class="modal-field"><span>维度名称</span><input v-model="dimForm.维度名称" placeholder="客户名或地区名" required /></label>
          <template v-if="dimForm.维度类型 === '客户'">
            <label class="modal-field"><span>所属地区键</span><input v-model="dimForm.所属地区键" placeholder="如 R-EAST" /></label>
            <label class="modal-field"><span>所属地区名称</span><input v-model="dimForm.所属地区名称" placeholder="如 华东" /></label>
          </template>
          <footer class="modal-foot">
            <button class="btn primary" type="submit">登记维表</button>
          </footer>
        </form>
      </article>

      <!-- 快照发布 -->
      <article class="panel">
        <h3>发布事实表快照</h3>
        <form class="modal-body" @submit.prevent="publishSnapshot">
          <label class="modal-field"><span>集市表id</span><input v-model.number="snapshotForm.集市表id" type="number" required /></label>
          <label class="modal-field">
            <span>事实粒度（行粒度，逗号分隔）</span>
            <input v-model="snapshotForm.事实粒度文本" placeholder="订单号,行号" required />
          </label>
          <label class="modal-field"><span>集市表名称</span><input v-model="snapshotForm.集市表名称" placeholder="如 订单行事实表" /></label>
          <footer class="modal-foot">
            <button class="btn primary" type="submit">发布快照（旧版保留）</button>
          </footer>
        </form>
      </article>
    </div>

    <p v-if="message" class="error-text">{{ message }}</p>

    <!-- 快照版本 -->
    <h3>快照版本（新版发布后旧快照仍可打开复现）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>快照编号</th><th>集市表名称</th><th>事实粒度</th><th>状态</th>
          <th>事实行数</th><th>正式金额合计</th><th>待核对条数</th><th>待核对金额合计</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="snap in snapshots" :key="snap.id">
          <td>{{ snap.快照编号 }}</td>
          <td>{{ snap.集市表名称 }}</td>
          <td>{{ snap.事实粒度.join(' + ') }}</td>
          <td>{{ snap.状态 }}</td>
          <td>{{ snap.事实行数 }}</td>
          <td>{{ snap.正式金额合计 }}</td>
          <td>{{ snap.待核对条数 }}</td>
          <td>{{ snap.待核对金额合计 }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openSnapshot(snap.id)">打开快照</button>
            <button class="link" type="button" @click="loadOlap(snap.id)">地区汇总</button>
          </td>
        </tr>
        <tr v-if="!snapshots.length">
          <td colspan="9" class="empty-state">尚未发布事实快照</td>
        </tr>
      </tbody>
    </table>

    <template v-if="currentSnapshot">
      <h3>快照 {{ currentSnapshot.快照编号 }} · 正式事实行（一行一金额，带来源版本）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>订单号</th><th>行号</th><th>金额</th><th>客户键</th><th>客户名称</th>
            <th>地区名称</th><th>采用记录类型</th><th>来源版本</th><th>业务生效时刻</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in currentSnapshot.事实行" :key="`${row.订单号}-${row.行号}`">
            <td>{{ row.订单号 }}</td><td>{{ row.行号 }}</td><td>{{ row.金额 }}</td>
            <td>{{ row.客户键 }}</td><td>{{ row.客户名称 }}</td><td>{{ row.地区名称 }}</td>
            <td>{{ row.采用记录类型 }}</td><td>{{ row.来源版本 }}</td><td>{{ row.业务生效时刻 }}</td>
          </tr>
          <tr v-if="!currentSnapshot.事实行.length">
            <td colspan="9" class="empty-state">该快照没有正式事实行</td>
          </tr>
        </tbody>
      </table>

      <h3>待核对记录（金额保留在总体内，不悄悄退出）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>核对原因</th><th>订单号</th><th>行号</th><th>金额</th><th>客户键</th>
            <th>候选关联</th><th>来源版本</th><th>业务生效时刻</th><th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, idx) in currentSnapshot.待核对记录" :key="`${row.来源版本}-${idx}`">
            <td>{{ row.核对原因 }}</td>
            <td>{{ row.订单号 }}</td><td>{{ row.行号 }}</td><td>{{ row.金额 }}</td>
            <td>{{ row.客户键 }}</td>
            <td>
              <span v-if="!row.候选关联.length">—</span>
              <span v-else>
                <span v-for="c in row.候选关联" :key="c.维度id" class="candidate-chip">
                  {{ c.维度名称 }}（{{ c.所属地区名称 }}）
                </span>
              </span>
            </td>
            <td>{{ row.来源版本 }}</td><td>{{ row.业务生效时刻 }}</td><td>{{ row.说明 || '—' }}</td>
          </tr>
          <tr v-if="!currentSnapshot.待核对记录.length">
            <td colspan="9" class="empty-state">该快照没有待核对记录</td>
          </tr>
        </tbody>
      </table>
    </template>

    <template v-if="olap">
      <h3>OLAP 地区汇总 · {{ olap.快照编号 }}（{{ olap.口径说明 }}）</h3>
      <table class="data-table">
        <thead>
          <tr><th>地区名称</th><th>正式金额</th><th>待核对金额</th><th>总体金额</th></tr>
        </thead>
        <tbody>
          <tr v-for="region in olap.地区汇总" :key="region.地区名称">
            <td>{{ region.地区名称 }}</td>
            <td>{{ region.正式金额 }}</td>
            <td>{{ region.待核对金额 }}</td>
            <td>{{ region.总体金额 }}</td>
          </tr>
          <tr>
            <td><strong>合计</strong></td>
            <td><strong>{{ olap.正式合计 }}</strong></td>
            <td><strong>{{ olap.待核对总额 }}</strong></td>
            <td><strong>{{ olap.总体金额 }}</strong></td>
          </tr>
        </tbody>
      </table>
    </template>

    <footer class="page-foot">
      <span v-if="message" class="error-text">{{ message }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

const ENDPOINT = '/api/data_mart/fact'

type FactForm = Record<string, string | number>
type DimForm = Record<string, string>
type SnapshotForm = Record<string, string | number>
type Row = Record<string, any>

const facts = ref<Row[]>([])
const dimensions = ref<Row[]>([])
const snapshots = ref<Row[]>([])
const currentSnapshot = ref<Row | null>(null)
const olap = ref<Row | null>(null)
const message = ref('')

const factForm = ref<FactForm>({
  订单号: '', 行号: '', 金额: '', 客户键: '', 记录类型: '确认',
  来源版本: '', 业务生效时刻: '',
})
const dimForm = ref<DimForm>({
  维度类型: '客户', 维度键: '', 维度名称: '', 所属地区键: '', 所属地区名称: '',
})
const snapshotForm = ref<SnapshotForm>({
  集市表id: 3, 事实粒度文本: '订单号,行号', 集市表名称: '订单行事实表',
})

async function postJson(path: string, values: Record<string, any>): Promise<boolean> {
  message.value = ''
  try {
    const response = await request(path, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const data = await response.json()
    if (!data.ok) {
      message.value = data.message || '操作未生效'
      return false
    }
    message.value = data.message
    return true
  } catch (error) {
    message.value = error instanceof Error ? error.message : '接口请求失败'
    return false
  }
}

async function submitFact() {
  const ok = await postJson(`${ENDPOINT}/records`, { ...factForm.value })
  if (ok) {
    factForm.value = {
      订单号: '', 行号: '', 金额: '', 客户键: '', 记录类型: '确认',
      来源版本: '', 业务生效时刻: '',
    }
    await reloadAll()
  }
}

async function submitDimension() {
  const ok = await postJson(`${ENDPOINT}/dimensions`, { ...dimForm.value })
  if (ok) {
    dimForm.value = { 维度类型: dimForm.value.维度类型, 维度键: '', 维度名称: '', 所属地区键: '', 所属地区名称: '' }
    await reloadAll()
  }
}

async function publishSnapshot() {
  const grain = String(snapshotForm.value.事实粒度文本)
    .split(/[,，、]/).map((item) => item.trim()).filter(Boolean)
  const ok = await postJson(`${ENDPOINT}/snapshots`, {
    集市表id: Number(snapshotForm.value.集市表id),
    事实粒度: grain,
    集市表名称: snapshotForm.value.集市表名称,
  })
  if (ok) await reloadAll()
}

async function openSnapshot(id: number) {
  try {
    const response = await request(`${ENDPOINT}/snapshots/${id}`)
    currentSnapshot.value = await response.json()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '快照读取失败'
  }
}

async function loadOlap(id: number) {
  try {
    const response = await request(`${ENDPOINT}/snapshots/${id}/olap`)
    olap.value = await response.json()
    await openSnapshot(id)
  } catch (error) {
    message.value = error instanceof Error ? error.message : 'OLAP 汇总读取失败'
  }
}

async function reloadAll() {
  try {
    const [factRes, dimRes, snapRes] = await Promise.all([
      request(`${ENDPOINT}/records`),
      request(`${ENDPOINT}/dimensions`),
      request(`${ENDPOINT}/snapshots`),
    ])
    facts.value = (await factRes.json()).items ?? []
    dimensions.value = (await dimRes.json()).items ?? []
    snapshots.value = (await snapRes.json()).items ?? []
  } catch (error) {
    message.value = error instanceof Error ? error.message : '数据加载失败'
  }
}

onMounted(reloadAll)
</script>

<style scoped>
.workbench-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 16px;
  margin: 16px 0;
}

.panel {
  border: 1px solid var(--border-color, #d9dee7);
  border-radius: 10px;
  padding: 14px 16px;
  background: #fff;
}

.panel h3 {
  margin: 0 0 10px;
  font-size: 15px;
}

.candidate-chip {
  display: inline-block;
  margin: 2px 4px 2px 0;
  padding: 1px 8px;
  border-radius: 999px;
  background: #eef3ff;
  font-size: 12px;
}
</style>
