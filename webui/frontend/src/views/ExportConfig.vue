<script setup>
import { computed, onActivated, onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getExportConfig, saveExportConfig, testExport } from '@/api/settings'
import FooterToolbar from '@/components/FooterToolbar.vue'

const pool = reactive({ enabled: false, url: '', key: '', keyPh: '粘贴账号池后台管理密钥', timeout: 40 })
const cpa = reactive({ enabled: false, url: '', key: '', keyPh: '粘贴 CPA 管理密钥', timeout: 30 })
const sub = reactive({ enabled: false, url: '', key: '', keyPh: '粘贴面板里生成的 x-api-key', groupIds: '2', timeout: 30 })
const saving = ref(false)
const testingPool = ref(false)
const testingCpa = ref(false)
const testingSub = ref(false)
const loaded = ref(false)
const savedConnections = reactive({ pool: null, cpa: null, sub2api: null })
const testResults = reactive(Object.fromEntries(['pool', 'cpa', 'sub2api'].map(target => [target, {
  phase: 'idle', message: '', elapsed: 0,
}])))
const timers = new Map()
const connectionForms = { pool, cpa, sub2api: sub }
const testingFlags = { pool: testingPool, cpa: testingCpa, sub2api: testingSub }

function connectionSnapshot(target) {
  const form = connectionForms[target]
  // Enabling automatic export does not change the read-only connection test.
  return JSON.stringify([form.url.trim(), form.key.trim(), Number(form.timeout)])
}

const connectionChanged = computed(() => Object.fromEntries(
  Object.keys(connectionForms).map(target => [target,
    loaded.value && savedConnections[target] !== connectionSnapshot(target),
  ]),
))

function feedbackTitle(target) {
  const result = testResults[target]
  const elapsed = result.elapsed.toFixed(1)
  const label = { running: '正在测试', success: '连接测试成功', error: '连接测试失败', warning: '请先保存配置' }[result.phase]
  return result.phase === 'warning' ? label : `${label} · ${elapsed} 秒`
}

function feedbackType(target) {
  return { running: 'info', success: 'success', error: 'error', warning: 'warning' }[testResults[target].phase] || 'info'
}

// Only show known safe messages, never an upstream response body or credentials.
const safePoolFailures = new Set([
  '号池配置无效，请检查接口地址、管理员密钥和超时时间。',
  '号池管理员密钥无效或已失效。',
  '号池接口拒绝访问，请检查管理员权限。',
  '后台拒绝本次操作；账号可能处于诊断隔离中，请在后台检查。',
  '号池接口返回重定向，已拒绝转发凭据；请填写最终接口地址。',
  '号池接口拒绝请求，请检查接口配置。',
  '号池接口暂不可用，未执行账号推送。',
  '后台响应过大，无法确认接口结果。',
  '号池 HTTPS 证书校验失败，未忽略证书验证。',
  '连接检查超时，未执行账号推送。',
  '号池连接检查失败，未执行账号推送。',
  '号池请求处理失败，请检查接口配置和服务状态。',
  '后台返回格式不符合号池列表接口，无法确认连接配置。',
])

function safeTestMessage(target, value, success) {
  const message = typeof value?.message === 'string' ? value.message : ''
  if (success) {
    if (target === 'pool') return '账号池连接和管理员鉴权正常，未推送任何账号。'
    if (target === 'cpa' && /HTTP 405/.test(message)) return 'CPA 地址可达，但接口不支持 GET，暂时无法确认管理密钥有效。'
    return `${target === 'cpa' ? 'CPA' : 'SUB2API'} 连接和密钥验证正常。`
  }
  if (target === 'pool' && safePoolFailures.has(message)) return message
  if (/timeout|timed out|超时/i.test(message)) return '连接检查超时，请检查网络和后台状态后重试。'
  if (/Network Error|Failed to fetch|连接失败|连接中断/i.test(message)) return '无法连接服务，请检查网络和本地服务是否仍在运行。'
  if (/鉴权失败|管理密钥错误|HTTP (401|403)/i.test(message)) return '后台拒绝访问，请检查已保存的管理密钥和权限。'
  if (/未配置/.test(message)) return '连接配置不完整，请填写接口地址和管理密钥，保存后再测试。'
  const status = message.match(/HTTP ([1-5]\d{2})/i)?.[1]
  return status ? `后台返回 HTTP ${status}，请检查接口地址和后台状态。` : '连接测试失败，请检查已保存的配置、网络和后台状态。'
}

async function load() {
  try {
    const { config } = await getExportConfig()
    pool.enabled = config.pool_enabled === '1'
    pool.url = config.pool_url || ''
    pool.key = ''
    pool.keyPh = config.pool_admin_key === '***' ? '已设置（留空不修改）' : '粘贴账号池后台管理密钥'
    pool.timeout = Number(config.pool_timeout || 40)
    cpa.enabled = config.cpa_enabled === '1'
    cpa.url = config.cpa_url || ''
    cpa.key = ''
    cpa.keyPh = config.cpa_mgmt_key === '***' ? '已设置（留空不修改）' : '粘贴 CPA 管理密钥'
    cpa.timeout = Number(config.cpa_timeout || 30)
    sub.enabled = config.sub2api_enabled === '1'
    sub.url = config.sub2api_url || ''
    sub.key = ''
    sub.keyPh = config.sub2api_api_key === '***' ? '已设置（留空不修改）' : '粘贴面板里生成的 x-api-key'
    sub.groupIds = config.sub2api_group_ids || '2'
    sub.timeout = Number(config.sub2api_timeout || 30)
    for (const target of Object.keys(connectionForms)) savedConnections[target] = connectionSnapshot(target)
    loaded.value = true
  } catch (e) { ElMessage.error(e.message) }
}

async function save() {
  if (saving.value) return
  saving.value = true
  try {
    await saveExportConfig({
      pool_enabled: pool.enabled ? '1' : '0',
      pool_url: pool.url.trim(),
      pool_admin_key: pool.key.trim() || '***',
      pool_timeout: String(pool.timeout || 40),
      cpa_enabled: cpa.enabled ? '1' : '0',
      cpa_url: cpa.url.trim(),
      cpa_mgmt_key: cpa.key.trim() || '***',
      cpa_timeout: String(cpa.timeout || 30),
      sub2api_enabled: sub.enabled ? '1' : '0',
      sub2api_url: sub.url.trim(),
      sub2api_api_key: sub.key.trim() || '***',
      sub2api_group_ids: sub.groupIds.trim() || '2',
      sub2api_timeout: String(sub.timeout || 30),
    })
    ElMessage.success('保存成功')
    await load()
  } catch (e) { ElMessage.error(e.message) }
  finally { saving.value = false }
}

async function test(target) {
  const flag = testingFlags[target]
  if (!flag || flag.value || saving.value) return
  const result = testResults[target]
  if (!loaded.value || connectionChanged.value[target]) {
    Object.assign(result, {
      phase: 'warning', elapsed: 0,
      message: loaded.value ? '接口地址、管理密钥或超时已修改。请先点击底部「保存配置」，再测试已保存的配置。' : '配置尚未加载，请稍后再试。',
    })
    return
  }
  const started = Date.now()
  const updateElapsed = () => { result.elapsed = Math.max(0, (Date.now() - started) / 1000) }
  flag.value = true
  Object.assign(result, { phase: 'running', elapsed: 0, message: '正在检查已保存的接口配置，请稍候。连接测试不会上传账号。' })
  timers.set(target, setInterval(updateElapsed, 250))
  try {
    const response = await testExport(target)
    const success = response?.ok === true
    Object.assign(result, { phase: success ? 'success' : 'error', message: safeTestMessage(target, response, success) })
  } catch (error) {
    Object.assign(result, { phase: 'error', message: safeTestMessage(target, error, false) })
  } finally {
    updateElapsed()
    clearInterval(timers.get(target))
    timers.delete(target)
    flag.value = false
  }
}

onUnmounted(() => {
  for (const timer of timers.values()) clearInterval(timer)
  timers.clear()
})

onActivated(() => load())
</script>

<template>
  <div class="page">
    <el-card shadow="never" style="max-width: 760px">
      <template #header>
        <span class="section-title" style="margin: 0">后台推送与导出</span>
      </template>
      <p class="hint">启用并保存后，注册成功的账号会自动推送到对应后台；推送失败仍保留本地注册结果。</p>

      <el-form label-position="top">
        <el-divider content-position="left">账号池后台</el-divider>
        <p class="hint">使用 accounts 格式推送 AT，并携带已保存的邮箱、注册设备 ID、ST、完整 Cookie、CSRF、账号密码、2FA 长期密钥和 factor ID；缺失字段省略，密码保留原样。</p>
        <p class="hint">注册日志使用完整快照 events 中的结构化 HTTP 事件，以 registration_log 发送；不包含终端日志。历史账号没有这些事件时省略该字段，不用空数组清除后台日志。本地完整 JSON 的保存与导出保持不变。</p>
        <p class="hint">AT 必填，不发送 RT 或 ID Token；注册默认仍不获取 RT、开启 2FA。新增密码、2FA 和日志字段需要接收端更新后才能保存，请先确认后台已部署对应版本；连接测试通过不代表新增字段已入库。</p>
        <el-form-item>
          <el-switch v-model="pool.enabled" aria-label="启用账号池自动推送" />
          <span style="margin-left: 10px">注册成功后自动推送到账号池</span>
        </el-form-item>
        <el-form-item label="账号池接口 URL（完整 /api/accounts 地址）">
          <el-input v-model="pool.url" placeholder="https://pool.example.com/api/accounts" />
        </el-form-item>
        <el-form-item label="后台管理密钥">
          <el-input v-model="pool.key" type="password" show-password :placeholder="pool.keyPh" />
        </el-form-item>
        <el-form-item label="超时 (秒)">
          <el-input-number v-model="pool.timeout" :min="5" :max="120" />
        </el-form-item>
        <el-button :loading="testingPool" :disabled="saving || !loaded" @click="test('pool')">
          {{ testingPool ? `正在测试 · ${testResults.pool.elapsed.toFixed(1)} 秒` : '测试账号池连接' }}
        </el-button>
        <el-alert v-if="connectionChanged.pool" title="连接配置有未保存的修改，请先点击底部「保存配置」。" type="warning" :closable="false" show-icon style="margin-top: 12px" />
        <el-alert v-if="testResults.pool.phase !== 'idle'" :title="feedbackTitle('pool')" :description="testResults.pool.message" :type="feedbackType('pool')" :closable="false" show-icon style="margin-top: 12px" aria-live="polite" />
        <p class="hint">先保存配置再测试。连接测试只读取后台状态，不上传账号。关闭自动推送后，仍可在「注册结果」页手动推送选中账号。</p>

        <el-divider content-position="left">CPA 面板</el-divider>
        <p class="hint">CPA 导出需要账号包含 RT；上方账号池推送无需 RT。</p>
        <el-form-item>
          <el-checkbox v-model="cpa.enabled">启用 CPA 自动导出（POST /v0/management/auth-files）</el-checkbox>
        </el-form-item>
        <el-form-item label="CPA URL">
          <el-input v-model="cpa.url" placeholder="https://cpa.example.com" />
        </el-form-item>
        <el-form-item label="管理密钥（Authorization Bearer + X-Management-Key）">
          <el-input v-model="cpa.key" type="password" show-password :placeholder="cpa.keyPh" />
        </el-form-item>
        <el-form-item label="超时 (秒)">
          <el-input-number v-model="cpa.timeout" :min="5" :max="300" />
        </el-form-item>
        <el-button :loading="testingCpa" :disabled="saving || !loaded" @click="test('cpa')">
          {{ testingCpa ? `正在测试 · ${testResults.cpa.elapsed.toFixed(1)} 秒` : '测试 CPA 连通性' }}
        </el-button>
        <el-alert v-if="connectionChanged.cpa" title="连接配置有未保存的修改，请先保存后测试。" type="warning" :closable="false" show-icon style="margin-top: 12px" />
        <el-alert v-if="testResults.cpa.phase !== 'idle'" :title="feedbackTitle('cpa')" :description="testResults.cpa.message" :type="feedbackType('cpa')" :closable="false" show-icon style="margin-top: 12px" aria-live="polite" />

        <el-divider content-position="left">SUB2API 面板</el-divider>
        <p class="hint">SUB2API 导出需要账号包含 RT；上方账号池推送无需 RT。</p>
        <el-form-item>
          <el-checkbox v-model="sub.enabled">启用 SUB2API 自动导出（POST /api/v1/admin/accounts）</el-checkbox>
        </el-form-item>
        <el-form-item label="SUB2API URL">
          <el-input v-model="sub.url" placeholder="https://sub2api.example.com" />
        </el-form-item>
        <el-form-item label="API Key（安全与认证-管理员 API Key）">
          <el-input v-model="sub.key" type="password" show-password :placeholder="sub.keyPh" />
        </el-form-item>
        <el-form-item label="分组 IDs（逗号分隔，如 2 或 1,2,3）">
          <el-input v-model="sub.groupIds" placeholder="2" />
        </el-form-item>
        <el-form-item label="超时 (秒)">
          <el-input-number v-model="sub.timeout" :min="5" :max="300" />
        </el-form-item>
        <el-button :loading="testingSub" :disabled="saving || !loaded" @click="test('sub2api')">
          {{ testingSub ? `正在测试 · ${testResults.sub2api.elapsed.toFixed(1)} 秒` : '测试 SUB2API 连通性' }}
        </el-button>
        <el-alert v-if="connectionChanged.sub2api" title="连接配置有未保存的修改，请先保存后测试。" type="warning" :closable="false" show-icon style="margin-top: 12px" />
        <el-alert v-if="testResults.sub2api.phase !== 'idle'" :title="feedbackTitle('sub2api')" :description="testResults.sub2api.message" :type="feedbackType('sub2api')" :closable="false" show-icon style="margin-top: 12px" aria-live="polite" />

      </el-form>
    </el-card>

    <FooterToolbar>
      <template #left>
        账号池 {{ pool.enabled ? '已启用' : '未启用' }} · CPA {{ cpa.enabled ? '已启用' : '未启用' }} · SUB2API {{ sub.enabled ? '已启用' : '未启用' }}
      </template>
      <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
    </FooterToolbar>
  </div>
</template>
