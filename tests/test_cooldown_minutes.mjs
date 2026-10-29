import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import test from 'node:test'

const store = readFileSync(new URL('../webui/frontend/src/stores/form.js', import.meta.url), 'utf8')
  .replace(/^import .+$/gm, '').replace(/^export /gm, '')
const context = vm.createContext({ defineStore: () => ({}) })
vm.runInContext(`${store}\nglobalThis.restore = restoreForm`, context)
const restore = context.restore

test('old seconds migrate once to minutes and retain the intended duration', () => {
  for (const seconds of [0, 3, 30, 120, 300]) {
    const migrated = restore({ autoCoolDown: seconds, proxy: 'saved-proxy' })
    assert.equal(migrated.autoCoolDownMinutes, seconds / 60)
    assert.equal(migrated.proxy, 'saved-proxy')
    assert.equal(Object.hasOwn(migrated, 'autoCoolDown'), false)
    assert.equal(restore(JSON.parse(JSON.stringify(migrated))).autoCoolDownMinutes, seconds / 60)
  }
  assert.equal(restore({ autoCoolDown: '120' }).autoCoolDownMinutes, 2)
  assert.equal(restore({ autoCoolDown: 120, autoCoolDownMinutes: 0 }).autoCoolDownMinutes, 0)
  assert.equal(restore({ autoCoolDown: 120, autoCoolDownMinutes: 10 }).autoCoolDownMinutes, 10)
  assert.equal(restore({}).autoCoolDownMinutes, 0.05)
})

const vue = readFileSync(new URL('../webui/frontend/src/views/AutoLoop.vue', import.meta.url), 'utf8')
const script = vue.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .+$/gm, '')

function page(minutes) {
  const requests = [], errors = []
  const harness = vm.createContext({
    computed: fn => ({ get value() { return fn() } }),
    useRouter: () => ({}), storeToRefs: value => value,
    useFormStore: () => ({ form: { value: { autoCoolDownMinutes: minutes } } }),
    useProxyStore: () => ({ count: { value: 0 }, text: '' }),
    useRuntimeStore: () => ({ autoStatus: { value: { state: 'stopped' } } }),
    proxyText: () => '',
    autoStart: async value => requests.push(value),
    ElMessage: { success() {}, error: value => errors.push(value) },
  })
  vm.runInContext(`${script}\nglobalThis.startPage = start`, harness)
  return { start: harness.startPage, requests, errors }
}

test('minutes, including fractions and values above two, reach the seconds API correctly', async () => {
  for (const [minutes, seconds] of [[0, 0], [0.5, 30], [2, 120], [5, 300], [180, 10800]]) {
    const h = page(minutes)
    await h.start()
    assert.equal(h.requests.length, 1)
    assert.equal(h.requests[0].cool_down_seconds, seconds)
    assert.deepEqual(h.errors, [])
  }
})

test('invalid durations are rejected before starting a run', async () => {
  for (const minutes of [-1, NaN, Infinity, 'not a number']) {
    const h = page(minutes)
    await h.start()
    assert.equal(h.requests.length, 0)
    assert.equal(h.errors.length, 1)
  }
})
