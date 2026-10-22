<script setup lang="ts">
import EmptyTrendChart from "../components/EmptyTrendChart.vue";

const STATUS_CARDS = Object.freeze([
  { label: "总资产", value: "—", note: "等待券商资产快照" },
  { label: "当前仓位", value: "—", note: "等待持仓对账" },
  { label: "活动委托", value: "—", note: "等待QMT查询" },
  { label: "Outbox积压", value: "—", note: "等待Journal状态" },
]);

const READINESS_ITEMS = Object.freeze([
  { label: "单实例账户锁", status: "等待Runtime" },
  { label: "LocalJournal / Writer", status: "等待Runtime" },
  { label: "QMT交易会话", status: "等待Runtime" },
  { label: "计划与命令同步", status: "等待Runtime" },
]);
</script>

<template>
  <section class="page-heading">
    <div>
      <p class="eyebrow">LOCAL OPERATIONS</p>
      <h1>运行总览</h1>
      <p>以本机Runtime和QMT事实为准，集中查看交易权限、连接、资产、委托与安全状态。</p>
    </div>
    <div class="freshness-panel">
      <span>状态快照</span>
      <strong>尚未接收</strong>
      <small>每秒轮询将在UI-002接入</small>
    </div>
  </section>

  <section class="metric-grid" aria-label="关键运行指标">
    <article v-for="card in STATUS_CARDS" :key="card.label" class="metric-card">
      <span>{{ card.label }}</span>
      <strong>{{ card.value }}</strong>
      <small>{{ card.note }}</small>
    </article>
  </section>

  <section class="dashboard-grid">
    <article class="panel panel--wide">
      <header class="panel__header">
        <div>
          <span class="panel__kicker">ASSET TRACE</span>
          <h2>日内资产与仓位</h2>
        </div>
        <span class="data-state">无快照</span>
      </header>
      <EmptyTrendChart />
    </article>

    <article class="panel">
      <header class="panel__header">
        <div>
          <span class="panel__kicker">READINESS</span>
          <h2>启动就绪检查</h2>
        </div>
      </header>
      <ul class="readiness-list">
        <li v-for="item in READINESS_ITEMS" :key="item.label">
          <span class="readiness-list__dot" aria-hidden="true" />
          <span>{{ item.label }}</span>
          <strong>{{ item.status }}</strong>
        </li>
      </ul>
    </article>

    <article class="panel panel--wide">
      <header class="panel__header">
        <div>
          <span class="panel__kicker">TODAY'S PLAN</span>
          <h2>当日计划执行面板</h2>
        </div>
        <span class="data-state">未加载</span>
      </header>
      <div class="empty-table" role="status">
        <span class="empty-table__line" />
        <strong>等待已验签的TradePlan</strong>
        <p>计划标的、目标金额、风险准入和执行阶段将在此处显示。</p>
      </div>
    </article>

    <article class="panel">
      <header class="panel__header">
        <div>
          <span class="panel__kicker">INCIDENTS</span>
          <h2>当前告警</h2>
        </div>
        <span class="data-state">未知</span>
      </header>
      <div class="empty-state compact">
        <strong>等待事件流</strong>
        <p>关键故障、降级原因和本地控制结果将按时间排序。</p>
      </div>
    </article>
  </section>
</template>
