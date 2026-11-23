// auth-helpers.jsx — capability / is_dev_role checks for the new permission model
// (replaces旧的 user.role === 'dev' 字符串比较，与后端 backend/auth/capabilities.py 对齐)。
//
// 加载方式：与其他 jsx 同步——通过 <script type="text/babel"> 注入，挂到 window 全局；
// 不能用 ESM `export`，因为 Babel standalone 不走模块解析。

const isDevRole = (user) => Boolean(user?.is_dev_role);

const hasCapability = (user, cap) => {
  if (!user) return false;
  if (user.is_dev_role) return true;            // dev 角色 bypass 所有 capability gate
  return Array.isArray(user.capabilities) && user.capabilities.includes(cap);
};

// Capability id 常量——与 backend/auth/capabilities.py 保持同步。
// 前端只需声明自己 check 的几个 id；dev-only 的 tokens.jsx 页面（Step 5）
// 会通过 /api/tokens/registry 拉全量 registry。
const CAP_VIEW_DEV_TASKS = 'view.dev_tasks';
const CAP_VIEW_DEV_AGENTS = 'view.dev_agents';
const CAP_VIEW_OTHERS_RUNS = 'view.others_runs';
const CAP_SUBMIT_URGENT_PRIORITY = 'submit.urgent_priority';
const CAP_MANAGE_CANCEL_ANY_RUN = 'manage.cancel_any_run';
const CAP_MANAGE_DELETE_ANY_RUN = 'manage.delete_any_run';
const CAP_ADMIN_VIEW_CLUSTER = 'admin.view_cluster';
const CAP_ADMIN_VIEW_USAGE = 'admin.view_usage';
const CAP_ADMIN_VIEW_FAILURES = 'admin.view_failures';
const CAP_ADMIN_VIEW_RUNS_BULK = 'admin.view_runs_bulk';
const CAP_ADMIN_MANAGE_GPU_POLICY = 'admin.manage_gpu_policy';
const CAP_ADMIN_MANAGE_SCHEDULER = 'admin.manage_scheduler';
const CAP_ADMIN_MANAGE_RUNS_BULK = 'admin.manage_runs_bulk';

Object.assign(window, {
  isDevRole, hasCapability,
  CAP_VIEW_DEV_TASKS, CAP_VIEW_DEV_AGENTS, CAP_VIEW_OTHERS_RUNS,
  CAP_SUBMIT_URGENT_PRIORITY,
  CAP_MANAGE_CANCEL_ANY_RUN, CAP_MANAGE_DELETE_ANY_RUN,
  CAP_ADMIN_VIEW_CLUSTER, CAP_ADMIN_VIEW_USAGE, CAP_ADMIN_VIEW_FAILURES,
  CAP_ADMIN_VIEW_RUNS_BULK, CAP_ADMIN_MANAGE_GPU_POLICY,
  CAP_ADMIN_MANAGE_SCHEDULER, CAP_ADMIN_MANAGE_RUNS_BULK,
});
