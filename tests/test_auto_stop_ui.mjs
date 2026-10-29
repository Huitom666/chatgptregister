import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import test from 'node:test'

const source = readFileSync(new URL('../webui/frontend/src/views/AutoLoop.vue', import.meta.url), 'utf8')
const script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1]
  .replace(/^import .+$/gm, '')

function harness(initial = { state: 'running', workers: [] }) {
  const status = { value: initial }
  const messages = []
  const context = vm.createContext({
    computed: fn => ({ get value() { return fn() } }),
    useRouter: () => ({ push() {} }),
    storeToRefs: store => store,
    useFormStore: () => ({ form: { value: {} } }),
    useProxyStore: () => ({ count: { value: 0 }, text: '' }),
    useRuntimeStore: () => ({ autoStatus: status }),
    ElMessage: {
      success: message => messages.push(['success', message]),
      error: message => messages.push(['error', message]),
    },
  })
  vm.runInContext(`${script}\nglobalThis.page = {
    canStart, canPause, canResume, canStop, stopping, stateLabel, stateType, workers, call,
  }`, context, { filename: 'AutoLoop.vue' })
  return { page: context.page, status, messages }
}

test('draining active registrations stay visible and cannot be restarted or resumed', () => {
  const h = harness({ state: 'running', workers: [{ id: 0, email: 'offline@example.invalid' }] })
  assert.equal(h.page.canPause.value, true)
  h.status.value = { ...h.status.value, stop_requested: true }
  for (const key of ['canStart', 'canPause', 'canResume', 'canStop']) {
    assert.equal(h.page[key].value, false, key)
  }
  assert.equal(h.page.workers.value.length, 1)
  assert.equal(h.page.stopping.value, true)
  assert.match(h.page.stateLabel.value, /停止中.*等待/)
  assert.equal(h.page.stateType.value, 'warning')
})

test('stop requested while paused blocks resume until outstanding work finishes', () => {
  const h = harness({ state: 'paused', stop_requested: false })
  assert.equal(h.page.canResume.value, true)
  h.status.value = { state: 'paused', stop_requested: true }
  assert.equal(h.page.canResume.value, false)
  assert.equal(h.page.canStart.value, false)
  assert.match(h.page.stateLabel.value, /停止中/)
})

test('a terminal status update enables a fresh start after draining completes', () => {
  const h = harness({ state: 'running', stop_requested: true, workers: [{ id: 0 }] })
  assert.equal(h.page.canStart.value, false)
  h.status.value = { state: 'stopped', stop_requested: false, workers: [] }
  assert.equal(h.page.canStart.value, true)
  assert.equal(h.page.canStop.value, false)
  assert.equal(h.page.canResume.value, false)
  assert.equal(h.page.workers.value.length, 0)
  assert.equal(h.page.stateLabel.value, '未运行')
})

test('older status responses without stop_requested preserve normal controls', () => {
  const h = harness()
  for (const [state, expected] of [
    ['stopped', [true, false, false, false]],
    ['running', [false, true, false, true]],
    ['paused', [false, false, true, true]],
  ]) {
    h.status.value = { state }
    assert.equal(h.page.stopping.value, false)
    assert.deepEqual(['canStart', 'canPause', 'canResume', 'canStop'].map(key => h.page[key].value), expected)
  }
})

test('stop acknowledgement explains waiting and does not locally declare completion', async () => {
  const h = harness()
  let acknowledge
  const pending = h.page.call(() => new Promise(resolve => { acknowledge = resolve }), '停止')
  assert.equal(h.messages.length, 0)
  acknowledge({ ok: true })
  await pending
  assert.equal(h.messages.length, 1)
  assert.equal(h.messages[0][0], 'success')
  assert.match(h.messages[0][1], /已请求停止.*等待/)
  assert.notEqual(h.messages[0][1], '停止 成功')
  assert.equal(h.status.value.state, 'running')
  assert.equal(h.page.canStart.value, false)
})

test('a rejected stop request never displays successful completion', async () => {
  const h = harness()
  await h.page.call(async () => { throw new Error('offline request rejected') }, '停止')
  assert.deepEqual(h.messages, [['error', '停止 失败: offline request rejected']])
  assert.equal(h.status.value.state, 'running')
  assert.equal(h.page.canStart.value, false)
})
