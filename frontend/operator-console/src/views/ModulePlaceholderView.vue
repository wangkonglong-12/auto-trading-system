<script setup lang="ts">
defineProps<{
  title: string;
  description: string;
  delivery: string;
}>();
</script>

<template>
  <section class="module-placeholder">
    <div class="module-placeholder__heading">
      <span class="module-placeholder__index">{{ delivery }}</span>
      <div>
        <p class="eyebrow">LOCAL OPERATIONS</p>
        <h1>{{ title }}</h1>
        <p>{{ description }}</p>
      </div>
    </div>
    <div class="module-placeholder__canvas">
      <div class="blueprint-lines" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
      <strong>页面路由与布局边界已就绪</strong>
      <p>数据查询、状态刷新与受控操作将在对应工作包接入。</p>
    </div>
  </section>
</template>
