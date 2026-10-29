import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import test from 'node:test'

const frontend = new URL('../webui/frontend/src/', import.meta.url)
const pageSource = readFileSync(new URL('views/Registered.vue', frontend), 'utf8')
const pageScript = pageSource.match(/<script setup>([\s\S]*?)<\/script>/)[1]
  .replace(/^import[\s\S]*?from ['"][^'"]+['"]\r?\n/gm, '')
const fixture = email => ({
  program: 'gpt-auto-register', mode: 'protocol', capture_complete: true,
  credentials: { email, access_token: 'TEST_AT_ONLY', session_token: 'TEST_SESSION', password: 'TEST_PASSWORD' },
  cookies: [{ name: 'test_cookie', value: 'TEST_COOKIE', domain: 'example.test', path: '/' }],
  events: [{ phase: 'complete', ok: true }],
})
const plain = value => JSON.parse(JSON.stringify(value))

function harness() {
  const calls = { exports: [], reads: [], copies: [], downloads: [], messages: [], mutations: [] }
  const blobs = new Map()
  let performExport = async payload => {
    const emails = payload.all ? ['all-1@example.test', 'all-2@example.test'] : payload.emails
    const data = emails.map(fixture)
    return {
      mode: 'download', filename: emails.length === 1 ? 'registration.json' : 'registrations.json',
      mime: 'application/json;charset=utf-8', count: emails.length, emails,
      b64: Buffer.from(JSON.stringify(data.length === 1 ? data[0] : data), 'utf8').toString('base64'),
    }
  }
  let performRead = async email => ({ ok: true, data: fixture(email) })
  let performCopy = async () => undefined
  const mutation = name => async (...args) => { calls.mutations.push([name, ...args]); return {} }
  const context = vm.createContext({
    ref: value => ({ value }),
    computed: fn => ({ get value() { return fn() } }),
    watch: () => {}, onActivated: () => {}, storeToRefs: value => value,
    useFormStore: () => ({ form: { value: {} } }),
    useProxyStore: () => ({ list: { value: [] } }),
    useRuntimeStore: () => ({ dataVersion: { value: 0 }, bumpData: () => calls.mutations.push(['bump']) }),
    ElMessage: Object.fromEntries(['success', 'warning', 'error', 'info'].map(type => [type, value => calls.messages.push([type, value])])),
    ElMessageBox: { confirm: mutation('confirm') },
    exportRegistered: async payload => { calls.exports.push(plain(payload)); return performExport(payload) },
    getRegistrationJson: async email => { calls.reads.push(email); return performRead(email) },
    getRegistered: async email => ({ data: fixture(email).credentials }),
    copyText: async value => { await performCopy(value); calls.copies.push(value) },
    listRegistered: async () => ({ items: [], total: 0 }),
    listExportFormats: async () => ({ formats: [{ id: 'registration_json', label: '完整注册 JSON', mode: 'download' }] }),
    deleteRegistered: mutation('deleteRegistered'),
    bulkDeleteRegistered: mutation('bulkDeleteRegistered'),
    bulkDeleteAccounts: mutation('bulkDeleteAccounts'),
    pushRegisteredToPool: mutation('pushRegisteredToPool'),
    updateCredentials: mutation('updateCredentials'),
    checkPlus: mutation('checkPlus'),
    Blob, Uint8Array, atob,
    URL: {
      createObjectURL: blob => { const url = `blob:${blobs.size}`; blobs.set(url, blob); return url },
      revokeObjectURL: () => {},
    },
    document: {
      createElement: () => {
        const anchor = { href: '', download: '', click: () => calls.downloads.push({ filename: anchor.download, blob: blobs.get(anchor.href) }) }
        return anchor
      },
      body: { appendChild: () => {}, removeChild: () => {} },
    },
  })
  vm.runInContext(`${pageScript}\nglobalThis.page = {
    selected, exporting, exportVisible, exportText, exportedEmails, exportFormats,
    doExport, exportRegistrationJson, loadExportFormats, registrationExportText,
    viewRegistrationJson, copyRegistrationJson, clearRegistrationJson,
    registrationJsonVisible, registrationJsonLoading, registrationJsonCopying,
    registrationJsonEmail, registrationJsonData, registrationJsonText,
    registrationJsonError, registrationJsonLegacy, registrationJsonIncomplete,
    registrationJsonLimitations, credData, copyCurrentCredentialsJson,
  }`, context, { filename: 'Registered.vue' })
  return {
    page: context.page, calls,
    setExport: fn => { performExport = fn },
    setRead: fn => { performRead = fn },
    setCopy: fn => { performCopy = fn },
  }
}

async function downloadedJson(calls, index = 0) {
  return JSON.parse(await calls.downloads[index].blob.text())
}

test('single-row JSON download selects that email, preserving the full result envelope', async () => {
  const { page, calls } = harness()
  page.selected.value = [{ email: 'unrelated@example.test' }]
  await page.exportRegistrationJson('single@example.test')
  assert.deepEqual(calls.exports, [{ format: 'registration_json', emails: ['single@example.test'] }])
  assert.deepEqual(await downloadedJson(calls), fixture('single@example.test'))
  assert.match(calls.downloads[0].blob.type, /application\/json/)
  assert.equal(page.exportVisible.value, false)
  assert.deepEqual(calls.mutations, [])
})

test('selected multi-account download deduplicates emails and returns an array', async () => {
  const { page, calls } = harness()
  page.selected.value = [{ email: 'one@example.test' }, { email: 'two@example.test' }, { email: 'one@example.test' }]
  await page.exportRegistrationJson()
  assert.deepEqual(calls.exports[0], { format: 'registration_json', emails: ['one@example.test', 'two@example.test'] })
  assert.deepEqual(await downloadedJson(calls), [fixture('one@example.test'), fixture('two@example.test')])
  assert.deepEqual(calls.mutations, [])
})

test('no selection exports all records across pages, not just the visible rows', async () => {
  const { page, calls } = harness()
  await page.exportRegistrationJson()
  assert.deepEqual(calls.exports[0], { format: 'registration_json', all: true })
  assert.equal((await downloadedJson(calls)).length, 2)
  assert.deepEqual(calls.mutations, [])
})

test('dropdown secondary instance argument does not become an email', async () => {
  const { page, calls } = harness()
  await page.loadExportFormats()
  await page.doExport(page.exportFormats.value[0], { instance: 'dropdown' })
  assert.deepEqual(calls.exports[0], { format: 'registration_json', all: true })
})

test('JSON export does not replace text preview deletion targets or open a deletion flow', async () => {
  const { page, calls } = harness()
  page.exportText.value = 'existing text preview'
  page.exportedEmails.value = ['previous@example.test']
  await page.exportRegistrationJson('new@example.test')
  assert.deepEqual(plain(page.exportedEmails.value), ['previous@example.test'])
  assert.equal(page.exportText.value, 'existing text preview')
  assert.equal(page.exportVisible.value, false)
  assert.deepEqual(calls.mutations, [])
})

test('legacy batch exports display a warning without deleting or pushing accounts', async () => {
  const { page, calls, setExport } = harness()
  const data = { source: 'legacy_database', capture_complete: false, credentials: { email: 'old@example.test' } }
  setExport(async () => ({ mode: 'download', b64: Buffer.from(JSON.stringify(data)).toString('base64'), filename: 'old.json', mime: 'application/json', count: 1, legacy_count: 1 }))
  await page.exportRegistrationJson('old@example.test')
  assert.deepEqual(await downloadedJson(calls), data)
  assert.ok(calls.messages.some(([type, text]) => type === 'warning' && text.includes('无法补齐')))
  assert.deepEqual(calls.mutations, [])
})

test('duplicate export clicks are ignored while a request is pending', async () => {
  const { page, calls, setExport } = harness()
  let reject
  setExport(() => new Promise((_, fail) => { reject = fail }))
  const pending = page.exportRegistrationJson('one@example.test')
  assert.equal(page.exporting.value, true)
  await page.exportRegistrationJson('two@example.test')
  assert.equal(calls.exports.length, 1)
  reject(new Error('request failed'))
  await pending
  assert.equal(page.exporting.value, false)
  assert.equal(calls.downloads.length, 0)
  assert.equal(calls.messages.at(-1)[0], 'error')
  assert.deepEqual(calls.mutations, [])
})

test('invalid JSON export responses never claim a successful download', async () => {
  const { page, calls, setExport } = harness()
  for (const response of [{ mode: 'download', filename: 'no-content.json' }, { mode: 'text', text: '{}' }]) {
    setExport(async () => response)
    await page.exportRegistrationJson('one@example.test')
    assert.equal(page.exporting.value, false)
    assert.equal(page.exportVisible.value, false)
  }
  assert.equal(calls.downloads.length, 0)
  assert.equal(calls.messages.filter(([type]) => type === 'success').length, 0)
  assert.deepEqual(calls.mutations, [])
})

test('snapshot preview loads only on demand and clears raw data when closed', async () => {
  const { page, calls } = harness()
  assert.deepEqual(calls.reads, [])
  await page.viewRegistrationJson('one@example.test')
  assert.deepEqual(calls.reads, ['one@example.test'])
  assert.deepEqual(JSON.parse(page.registrationJsonText.value), fixture('one@example.test'))
  assert.equal(page.registrationJsonLegacy.value, false)
  page.registrationJsonVisible.value = false
  page.clearRegistrationJson()
  assert.equal(page.registrationJsonData.value, null)
  assert.equal(page.registrationJsonText.value, '')
  assert.equal(page.registrationJsonEmail.value, '')
  assert.deepEqual(calls.mutations, [])
})

test('a pending closed preview cannot restore raw credentials after close', async () => {
  const { page, setRead } = harness()
  let resolve
  setRead(() => new Promise(done => { resolve = done }))
  const pending = page.viewRegistrationJson('one@example.test')
  page.registrationJsonVisible.value = false
  page.clearRegistrationJson()
  resolve({ ok: true, data: fixture('one@example.test') })
  await pending
  assert.equal(page.registrationJsonData.value, null)
  assert.equal(page.registrationJsonLoading.value, false)
})

test('late preview responses cannot replace a newer account result', async () => {
  const { page, setRead } = harness()
  let resolveFirst
  setRead(email => email === 'one@example.test'
    ? new Promise(done => { resolveFirst = done })
    : Promise.resolve({ ok: true, data: fixture(email) }))
  const first = page.viewRegistrationJson('one@example.test')
  await page.viewRegistrationJson('two@example.test')
  resolveFirst({ ok: true, data: fixture('one@example.test') })
  await first
  assert.equal(page.registrationJsonData.value.credentials.email, 'two@example.test')
})

test('copy full registration JSON preserves its envelope instead of current edited credentials', async () => {
  const { page, calls } = harness()
  page.credData.value = { email: 'one@example.test', password: 'CURRENT_EDITED_PASSWORD' }
  await page.copyRegistrationJson('one@example.test')
  assert.deepEqual(JSON.parse(calls.copies[0]), fixture('one@example.test'))
  assert.doesNotMatch(calls.copies[0], /CURRENT_EDITED_PASSWORD/)
  page.copyCurrentCredentialsJson()
  await Promise.resolve()
  assert.equal(JSON.parse(calls.copies[1]).password, 'CURRENT_EDITED_PASSWORD')
  assert.equal(page.registrationJsonData.value, null)
  assert.deepEqual(calls.mutations, [])
})

test('legacy preview and copy disclose incomplete historical capture', async () => {
  const { page, calls, setRead } = harness()
  const data = { source: 'legacy_database', capture_complete: false, capture_note: 'Original events unavailable', credentials: { email: 'old@example.test' } }
  setRead(async () => ({ ok: true, data }))
  await page.viewRegistrationJson('old@example.test')
  assert.equal(page.registrationJsonLegacy.value, true)
  assert.equal(page.registrationJsonIncomplete.value, false)
  await page.copyRegistrationJson('old@example.test')
  assert.equal(calls.reads.length, 1)
  assert.deepEqual(JSON.parse(calls.copies[0]), data)
  assert.ok(calls.messages.some(([type, text]) => type === 'warning' && text.includes('capture_complete: false')))
})

test('partially captured new snapshots stay distinct from legacy database records', async () => {
  const { page, calls, setRead } = harness()
  const data = {
    ...fixture('partial@example.test'), capture_complete: false,
    capture: { kind: 'observed', limitations: ['One response could not be inspected.', null, '', 42] },
  }
  setRead(async () => ({ ok: true, data }))
  await page.viewRegistrationJson('partial@example.test')
  assert.equal(page.registrationJsonLegacy.value, false)
  assert.equal(page.registrationJsonIncomplete.value, true)
  assert.equal(page.registrationJsonLimitations.value, 'One response could not be inspected.')
  await page.copyRegistrationJson('partial@example.test')
  assert.deepEqual(JSON.parse(calls.copies[0]), data)
  assert.ok(calls.messages.some(([type, text]) => type === 'warning' && text.includes('部分采集信息缺失')))
  assert.doesNotMatch(JSON.stringify(calls.messages), /历史记录|无法补齐/)
})

test('partial snapshots with no limitations still show incomplete capture without claiming legacy origin', async () => {
  const { page, setRead } = harness()
  setRead(async () => ({ ok: true, data: { ...fixture('partial@example.test'), capture_complete: false } }))
  await page.viewRegistrationJson('partial@example.test')
  assert.equal(page.registrationJsonLegacy.value, false)
  assert.equal(page.registrationJsonIncomplete.value, true)
  assert.equal(page.registrationJsonLimitations.value, '')
})

test('read and clipboard failures reset progress and do not leak exception credentials', async () => {
  const { page, calls, setRead, setCopy } = harness()
  setRead(async () => { throw new Error('TEST_PRIVATE_ERROR') })
  await page.viewRegistrationJson('one@example.test')
  assert.equal(page.registrationJsonData.value, null)
  assert.equal(page.registrationJsonLoading.value, false)
  assert.doesNotMatch(page.registrationJsonError.value, /TEST_PRIVATE_ERROR/)
  await page.copyRegistrationJson('one@example.test')
  assert.equal(page.registrationJsonCopying.value, false)
  assert.equal(calls.copies.length, 0)
  setRead(async email => ({ ok: true, data: fixture(email) }))
  setCopy(async () => { throw new Error('TEST_PRIVATE_ERROR') })
  await page.copyRegistrationJson('one@example.test')
  assert.equal(page.registrationJsonCopying.value, false)
  assert.equal(calls.copies.length, 0)
  assert.doesNotMatch(JSON.stringify(calls.messages), /TEST_PRIVATE_ERROR/)
  assert.deepEqual(calls.mutations, [])
})

test('full JSON API uses encoded email and its dedicated readonly route', async () => {
  const calls = []
  const source = readFileSync(new URL('api/register.js', frontend), 'utf8')
    .replace(/^import .+$/gm, '').replace(/^export \{.+$/gm, '').replace(/export const /g, 'const ')
  const context = vm.createContext({ http: { get: url => { calls.push(url); return Promise.resolve({}) } } })
  vm.runInContext(`${source}\nglobalThis.api = { getRegistrationJson }`, context)
  await context.api.getRegistrationJson('test+tag@example.test')
  assert.deepEqual(calls, ['/api/registered/test%2Btag%40example.test/registration-json'])
})
