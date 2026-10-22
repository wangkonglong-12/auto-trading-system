export interface NavigationItem {
  readonly path: string;
  readonly label: string;
  readonly mark: string;
  readonly description: string;
  readonly delivery: string;
}

export const NAVIGATION_ITEMS: readonly NavigationItem[] = Object.freeze([
  {
    path: "/overview",
    label: "运行总览",
    mark: "总",
    description: "节点连接、交易模式、资产仓位、订单、告警和同步状态",
    delivery: "UI-002",
  },
  {
    path: "/plans",
    label: "当日计划",
    mark: "计",
    description: "冻结计划、验签、风险准入和执行进度",
    delivery: "UI-002",
  },
  {
    path: "/orders",
    label: "委托与成交",
    mark: "单",
    description: "本地意图、QMT委托、成交和不确定状态",
    delivery: "UI-002",
  },
  {
    path: "/portfolio",
    label: "持仓与资产",
    mark: "仓",
    description: "券商资产、持仓数量、成本和行情新鲜度",
    delivery: "UI-002",
  },
  {
    path: "/execution",
    label: "买卖状态",
    mark: "执",
    description: "入场阶段、退出策略、容量和不可执行原因",
    delivery: "UI-003",
  },
  {
    path: "/risk",
    label: "风控与控制",
    mark: "控",
    description: "Hard Safety、运行权限、本地限制和健康阻断项",
    delivery: "UI-003",
  },
  {
    path: "/reconciliation",
    label: "对账中心",
    mark: "对",
    description: "本地、QMT和中央的订单、成交、持仓及资产差异",
    delivery: "UI-004",
  },
  {
    path: "/incidents",
    label: "告警与事件",
    mark: "警",
    description: "关键事件时间线、故障原因和操作审计",
    delivery: "UI-003",
  },
  {
    path: "/diagnostics",
    label: "系统诊断",
    mark: "诊",
    description: "版本、队列、Writer、WAL、磁盘、时钟和故障包",
    delivery: "UI-004",
  },
  {
    path: "/settings",
    label: "设置与版本",
    mark: "设",
    description: "只读运行配置、配置哈希和兼容矩阵",
    delivery: "UI-004",
  },
]);
