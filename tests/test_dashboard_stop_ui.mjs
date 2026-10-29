import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import test from 'node:test'

const frontend = new URL('../webui/frontend/src/', import.meta.url)
const runtimeScript = readFileSync(new URL('stores/runtime.js', frontend), 'utf8')
  .replace(/^import .+$/gm, '')
  .replace('export const useRuntimeStore', 'const useRuntimeStore')
const pageScript = readFileSync(new URL('views/Dashboard.vue', frontend), 'utf8')
  .match(/<script setup>([\s\S]*?)<\/script>/)[1]
  .replace(/^import .+$/gm, '')

// Execute the runtime's actual SSE handlers and the dashboard's actual computed
// values together, using only in-memory state and fake EventSource connections.
function harness() {
  const connections = []
  const reconnects = []
  const context = vm.createContext({
    defineStore: (_name, setup) => {
      let store
      return () => store ??= setup()
    },
    ref: value => ({ value }),
    computed: getter => ({ get value() { return getter() } }),
    storeToRefs: store => store,
    useRouter: () => ({ push() {} }),
    useStatsStore: () => ({ stats: { value: {} }, refresh() {} }),
    createSSE: (path, handlers, onError) => {
      const connection = { path, handlers, onError, closed: false, close() { this.closed = true } }
      connections.push(connection)
      return connection
    },
    setTimeout: callback => reconnects.push(callback),
  })
  vm.runInContext(`${runtimeScript}\n${pageScript}\nglobalThis.view = {
    runtime: useRuntimeStore(), autoStateLabel, autoStateType,
  }`, context)
  context.view.runtime.connectAutoStream()
  return {
    view: context.view,
    connections,
    reconnects,
    state: payload => connections.at(-1).handlers.state({ data: JSON.stringify(payload) }),
  }
}

test('dashboard displays draining status from the real runtime SSE state handler', () => {
  const h = harness()
  h.state({ state: 'running', stop_requested: true, workers: [{ id: 0 }] })
  assert.equal(h.view.autoStateLabel.value, '停止中，等待当前任务')
  assert.equal(h.view.autoStateType.value, 'warning')
  assert.equal(h.view.runtime.autoStatus.value.workers.length, 1)
})

test('dashboard distinguishes stopping a paused batch from an ordinary pause', () => {
  const h = harness()
  h.state({ state: 'paused', stop_requested: false })
  assert.equal(h.view.autoStateLabel.value, '已暂停')
  h.state({ state: 'paused', stop_requested: true })
  assert.equal(h.view.autoStateLabel.value, '停止中，等待当前任务')
  assert.equal(h.view.autoStateType.value, 'warning')
})

test('SSE disconnection preserves draining until the reconnect snapshot confirms completion', () => {
  const h = harness()
  h.state({ state: 'running', stop_requested: true })
  h.connections.at(-1).onError(new Error('offline test disconnect'))
  assert.equal(h.connections[0].closed, true)
  assert.equal(h.view.autoStateLabel.value, '停止中，等待当前任务')
  assert.equal(h.reconnects.length, 1)
  h.reconnects.shift()()
  assert.equal(h.connections.length, 2)
  h.state({ state: 'stopped', stop_requested: false, workers: [] })
  assert.equal(h.view.autoStateLabel.value, '未运行')
  assert.equal(h.view.autoStateType.value, 'info')
})

test('dashboard remains compatible with snapshots that omit stop_requested', () => {
  const h = harness()
  for (const [state, label, type] of [
    ['running', '运行中', 'success'],
    ['paused', '已暂停', 'warning'],
    ['stopped', '未运行', 'info'],
  ]) {
    h.state({ state })
    assert.equal(h.view.autoStateLabel.value, label)
    assert.equal(h.view.autoStateType.value, type)
  }
})

test('malformed SSE state does not clear the visible stopping status', () => {
  const h = harness()
  h.state({ state: 'running', stop_requested: true })
  h.connections.at(-1).handlers.state({ data: '{malformed' })
  assert.equal(h.view.autoStateLabel.value, '停止中，等待当前任务')
  assert.equal(h.view.autoStateType.value, 'warning')
})
