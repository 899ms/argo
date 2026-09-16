import { test, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, writeFileSync, readFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { normalizeTracks, stageTracks, evaluateGates, normalizeSource, mapArgoToWebResult, buildLocalSources, parseJsonArray, parseJsonObject, extractValues, resolveNativeSpawn, makeNativeTool, buildChildToolFilters, apply, nativeViaMcpPool, disposeSharedMcp } from '../dsh/index.js'

test('mapArgoToWebResult maps compact argo payload to web sources', () => {
  const mapped = mapArgoToWebResult({
    query: 'q',
    engine: 'octen',
    results: [
      { title: '甲', url: 'https://a.com/1', snippet: 's1', source: 'octen', score: 0.9 },
      { title: '', url: 'https://b.com/2', snippet: null },
      { url: 'https://c.com/3' },
      { title: '无url', snippet: 'no' }
    ]
  })
  assert.equal(mapped.sources.length, 3)
  assert.deepEqual(mapped.sources[0], { url: 'https://a.com/1', title: '甲', snippet: 's1' })
  assert.deepEqual(mapped.sources[1], { url: 'https://b.com/2' })
  assert.equal(mapped.truncated, false)
  assert.equal(mapped.engine, 'octen')
})

test('mapArgoToWebResult tolerates empty and malformed payloads', () => {
  const empty = mapArgoToWebResult({}, 'q')
  assert.deepEqual(empty.sources, [])
  const junk = mapArgoToWebResult({ results: 'nope' }, 'q')
  assert.deepEqual(junk.sources, [])
  const nonArray = mapArgoToWebResult({ results: [42, { url: 7 }] }, 'q')
  assert.deepEqual(nonArray.sources, [])
})

test('normalizeTracks keeps depends_on and dedupes ids', () => {
  const tracks = normalizeTracks({
    tracks: [
      { id: 'def', title: '定义', question: '如何分类', rationale: 'r1', depends_on: [] },
      { id: 'risk', title: '风险', question: '瓶颈', rationale: 'r2', depends_on: ['def'] },
      { id: 'def', title: '重复', question: '重复', rationale: 'r3' },
    ],
  }, 9)
  assert.equal(tracks.length, 2)
  assert.deepEqual(tracks[0].depends_on, [])
  assert.deepEqual(tracks[1].depends_on, ['def'])
})

test('normalizeTracks caps at maximum and clips long fields', () => {
  const tracks = normalizeTracks({
    tracks: [1, 2, 3, 4].map((n, i) => ({
      id: `t${i}`, title: 't', question: 'q', rationale: 'r',
    })),
  }, 2)
  assert.equal(tracks.length, 2)
})

test('stageTracks: no deps means a single parallel stage', () => {
  const { stages, warnings } = stageTracks([
    { id: 'a', depends_on: [] },
    { id: 'b', depends_on: [] },
  ])
  assert.equal(warnings.length, 0)
  assert.equal(stages.length, 1)
  assert.deepEqual(stages[0].map(t => t.id).sort(), ['a', 'b'])
})

test('stageTracks: depends_on splits into sequential stages', () => {
  const { stages, warnings } = stageTracks([
    { id: 'risk', depends_on: ['def'] },
    { id: 'def', depends_on: [] },
    { id: 'market', depends_on: ['def'] },
  ])
  assert.equal(warnings.length, 0)
  assert.deepEqual(stages.map(s => s.map(t => t.id)), [['def'], ['market', 'risk']])
})

test('stageTracks: missing dependency is a warning, not a crash', () => {
  const { stages, warnings } = stageTracks([
    { id: 'a', depends_on: ['ghost'] },
  ])
  assert.equal(warnings.length, 1)
  assert.match(warnings[0], /ghost/)
  assert.equal(stages.length, 1)
})

test('stageTracks: cycle merges into last stage with warning', () => {
  const { stages, warnings } = stageTracks([
    { id: 'a', depends_on: ['b'] },
    { id: 'b', depends_on: ['a'] },
  ])
  assert.equal(stages.length, 1)
  assert.equal(stages[0].length, 2)
  assert.ok(warnings.some(w => w.includes('cycle')))
})

test('evaluateGates: no sources fails to low', () => {
  const gates = evaluateGates({
    stats: { completedTracks: 2, failedTracks: 0, sourceCount: 0 },
    sources: [], caveats: [], unansweredQuestions: [],
  })
  assert.equal(gates.passed, false)
  assert.equal(gates.conclusion_cap, 'low')
  assert.ok(gates.failures.some(f => f.id === 'no_sources'))
})

test('evaluateGates: all tracks failed fails to low', () => {
  const gates = evaluateGates({
    stats: { completedTracks: 0, failedTracks: 3, sourceCount: 0 },
    sources: [], caveats: [], unansweredQuestions: [],
  })
  assert.equal(gates.conclusion_cap, 'low')
  assert.ok(gates.failures.some(f => f.id === 'all_tracks_failed'))
})

test('evaluateGates: partial track failure caps at medium', () => {
  const gates = evaluateGates({
    stats: { completedTracks: 2, failedTracks: 1, sourceCount: 5 },
    sources: [
      { confidence: 'high', url: 'https://a.com' },
      { confidence: 'medium', url: 'https://b.com' },
    ],
    caveats: [], unansweredQuestions: [],
  })
  assert.equal(gates.passed, true)
  assert.equal(gates.conclusion_cap, 'medium')
  assert.ok(gates.warnings.some(w => w.id === 'partial_track_failure'))
})

test('evaluateGates: no high-confidence sources warns to medium', () => {
  const gates = evaluateGates({
    stats: { completedTracks: 2, failedTracks: 0, sourceCount: 3 },
    sources: [
      { confidence: 'low', url: 'https://a.com' },
      { confidence: 'low', url: 'https://b.com' },
    ],
    caveats: [], unansweredQuestions: [],
  })
  assert.equal(gates.conclusion_cap, 'medium')
  assert.ok(gates.warnings.some(w => w.id === 'no_high_confidence_sources'))
})

test('evaluateGates: clean run caps at high', () => {
  const gates = evaluateGates({
    stats: { completedTracks: 3, failedTracks: 0, sourceCount: 8 },
    sources: [
      { confidence: 'high', url: 'https://a.com' },
      { confidence: 'high', url: 'https://b.com' },
      { confidence: 'medium', url: 'https://c.com' },
    ],
    caveats: ['one caveat'], unansweredQuestions: [],
  })
  assert.equal(gates.passed, true)
  assert.equal(gates.conclusion_cap, 'high')
  assert.equal(gates.failures.length, 0)
})

test('normalizeSource: only http(s) urls enter the ledger', () => {
  const http = normalizeSource({ title: 't', url: 'https://a.com/x', claim: 'c' })
  assert.equal(http.url, 'https://a.com/x')
  const javascript = normalizeSource({ title: 't', url: 'javascript:alert(1)', claim: 'c' })
  assert.equal(javascript, undefined)
  const ftp = normalizeSource({ title: 't', url: 'ftp://a.com/file', claim: 'c' })
  assert.equal(ftp, undefined)
  const httpNoScheme = normalizeSource({ title: 't', url: 'example.com/x', claim: 'c' })
  assert.equal(httpNoScheme, undefined)
})

test('normalizeSource: confidence defaults to low, limits high to high/medium', () => {
  const low = normalizeSource({ title: 't', url: 'https://a.com', claim: 'c' })
  assert.equal(low.confidence, 'low')
  const high = normalizeSource({ title: 't', url: 'https://a.com', claim: 'c', confidence: 'high' })
  assert.equal(high.confidence, 'high')
  const bogus = normalizeSource({ title: 't', url: 'https://a.com', claim: 'c', confidence: 'certain' })
  assert.equal(bogus.confidence, 'low')
})

test('persistReport writes to <dir>/<ts>-<slug>.md and returns absolute path', async () => {
  const { mkdtemp, readFile } = await import('node:fs/promises')
  const { tmpdir } = await import('node:os')
  const { join } = await import('node:path')
  const { persistReport } = await import('../dsh/index.js')
  const dir = await mkdtemp(join(tmpdir(), 'argo-report-'))
  const file = await persistReport(dir, '测试 报告？问题', '# Report\ncontent')
  assert.ok(file.startsWith(dir))
  assert.ok(file.endsWith('.md'))
  const body = await readFile(file, 'utf8')
  assert.equal(body, '# Report\ncontent')
  // 目录自动创建
  const nested = await persistReport(join(dir, 'a', 'b'), 'q', 'x')
  assert.ok(nested.startsWith(join(dir, 'a', 'b')))
})

test('buildLocalSources registers lineage without content', () => {
  const out = buildLocalSources([
    { path: '~/data/company.xlsx', role: '原始数据', sha256: 'abc', size: 100, mtime: 123 },
    { path: '', role: 'x' },
    { path: '~/notes/a.md' },
  ])
  assert.equal(out.length, 2)
  assert.equal(out[0].ref, '[L1]')
  assert.equal(out[0].type, 'file')
  assert.equal(out[0].role, '原始数据')
  assert.equal(out[0].note.includes('内容未入库'), true)
  assert.equal(out[1].kind, '')
  assert.equal(out[1].role, 'data')
})

test('parseJsonArray tolerates empty & malformed', () => {
  assert.deepEqual(parseJsonArray(undefined, 'fi'), [])
  assert.deepEqual(parseJsonArray('', 'fi'), [])
  const good = parseJsonArray('[{"path":"a"}]', 'fi')
  assert.equal(good.length, 1)
  assert.deepEqual(parseJsonArray('nope', 'fi'), [])
  assert.deepEqual(parseJsonArray([1, 2], 'fi'), [])
})

test('parseJsonObject tolerates empty & malformed', () => {
  assert.deepEqual(parseJsonObject(undefined, 'rc'), {})
  assert.deepEqual(parseJsonObject('', 'rc'), {})
  const good = parseJsonObject('{"script":"1+1","budget":{"timeout_s":30}}', 'rc')
  assert.equal(good.script, '1+1')
  assert.equal(good.budget.timeout_s, 30)
  assert.deepEqual(parseJsonObject('oops', 'rc'), {})
})

test('extractValues pulls numbers and percentages', () => {
  assert.deepEqual(extractValues('营收 1,234.5 万，同比 23%'), [1234.5, 0.23])
  assert.deepEqual(extractValues(''), [])
  assert.deepEqual(extractValues('无数字文本'), [])
})

test('evaluateGates: recompute_skipped caps at medium', () => {
  const gates = evaluateGates({
    stats: { completedTracks: 2, failedTracks: 0, sourceCount: 3 },
    sources: [
      { confidence: 'high', url: 'https://a.com' },
      { confidence: 'high', url: 'https://b.com' },
    ],
    caveats: [], unansweredQuestions: [],
    recomputeExpected: true, recomputedValues: [], localSources: [],
  })
  assert.equal(gates.conclusion_cap, 'medium')
  assert.ok(gates.warnings.some(w => w.id === 'recompute_skipped'))
})

test('evaluateGates: recompute_conflict when no numeric overlap', () => {
  const gates = evaluateGates({
    stats: { completedTracks: 2, failedTracks: 0, sourceCount: 3 },
    sources: [
      { confidence: 'high', url: 'https://a.com', title: '甲', claim: '营收 100 万元' },
      { confidence: 'high', url: 'https://b.com', title: '乙', claim: '利润 5 万元' },
    ],
    caveats: [], unansweredQuestions: [],
    recomputeExpected: true,
    recomputedValues: [{ ok: true, values: [42], stdout_tail: '', stderr_tail: '' }],
    localSources: [],
  })
  assert.ok(gates.warnings.some(w => w.id === 'recompute_conflict'))
})

test('evaluateGates: local first-hand source counts as evidence', () => {
  // 无网络源但含本地一手文件时，不应触发 no_sources 假阴性
  const gates = evaluateGates({
    stats: { completedTracks: 1, failedTracks: 0, sourceCount: 0 },
    sources: [], caveats: [], unansweredQuestions: [],
    localSources: [{ path: '~/company.xlsx', ref: '[L1]' }],
  })
  assert.ok(!gates.failures.some(f => f.id === 'no_sources'), '本地一手存在时不应判 no_sources')
})

// ── 原生一等工具（CLI 单发形态）──────────────────────────────────────────

test('resolveNativeSpawn: npx mode appends call subcommand', () => {
  const config = { searchCommand: 'npx', searchArgs: ['-y', 'github:taxueseek/argo'] }
  const spec = resolveNativeSpawn(config, 'argo_search', '{"query":"q"}')
  assert.equal(spec.command, 'npx')
  assert.deepEqual(spec.args.slice(-3), ['call', 'argo_search', '{"query":"q"}'])
})

test('resolveNativeSpawn: local mcp_server.py mode uses --call flag', () => {
  const config = { searchCommand: 'python3', searchArgs: ['/opt/argo/scripts/mcp_server.py'] }
  const spec = resolveNativeSpawn(config, 'argo_fetch', '{"url":"https://x"}')
  assert.equal(spec.command, 'python3')
  assert.deepEqual(spec.args, ['/opt/argo/scripts/mcp_server.py', '--call', 'argo_fetch', '{"url":"https://x"}'])
})

test('resolveNativeSpawn: empty config falls back to defaults with call tail', () => {
  // DEFAULTS.searchCommand 读环境变量，不硬断言具体命令值；
  // 两种部署路径的 args 尾部统一是 [tool, payload]。
  const spec = resolveNativeSpawn({}, 'argo_search', '{"query":"q"}')
  assert.equal(typeof spec.command, 'string')
  assert.ok(spec.command.length > 0)
  assert.deepEqual(spec.args.slice(-2), ['argo_search', '{"query":"q"}'])
})

test('makeNativeTool: execute forwards picked args and render unwraps MCP payload', async () => {
  const captured = []
  const run = async (spawnSpec) => {
    captured.push(spawnSpec)
    return JSON.stringify({ content: [{ type: 'text', text: 'hello argo' }] })
  }
  const tool = makeNativeTool({ nativeMaxChars: 24_000 }, 'argo_search', { run })
  assert.equal(tool.name, 'argo_search')
  assert.deepEqual(tool.parameters.required, ['query'])

  const value = await tool.execute({ query: 'q1', evil: 'injected', engine: '' })
  // junk 键被 pickArgs 丢弃、空串过滤；payload 只含合法非空参数
  const payload = JSON.parse(captured[0].args[captured[0].args.length - 1])
  assert.deepEqual(payload, { query: 'q1' })

  const blocks = tool.output.render({}, value)
  assert.equal(blocks[0].type, 'text')
  assert.equal(blocks[0].text, 'hello argo')
})

test('makeNativeTool: render clips to nativeMaxChars and tolerates raw stdout', () => {
  const run = async () => JSON.stringify({ content: [{ type: 'text', text: 'x'.repeat(100) }] })
  const tool = makeNativeTool({ nativeMaxChars: 10 }, 'argo_fetch', { run })
  const blocks = tool.output.render({}, { stdout: JSON.stringify({ content: [{ type: 'text', text: 'x'.repeat(100) }] }) })
  assert.ok(blocks[0].text.length <= 10)
  // 非 JSON stdout 原样透传（同样受 clip 约束）
  const raw = makeNativeTool({ nativeMaxChars: 5 }, 'argo_fetch', { run: async () => 'plain text!' })
  assert.ok(raw.output.render({}, { stdout: 'plain text!' })[0].text.length <= 5)
})

test('makeNativeTool: prefers persistent MCP connection and passes picked args object', async () => {
  const seen = []
  // mcpCall 收到的是参数对象（不是 JSON 字符串），与 stdio tools/call 的 arguments 对齐
  const mcpCall = async (config, kind, argsObj, timeoutMs) => {
    seen.push({ kind, argsObj, timeoutMs })
    return JSON.stringify({ content: [{ type: 'text', text: 'via-persistent' }] })
  }
  // 常驻连接可用时，单发 runner 不应被调用
  const run = async () => { throw new Error('should not spawn when persistent MCP works') }
  const tool = makeNativeTool(
    { nativeTimeoutMs: 1234 }, 'argo_search', { mcpCall, run })
  const value = await tool.execute({ query: 'q', engine: '', bogus: 1 })
  assert.deepEqual(seen[0].argsObj, { query: 'q' })
  assert.equal(seen[0].kind, 'argo_search')
  assert.equal(seen[0].timeoutMs, 1234)
  assert.equal(tool.output.render({}, value)[0].text, 'via-persistent')
})

test('makeNativeTool: falls back to single-shot spawn when persistent connection fails', async () => {
  const spawned = []
  const mcpCall = async () => { throw new Error('connection closed') }
  const run = async (spawnSpec) => {
    spawned.push(spawnSpec)
    return JSON.stringify({ content: [{ type: 'text', text: 'via-spawn' }] })
  }
  const tool = makeNativeTool(
    { searchCommand: 'python3', searchArgs: ['/opt/argo/scripts/mcp_server.py'] },
    'argo_fetch', { mcpCall, run })
  const value = await tool.execute({ url: 'https://x' })
  assert.equal(spawned.length, 1, '连接失败后应回退单发一次')
  assert.equal(tool.output.render({}, value)[0].text, 'via-spawn')
})

test('makeNativeTool: unknown tool name fails loudly', () => {
  assert.throws(() => makeNativeTool({}, 'argo_nope'), /unknown native tool/)
})

function makeCtx() {
  const registered = []
  const providers = []
  const ctx = {
    tools: { register: (t) => registered.push(t) },
    subagents: { getProvider: () => null, list: () => [] },
    effect() {},
    get: (svc) => svc === 'web' ? { registerSearchProvider: (p) => providers.push(p) } : null,
  }
  return { ctx, registered, providers }
}

test('apply: default config registers native tools and an available web provider', () => {
  const { ctx, registered, providers } = makeCtx()
  apply(ctx, {})
  const names = registered.map(t => t.name)
  assert.ok(names.includes('argo_search'), 'argo_search 应注册')
  assert.ok(names.includes('argo_fetch'), 'argo_fetch 应注册')
  assert.ok(names.includes('wide_research'))
  assert.equal(providers.length, 1)
  assert.equal(providers[0].id, 'argo')
  assert.equal(providers[0].available(), true)
})

test('apply: nativeTools=[] and searchProviderEnabled=false disables both seams', () => {
  const { ctx, registered, providers } = makeCtx()
  apply(ctx, { nativeTools: [], searchProviderEnabled: false })
  const names = registered.map(t => t.name)
  assert.ok(!names.includes('argo_search'))
  assert.ok(!names.includes('argo_fetch'))
  assert.ok(names.includes('wide_research'), 'wide_research 不受搜索开关影响')
  assert.equal(providers.length, 1, 'provider 仍注册（headless 一致性）')
  assert.equal(providers[0].available(), false)
})

test('apply: unknown native tool name fails loudly', () => {
  const { ctx } = makeCtx()
  assert.throws(() => apply(ctx, { nativeTools: ['argo_nope'] }), /unknown native tool/)
})

test('buildChildToolFilters: default allow list dual-writes MCP and native tools', () => {
  const { allow, deny } = buildChildToolFilters({}, 'wide_research')
  for (const tool of ['argo_search', 'argo_fetch', 'web_search', 'web_fetch', 'mcp__argo__argo_search']) {
    assert.ok(allow.includes(tool), `默认白名单应含 ${tool}（MCP/原生两态自洽）`)
  }
  assert.ok(!allow.includes('wide_research'), '自身不入白名单')
  assert.ok(!allow.includes('mcp__argo__argo_research'), '研究不套研究')
  assert.ok(deny.includes('wide_research'))
})

test('buildChildToolFilters: explicit empty allow falls back to deny path', () => {
  const { allow, deny } = buildChildToolFilters({ childToolAllow: [] }, 'wide_research')
  assert.deepEqual(allow, [])
  assert.ok(deny.includes('wide_research'))
})

test('buildChildToolFilters: explicit list filters blocked names and dedupes', () => {
  const { allow } = buildChildToolFilters({
    childToolAllow: ['argo_search', 'argo_search', 'mcp__argo__argo_research', 'web_search'],
  }, 'wide_research')
  assert.deepEqual(allow, ['argo_search', 'web_search'])
})

// ── 连接池（真实 stdio 桩）─────────────────────────────────────────────────
// 上面注入的 mcpCall 是替身，测不到池本身。这里起一个真进程讲 NDJSON：每次
// 启动往计数文件追加一行，「开了几条连接」于是成为可断言的观测值——复用、
// 按需增长、并发是否真并行、dispose 是否收干净，都能被证明而不是被假设。

const STUB_DIR = mkdtempSync(join(tmpdir(), 'argo-dsh-stub-'))
const STUB_SERVER = join(STUB_DIR, 'stub-mcp.js')

writeFileSync(STUB_SERVER, `
const fs = require('node:fs')
if (process.env.ARGO_STUB_COUNTER) fs.appendFileSync(process.env.ARGO_STUB_COUNTER, 'spawn\\n')
const delayMs = Number(process.env.ARGO_STUB_DELAY_MS || 0)
const toolError = process.env.ARGO_STUB_TOOL_ERROR === '1'
let buf = ''
process.stdin.setEncoding('utf8')
const write = (obj) => process.stdout.write(JSON.stringify(obj) + '\\n')
process.stdin.on('data', (chunk) => {
  buf += chunk
  let nl
  while ((nl = buf.indexOf('\\n')) >= 0) {
    const line = buf.slice(0, nl).trim()
    buf = buf.slice(nl + 1)
    if (line === '') continue
    let msg
    try { msg = JSON.parse(line) } catch { continue }
    if (msg.id === undefined) continue
    if (msg.method === 'initialize') {
      write({ jsonrpc: '2.0', id: msg.id, result: { protocolVersion: '2025-06-18', capabilities: {}, serverInfo: { name: 'stub', version: '0' } } })
      continue
    }
    setTimeout(() => {
      write({
        jsonrpc: '2.0',
        id: msg.id,
        result: toolError
          ? { content: [{ type: 'text', text: 'stub tool failed' }], isError: true }
          : { content: [{ type: 'text', text: 'pooled-ok' }] },
      })
    }, delayMs)
  }
})
`, 'utf8')

let spawnLog = ''
let envRestore = {}

function stubEnv(vars = {}) {
  spawnLog = join(mkdtempSync(join(tmpdir(), 'argo-dsh-count-')), 'spawns.log')
  envRestore = {}
  for (const [key, value] of Object.entries({ ARGO_STUB_COUNTER: spawnLog, ...vars })) {
    envRestore[key] = process.env[key]
    process.env[key] = value
  }
}

/** 桩进程启动次数 === 池开出的连接数。 */
function spawnCount() {
  if (!existsSync(spawnLog)) return 0
  return readFileSync(spawnLog, 'utf8').split('\n').filter(Boolean).length
}

function stubTool(config = {}) {
  return makeNativeTool(
    { searchCommand: process.execPath, searchArgs: [STUB_SERVER], ...config },
    'argo_search',
  )
}

afterEach(() => {
  disposeSharedMcp()
  for (const [key, value] of Object.entries(envRestore)) {
    if (value === undefined) delete process.env[key]
    else process.env[key] = value
  }
  envRestore = {}
})

test('pool: sequential calls reuse one connection (no extra resident process)', async () => {
  stubEnv()
  const tool = stubTool({ searchConcurrency: 3 })
  await tool.execute({ query: 'a' })
  await tool.execute({ query: 'b' })
  assert.equal(spawnCount(), 1, '顺序调用只该有一条常驻连接，池化不该带来固定开销')
})

test('pool: concurrent calls grow the pool without exceeding the cap', async () => {
  stubEnv({ ARGO_STUB_DELAY_MS: '150' })
  const tool = stubTool({ searchConcurrency: 3 })
  await Promise.all([1, 2, 3].map(i => tool.execute({ query: `q${i}` })))
  const spawned = spawnCount()
  assert.ok(spawned > 1, `并发调用应补开连接，实际只有 ${spawned} 条`)
  assert.ok(spawned <= 3, `不得越过 searchConcurrency，实际开了 ${spawned} 条`)
})

test('pool: concurrent calls run in parallel instead of queueing on one connection', async () => {
  stubEnv({ ARGO_STUB_DELAY_MS: '300' })
  const tool = stubTool({ searchConcurrency: 3 })
  const started = Date.now()
  await Promise.all([1, 2, 3].map(i => tool.execute({ query: `q${i}` })))
  const elapsed = Date.now() - started
  // 单连接串行会累加到 ~900ms；池化后应贴近单次 300ms。阈值放宽到 700ms 抗抖动。
  assert.ok(elapsed < 700, `3 个 300ms 调用耗时 ${elapsed}ms，说明仍在同一连接上串行`)
})

test('pool: tool-level error is NOT retried through single-shot spawn', async () => {
  stubEnv({ ARGO_STUB_TOOL_ERROR: '1' })
  const spawned = []
  const tool = makeNativeTool(
    { searchCommand: process.execPath, searchArgs: [STUB_SERVER] },
    'argo_search',
    { mcpCall: nativeViaMcpPool, run: async (spec) => { spawned.push(spec); return 'unreachable' } },
  )
  await assert.rejects(() => tool.execute({ query: 'q' }), /stub tool failed/)
  assert.equal(spawned.length, 0, '工具级错误不得回退单发（那是双倍引擎调用与配额）')
  assert.equal(spawnCount(), 1, '工具级错误不得新起进程重跑')
})

test('pool: transport failure still falls back to single-shot spawn', async () => {
  const spawned = []
  const tool = makeNativeTool(
    { searchCommand: process.execPath, searchArgs: [join(STUB_DIR, 'does-not-exist.js')] },
    'argo_search',
    {
      mcpCall: nativeViaMcpPool,
      run: async (spec) => {
        spawned.push(spec)
        return JSON.stringify({ content: [{ type: 'text', text: 'via-spawn' }] })
      },
    },
  )
  const value = await tool.execute({ query: 'q' })
  assert.equal(spawned.length, 1, '连接建立失败应回退单发一次，功能不丢')
  assert.equal(tool.output.render({}, value)[0].text, 'via-spawn')
})

test('pool: disposeSharedMcp closes every connection; next call rebuilds', async () => {
  stubEnv()
  const tool = stubTool({ searchConcurrency: 2 })
  await Promise.all([tool.execute({ query: 'a' }), tool.execute({ query: 'b' })])
  const before = spawnCount()
  assert.ok(before >= 1)
  disposeSharedMcp()
  await tool.execute({ query: 'c' })
  assert.equal(spawnCount(), before + 1, 'dispose 后池应为空，下次调用重新建立连接')
})
