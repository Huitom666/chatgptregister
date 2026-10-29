import { defineStore } from 'pinia'
import { reactive, watch } from 'vue'

const KEY = 'gpt_outlook_register_form_v2'

// 跨页面共享 + localStorage 持久化的表单字段
// （proxy 在 注册 / 自动跑号 / Plus 检测 三处共用）
const defaults = {
  proxy: '',
  otpTimeout: 10,
  autoConcurrency: 1,
  autoCoolDownMinutes: 3 / 60,
  autoTargetCount: 0,
  // 单次和自动注册分别保存选项；AT、ST 固定获取。
  wantRefreshToken: false,
  autoWantRefreshToken: false,
  want2fa: true,
  autoWant2fa: true,
}

export function restoreForm(saved) {
  const values = saved && typeof saved === 'object' && !Array.isArray(saved) ? saved : {}
  const restored = { ...defaults, ...values }
  // 兼容旧缓存：新增 RT 选项默认关闭，已明确选择的布尔值继续保留。
  for (const key of ['wantRefreshToken', 'autoWantRefreshToken', 'want2fa', 'autoWant2fa']) {
    if (typeof restored[key] !== 'boolean') restored[key] = defaults[key]
  }
  restored.proxy = String(restored.proxy ?? '')
  // The old field stored seconds. Migrate once without changing the duration.
  const hasMinutes = Object.prototype.hasOwnProperty.call(values, 'autoCoolDownMinutes')
  const rawCooldown = hasMinutes ? values.autoCoolDownMinutes : values.autoCoolDown
  const cooldown = (typeof rawCooldown === 'number' ||
    (typeof rawCooldown === 'string' && rawCooldown.trim() !== '')) ? Number(rawCooldown) : NaN
  restored.autoCoolDownMinutes = Number.isFinite(cooldown) && cooldown >= 0
    ? cooldown / (hasMinutes ? 1 : 60)
    : defaults.autoCoolDownMinutes
  delete restored.autoCoolDown
  return restored
}

// el-select 的 clearable 清空时把值写成 **undefined**（不是 ''），而 proxy 在三个
// 页面都是 `form.value.proxy.trim()` 直接调 —— 主人点一次叉，下次提交就
// "Cannot read properties of undefined (reading 'trim')"。这里统一兜底成字符串，
// 免得每个调用点各写各的可选链，也顺手挡住 localStorage 里的历史脏值。
export function proxyText(form) {
  return String(form?.proxy ?? '').trim()
}

export const useFormStore = defineStore('form', () => {
  let saved = {}
  try { saved = JSON.parse(localStorage.getItem(KEY) || '{}') } catch (_) { saved = {} }
  const form = reactive(restoreForm(saved))

  // clearable 清空后 proxy 会变成 undefined 并被持久化进 localStorage，
  // 刷新页面后依然是 undefined。这里watch 回填成 ''，保证存量数据也是干净的。
  watch(() => form.proxy, (v) => {
    if (v === undefined || v === null) form.proxy = ''
  })

  watch(form, (v) => {
    try { localStorage.setItem(KEY, JSON.stringify(v)) } catch (_) {}
  }, { deep: true, immediate: true })

  return { form }
})
