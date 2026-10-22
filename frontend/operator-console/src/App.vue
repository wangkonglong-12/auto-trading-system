<script setup lang="ts">
import { darkTheme, dateZhCN, NConfigProvider, NMessageProvider, zhCN } from "naive-ui";

import AppShell from "./components/AppShell.vue";
</script>

<template>
  <NConfigProvider :theme="darkTheme" :locale="zhCN" :date-locale="dateZhCN">
    <NMessageProvider>
      <AppShell />
    </NMessageProvider>
  </NConfigProvider>
</template>
