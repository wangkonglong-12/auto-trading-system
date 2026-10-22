import {
  createMemoryHistory,
  createRouter,
  createWebHashHistory,
  type Router,
  type RouterHistory,
  type RouteRecordRaw,
} from "vue-router";

import { NAVIGATION_ITEMS } from "./navigation";
import OverviewView from "./views/OverviewView.vue";

const placeholderRoutes: RouteRecordRaw[] = NAVIGATION_ITEMS.filter(
  (item) => item.path !== "/overview",
).map((item) => ({
  path: item.path,
  name: item.path.slice(1),
  component: () => import("./views/ModulePlaceholderView.vue"),
  props: {
    title: item.label,
    description: item.description,
    delivery: item.delivery,
  },
  meta: {
    title: item.label,
  },
}));

export const CONSOLE_ROUTES: readonly RouteRecordRaw[] = Object.freeze([
  { path: "/", redirect: "/overview" },
  {
    path: "/overview",
    name: "overview",
    component: OverviewView,
    meta: { title: "运行总览" },
  },
  ...placeholderRoutes,
  { path: "/:pathMatch(.*)*", redirect: "/overview" },
]);

export function createConsoleRouter(history?: RouterHistory): Router {
  return createRouter({
    history: history ?? createWebHashHistory(),
    routes: [...CONSOLE_ROUTES],
    scrollBehavior: () => ({ top: 0 }),
  });
}

export function createTestRouter(): Router {
  return createConsoleRouter(createMemoryHistory());
}
