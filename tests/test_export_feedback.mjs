import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'
import test from 'node:test'

const frontend = new URL('../webui/frontend/src/', import.meta.url)
const pageSource = readFileSync(new URL('views/ExportConfig.vue', frontend), 'utf8')
const pageScript = pageSource.match(/<script setup>([\s\S]*?)<\/script>/)[1]
  .replace(/^import .+$/gm, '')

function harness() {
  const clock = { now: 1000 }
  const intervals = new Map()
  const callbacks = {}
  const calls = { tests: [], saves: [], messages: [] }
  let config = {
    pool_enabled: '0', pool_url: 'https://pool.example.test/api/accounts',
    pool_admin_key: '***', pool_timeout: '40',
    cpa_url: 'https://cpa.example.test', cpa_timeout: '30',
    sub2api_url: 'https://sub.example.test', sub2api_timeout: '30',
  }
  let performTest = async () => ({ ok: true, message: 'connected' })
  const context = vm.createContext({
    reactive: value => value,
    ref: value => ({ value }),
    computed: fn => ({ get value() { return fn() } }),
    onActivated: fn => { callbacks.activate = fn },
    onUnmounted: fn => { callbacks.unmount = fn },
    Date: { now: () => clock.now },
    setInterval: fn => { const id = Symbol(); intervals.set(id, fn); return id },
    clearInterval: id => intervals.delete(id),
    ElMessage: {
      success: value => calls.messages.push(['success', value]),
      error: value => calls.messages.push(['error', value]),
    },
    getExportConfig: async () => ({ config }),
    saveExportConfig: async payload => {
      calls.saves.push(payload)
      config = { ...config, ...payload, pool_admin_key: '***' }
    },
    testExport: async target => { calls.tests.push(target); return performTest(target) },
  })
  vm.runInContext(`${pageScript}\nglobalThis.page = {
    pool, cpa, sub, loaded, saving, testingPool, testingCpa, testingSub,
    testResults, connectionChanged, test, save, load, feedbackTitle, feedbackType,
  }`, context, { filename: 'ExportConfig.vue' })
  return {
    page: context.page, calls, callbacks, intervals,
    setTest: fn => { performTest = fn },
    advance: milliseconds => {
      clock.now += milliseconds
      for (const fn of intervals.values()) fn()
    },
  }
}

test('loaded masked credentials establish a clean baseline; enabled is independent', async () => {
  const { page } = harness()
  await page.load()
  assert.equal(page.loaded.value, true)
  assert.equal(page.pool.key, '')
  assert.equal(page.connectionChanged.value.pool, false)
  page.pool.enabled = true
  assert.equal(page.connectionChanged.value.pool, false)
})

for (const [field, value] of [
  ['url', 'https://changed.example.test/api/accounts'],
  ['key', 'new-test-key'],
  ['timeout', 120],
]) {
  test(`unsaved ${field} prevents testing stale configuration`, async () => {
    const { page, calls } = harness()
    await page.load()
    page.pool[field] = value
    assert.equal(page.connectionChanged.value.pool, true)
    await page.test('pool')
    assert.equal(calls.tests.length, 0)
    assert.equal(page.testResults.pool.phase, 'warning')
    assert.match(page.testResults.pool.message, /先点击底部「保存配置」/)
  })
}

test('configuration must load before a probe; reverted fields do not remain dirty', async () => {
  const { page, calls } = harness()
  await page.test('pool')
  assert.equal(calls.tests.length, 0)
  assert.match(page.testResults.pool.message, /尚未加载/)
  await page.load()
  page.pool.key = 'changed'
  page.pool.key = ''
  assert.equal(page.connectionChanged.value.pool, false)
})

test('pending probes show elapsed time, reject duplicate clicks, then persist success', async () => {
  const { page, calls, setTest, advance, intervals } = harness()
  await page.load()
  let resolve
  setTest(() => new Promise(done => { resolve = done }))
  const pending = page.test('pool')
  assert.equal(page.testingPool.value, true)
  assert.equal(page.testResults.pool.phase, 'running')
  advance(2250)
  assert.match(page.feedbackTitle('pool'), /2\.3 秒/)
  await page.test('pool')
  assert.equal(calls.tests.length, 1)
  resolve({ ok: true, message: 'upstream body should not be shown: TEST_SECRET' })
  await pending
  assert.equal(page.testingPool.value, false)
  assert.equal(intervals.size, 0)
  assert.equal(page.testResults.pool.phase, 'success')
  assert.equal(page.testResults.pool.elapsed, 2.25)
  assert.match(page.testResults.pool.message, /鉴权正常，未推送任何账号/)
  assert.doesNotMatch(page.testResults.pool.message, /TEST_SECRET/)
  advance(5000)
  assert.equal(page.testResults.pool.elapsed, 2.25)
  assert.equal(page.testResults.pool.phase, 'success')
})

test('known pool failures remain visible and always reset loading/timers', async () => {
  const { page, setTest, advance, intervals } = harness()
  await page.load()
  setTest(async () => {
    advance(2100)
    throw new Error('号池管理员密钥无效或已失效。')
  })
  await page.test('pool')
  assert.equal(page.testingPool.value, false)
  assert.equal(intervals.size, 0)
  assert.equal(page.testResults.pool.phase, 'error')
  assert.equal(page.testResults.pool.message, '号池管理员密钥无效或已失效。')
  assert.match(page.feedbackTitle('pool'), /2\.1 秒/)
})

test('timeout and unexpected error text are safely summarized', async () => {
  const { page, setTest } = harness()
  await page.load()
  setTest(async () => { throw new Error('timeout of 250000ms exceeded') })
  await page.test('pool')
  assert.match(page.testResults.pool.message, /超时/)
  assert.equal(page.testingPool.value, false)
  setTest(async () => { throw new Error('<html>Authorization: Bearer TEST_SECRET</html>') })
  await page.test('pool')
  assert.equal(page.testResults.pool.phase, 'error')
  assert.doesNotMatch(page.testResults.pool.message, /TEST_SECRET|<html>|Bearer/)
})

test('HTTP success with ok:false is displayed as a failure', async () => {
  const { page, setTest } = harness()
  await page.load()
  setTest(async () => ({ ok: false, message: '号池接口拒绝访问，请检查管理员权限。' }))
  await page.test('pool')
  assert.equal(page.testResults.pool.phase, 'error')
  assert.equal(page.feedbackType('pool'), 'error')
})

test('malformed successful HTTP responses do not claim verified connectivity', async () => {
  const { page, setTest } = harness()
  await page.load()
  for (const response of [undefined, {}, { ok: 'true' }]) {
    setTest(async () => response)
    await page.test('pool')
    assert.equal(page.testResults.pool.phase, 'error')
    assert.doesNotMatch(page.testResults.pool.message, /鉴权正常/)
  }
})

test('saving refreshes the baseline and keeps blank key preservation behavior', async () => {
  const { page, calls } = harness()
  await page.load()
  page.pool.url = 'https://new.example.test/api/accounts'
  await page.save()
  assert.equal(calls.saves.length, 1)
  assert.equal(calls.saves[0].pool_admin_key, '***')
  assert.equal(page.connectionChanged.value.pool, false)
  await page.test('pool')
  assert.equal(calls.tests.length, 1)
  page.saving.value = true
  await page.test('pool')
  assert.equal(calls.tests.length, 1)
})

test('CPA 405 does not incorrectly claim that its credentials were verified', async () => {
  const { page, setTest } = harness()
  await page.load()
  setTest(async () => ({ ok: true, message: 'CPA URL 可达（HTTP 405），但无法用 GET 验证密钥' }))
  await page.test('cpa')
  assert.match(page.testResults.cpa.message, /暂时无法确认管理密钥有效/)
  assert.equal(page.testingCpa.value, false)
})

test('unmount clears an active interval', async () => {
  const { page, setTest, intervals, callbacks } = harness()
  await page.load()
  let resolve
  setTest(() => new Promise(done => { resolve = done }))
  const pending = page.test('pool')
  assert.equal(intervals.size, 1)
  callbacks.unmount()
  assert.equal(intervals.size, 0)
  resolve({ ok: true })
  await pending
})

test('only export probes override the global request timeout', async () => {
  const requests = []
  const source = readFileSync(new URL('api/settings.js', frontend), 'utf8')
    .replace(/^import .+$/gm, '').replace(/export const /g, 'const ')
  const context = vm.createContext({
    http: { post: (...args) => { requests.push(args); return Promise.resolve({ ok: true }) } },
  })
  vm.runInContext(`${source}\nglobalThis.api = { testExport, testMail, saveExportConfig }`, context,
    { filename: fileURLToPath(new URL('api/settings.js', frontend)) })
  await context.api.testExport('pool')
  await context.api.testExport('cpa')
  await context.api.testExport('sub2api')
  await context.api.testMail()
  await context.api.saveExportConfig({ pool_enabled: '0' })
  assert.deepEqual(requests.slice(0, 3).map(request => request[2].timeout), [250000, 610000, 610000])
  assert.equal(requests[0][1].target, 'pool')
  assert.equal(requests[3][2], undefined)
  assert.equal(requests[4][2], undefined)
})
