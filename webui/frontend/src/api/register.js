import http from './request'

// ──────────────── 单个注册 ────────────────
export const startRegister = (payload) => http.post('/api/register', payload)

// ──────────────── 运行记录 ────────────────
export const listRuns = (limit = 50) => http.get('/api/runs', { params: { limit } })

// ──────────────── 注册结果 registered ────────────────
export const listRegistered = (params) =>
  http.get('/api/registered', { params }) // { limit, offset, filter }

export const getRegistered = (email) =>
  http.get(`/api/registered/${encodeURIComponent(email)}`)

// 完整注册快照按需读取；列表接口不传输原始 Cookie、事件和完整凭证。
export const getRegistrationJson = (email) =>
  http.get(`/api/registered/${encodeURIComponent(email)}/registration-json`)

export const deleteRegistered = (email) =>
  http.delete(`/api/registered/${encodeURIComponent(email)}`)

// 手填凭证：不传的字段后端不动，传空串才是清空
export const updateCredentials = (payload) =>
  http.post('/api/registered/update_credentials', payload)

export const bulkDeleteRegistered = (payload) =>
  http.post('/api/registered/bulk_delete', payload) // { emails } 或 { all: true }

// 导出后清理用：把号池那一行也删掉。
// 从 accounts.js 转出来一份，省得 Registered.vue 同时 import 两个 api 模块。
export { bulkDeleteAccounts } from './accounts'

// 批量导出：格式清单由后端 export_formats.py 提供，加格式前端不用改
export const listExportFormats = () => http.get('/api/registered/export/formats')
export const exportRegistered = (payload) => http.post('/api/registered/export', payload)

export const checkPlus = (emails, proxy = '') =>
  http.post('/api/registered/check_plus', { emails, proxy })

export const exportToPanel = (email, targets) =>
  http.post('/api/registered/export_to_panel', { email, targets })

// 后端逐个推送，每个账号最多等待配置的 120 秒；避免全局 60 秒提前超时。
export const pushRegisteredToPool = (emails) =>
  http.post('/api/registered/push-pool', { emails }, { timeout: Math.max(60000, emails.length * 120000 + 10000) })

// ──────────────── 自动跑号 auto-loop ────────────────
export const autoStart = (payload) => http.post('/api/auto/start', payload)
export const autoPause = () => http.post('/api/auto/pause')
export const autoResume = () => http.post('/api/auto/resume')
export const autoStop = () => http.post('/api/auto/stop')
export const autoStatus = () => http.get('/api/auto/status')
