/**
 * 路由表（T04 六页面：项目看板 / 运行详情 / 产物库 / 审批中心 / 评测报告 / 设置）。
 */

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import ApprovalCenter from '@/views/ApprovalCenter.vue'
import ArtifactLibrary from '@/views/ArtifactLibrary.vue'
import EvalReport from '@/views/EvalReport.vue'
import ProjectBoard from '@/views/ProjectBoard.vue'
import RunDetail from '@/views/RunDetail.vue'
import Settings from '@/views/Settings.vue'

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'projects',
    component: ProjectBoard,
    meta: { title: '项目看板' },
  },
  {
    path: '/runs/:id',
    name: 'run-detail',
    component: RunDetail,
    meta: { title: '运行详情' },
  },
  {
    path: '/artifacts',
    name: 'artifacts',
    component: ArtifactLibrary,
    meta: { title: '产物库' },
  },
  {
    path: '/approvals',
    name: 'approvals',
    component: ApprovalCenter,
    meta: { title: '审批中心' },
  },
  {
    path: '/evaluation',
    name: 'evaluation',
    component: EvalReport,
    meta: { title: '评测报告' },
  },
  {
    path: '/settings',
    name: 'settings',
    component: Settings,
    meta: { title: '设置' },
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.afterEach((to) => {
  const title = to.meta?.title
  document.title = typeof title === 'string' ? `${title} · ymir-agent` : 'ymir-agent'
})

export default router
