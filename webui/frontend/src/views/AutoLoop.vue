<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { ElMessage } from 'element-plus'
import { autoStart, autoPause, autoResume, autoStop } from '@/api/register'
import { useFormStore, proxyText } from '@/stores/form'
import { useProxyStore } from '@/stores/proxy'
import { useRuntimeStore } from '@/stores/runtime'
import LogPanel from '@/components/LogPanel.vue'
import StatusDot from '@/components/StatusDot.vue'

const router = useRouter()
const { form } = storeToRefs(useFormStore())
const proxyStore = useProxyStore()
const { count: proxyCount } = storeToRefs(proxyStore)
const runtime = useRuntimeStore()
const { autoStatus } = storeToRefs(runtime)

const st = computed(() => autoStatus.value.state || 'stopped')
const stopping = computed(() => !!autoStatus.value.stop_requested)
const canStart = computed(() => !stopping.value && st.value === 'stopped')
const canPause = computed(() => !stopping.value && st.value === 'running')
const canResume = computed(() => !stopping.value && st.value === 'paused')
const canStop = computed(() => !stopping.value && st.value !== 'stopped')

const stateLabel = computed(() => stopping.value ? '停止中，等待当前任务' : ({
  stopped: '未运行', running: '运行中', paused: '已暂停',
}[st.value] || st.value))
const stateType = computed(() => stopping.value ? 'warning' : ({
  stopped: 'info', running: 'success', paused: 'warning',
}[st.value] || 'info'))

const workers = computed(() => Array.isArray(autoStatus.value.workers) ? autoStatus.value.workers : [])

async function start() {
  try {
    const cooldownSeconds = Number(form.value.autoCoolDownMinutes ?? 0) * 60
    if (!Number.isFinite(cooldownSeconds) || cooldownSeconds < 0) {
      ElMessage.error('冷却时间请输入大于或等于 0 的有效分钟数')
      return
    }
    await autoStart({
      proxy: proxyText(form.value),
      proxy_pool: proxyStore.text,
      concurrency: parseInt(form.value.autoConcurrency, 10) || 1,
      otp_timeout: parseInt(form.value.otpTimeout, 10) || 10,
      want_access_token: true,
      want_session_token: true,
      want_refresh_token: form.value.autoWantRefreshToken,
      cool_down_seconds: cooldownSeconds,
      target_count: parseInt(form.value.autoTargetCount, 10) || 0,
      want_2fa: form.value.autoWant2fa,
    })
    ElMessage.success('自动跑号已启动')
  } catch (e) { ElMessage.error('启动失败: ' + e.message) }
}
async function call(fn, name) {
  try { await fn(); ElMessage.success(name === '停止' ? '已请求停止，请等待当前任务完成' : name + ' 成功') }
  catch (e) { ElMessage.error(name + ' 失败: ' + e.message) }
}
</script>

<template>
  <div class="page">
    <el-card shadow="never" style="margin-bottom: 16px">
      <template #header><span class="section-title" style="margin: 0">全自动批量注册</span></template>

      <el-space wrap :size="16" style="margin-bottom: 12px">
        <el-form-item label="并发" style="margin: 0">
          <el-input-number v-model="form.autoConcurrency" :min="1" :max="20" />
        </el-form-item>
        <el-form-item label="冷却(分钟)" style="margin: 0">
          <el-input-number v-model="form.autoCoolDownMinutes" :min="0" :step="1" />
        </el-form-item>
        <el-form-item label="目标数(0=不限)" style="margin: 0">
          <el-input-number v-model="form.autoTargetCount" :min="0" :max="100000" />
        </el-form-item>
        <el-form-item label="OTP 等待(秒)" style="margin: 0">
          <el-input-number v-model="form.otpTimeout" :min="10" :max="600" />
        </el-form-item>
      </el-space>

      <p class="hint" style="margin: 0 0 12px">冷却时间支持小数，无上限；填 0 表示不等待。</p>

      <el-form-item label="RT">
        <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
          <el-switch v-model="form.autoWantRefreshToken" aria-label="自动注册获取 Refresh Token" />
          <span>获取 RT（Refresh Token）</span>
        </div>
        <div class="hint" style="margin-top: 6px; line-height: 1.5">
          默认关闭。AT（Access Token）和 ST（Session Token）正常获取；开启后额外尝试获取 RT。
        </div>
      </el-form-item>

      <el-form-item label="2FA">
        <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
          <el-switch v-model="form.autoWant2fa" aria-label="自动注册绑定 2FA" />
          <span>每个号注册成功后自动绑定 2FA（TOTP）</span>
        </div>
        <div class="hint" style="margin-top: 6px; line-height: 1.5">
          默认开启，与 RT 独立。绑定成功后，请到「注册结果」页备份密钥并保存到验证器；绑定结果请查看日志和注册结果。
        </div>
      </el-form-item>

      <el-form-item label="代理池">
        <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
          <el-tag :type="proxyCount ? 'success' : 'info'" effect="light">
            当前 {{ proxyCount }} 个代理
          </el-tag>
          <span class="hint">
            {{ proxyCount ? '各 worker 按顺序轮流取用' : '为空：所有 worker 用「单次注册」页填的单代理' }}
          </span>
          <el-button size="small" @click="router.push('/proxy')">管理代理池</el-button>
        </div>
      </el-form-item>

      <el-space wrap style="margin-top: 8px">
        <el-button type="primary" :disabled="!canStart" @click="start">开始</el-button>
        <el-button :disabled="!canPause" @click="call(autoPause, '暂停')">暂停</el-button>
        <el-button :disabled="!canResume" @click="call(autoResume, '恢复')">恢复</el-button>
        <el-button type="danger" :disabled="!canStop" :loading="stopping" @click="call(autoStop, '停止')">停止</el-button>
      </el-space>

      <p class="hint" style="margin-top: 10px">
        切换节点：点击停止，等所有当前任务结束且状态显示「未运行」后再换 IP，然后重新开始。
        「停止中」仍可能正在取凭证、绑定 2FA 或推送后台；并发任务也要全部结束。
      </p>

      <el-descriptions :column="4" border size="small" style="margin-top: 16px">
        <el-descriptions-item label="状态"><StatusDot :type="stateType" :text="stateLabel" /></el-descriptions-item>
        <el-descriptions-item label="成功">
          <b style="color: var(--el-color-success)">{{ autoStatus.registered_ok || 0 }}</b>
          <span v-if="autoStatus.target_count"> / {{ autoStatus.target_count }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="失败">
          <b style="color: var(--el-color-danger)">{{ autoStatus.registered_fail || 0 }}</b>
        </el-descriptions-item>
        <el-descriptions-item label="并发">{{ autoStatus.concurrency || 1 }}</el-descriptions-item>
      </el-descriptions>

      <div v-if="workers.length" style="margin-top: 12px">
        <el-tag v-for="w in workers" :key="w.id" type="warning" effect="plain" style="margin: 0 6px 6px 0">
          worker-{{ w.id }} · {{ w.email }}
        </el-tag>
      </div>
      <p v-if="autoStatus.last_message" class="hint" style="margin-top: 8px">{{ autoStatus.last_message }}</p>
    </el-card>

    <el-card shadow="never">
      <LogPanel />
    </el-card>
  </div>
</template>
