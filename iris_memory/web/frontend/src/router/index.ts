import { createRouter, createWebHashHistory, RouteRecordRaw } from 'vue-router'

// 静态导入所有视图：避免运行时 dynamic import 携带短期 asset_token，
// token 过期（默认 60s）后再切路由会 401 -> "Failed to fetch dynamically
// imported module" -> 内容区整片黑屏。静态导入后所有模块在页面初始加载
// 阶段一次性拉取完成，不再依赖后续 token 有效期。
import DashboardView from '@/views/DashboardView.vue'
import L1BufferView from '@/views/L1BufferView.vue'
import L2MemoryView from '@/views/L2MemoryView.vue'
import L3GraphView from '@/views/L3GraphView.vue'
import ProfileView from '@/views/ProfileView.vue'
import LearningView from '@/views/LearningView.vue'
import PersonaEvolutionView from '@/views/PersonaEvolutionView.vue'
import DataManageView from '@/views/DataManageView.vue'
import ReplyControlView from '@/views/ReplyControlView.vue'
import RunLogView from '@/views/RunLogView.vue'
import HiddenConfigView from '@/views/HiddenConfigView.vue'
import IgnoreListView from '@/views/IgnoreListView.vue'

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    redirect: '/dashboard'
  },
  {
    path: '/dashboard',
    name: 'Dashboard',
    component: DashboardView,
    meta: { title: '仪表盘', icon: 'mdi-view-dashboard' }
  },
  {
    path: '/l1-buffer',
    name: 'L1Buffer',
    component: L1BufferView,
    meta: { title: 'L1 缓冲', icon: 'mdi-lightning-bolt' }
  },
  {
    path: '/l2-memory',
    name: 'L2Memory',
    component: L2MemoryView,
    meta: { title: 'L2 记忆', icon: 'mdi-database-search' }
  },
  {
    path: '/l3-graph',
    name: 'L3Graph',
    component: L3GraphView,
    meta: { title: 'L3 图谱', icon: 'mdi-graph' }
  },
  {
    path: '/profile',
    name: 'Profile',
    component: ProfileView,
    meta: { title: '画像管理', icon: 'mdi-account-group' }
  },
  {
    path: '/learning',
    name: 'Learning',
    component: LearningView,
    meta: { title: '学习管理', icon: 'mdi-school' }
  },
  {
    path: '/persona-evolution',
    name: 'PersonaEvolution',
    component: PersonaEvolutionView,
    meta: { title: '人格自迭代', icon: 'mdi-account-cog' }
  },
  {
    path: '/data-manage',
    name: 'DataManage',
    component: DataManageView,
    meta: { title: '数据管理', icon: 'mdi-swap-vertical' }
  },
  {
    path: '/reply-control',
    name: 'ReplyControl',
    component: ReplyControlView,
    meta: { title: '主动回复', icon: 'mdi-robot' }
  },
  {
    path: '/run-log',
    name: 'RunLog',
    component: RunLogView,
    meta: { title: '运行日志', icon: 'mdi-text-box-search-outline' }
  },
  {
    path: '/hidden-config',
    name: 'HiddenConfig',
    component: HiddenConfigView,
    meta: { title: '隐藏参数', icon: 'mdi-cog-outline' }
  },
  {
    path: '/ignore-list',
    name: 'IgnoreList',
    component: IgnoreListView,
    meta: { title: '忽略名单', icon: 'mdi-account-cancel-outline' }
  }
]

const router = createRouter({
  history: createWebHashHistory(),
  routes
})

export default router
