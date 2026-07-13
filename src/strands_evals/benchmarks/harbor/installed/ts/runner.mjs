/**
 * In-container entrypoint that runs a user's Strands TypeScript agent.
 *
 * Uploaded into the Harbor container and executed as:
 *
 *   node /installed-agent/runner.mjs \
 *       --agent ./agent.js \
 *       --instruction "..." \
 *       --output /logs/agent/result.json
 *
 * Imports the user's agent (via createAgent()), invokes it with the instruction,
 * and writes token metrics + conversation to files that Harbor downloads post-run.
 *
 * This file depends ONLY on @strands-agents/sdk (installed in the container).
 */

import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { join, dirname, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { parseArgs } from 'node:util'

const { values: args } = parseArgs({
  options: {
    agent: { type: 'string' },
    instruction: { type: 'string' },
    output: { type: 'string' },
    'max-turns': { type: 'string' },
  },
})

if (!args.agent || !args.instruction || !args.output) {
  console.error('Usage: node runner.mjs --agent <path> --instruction <text> --output <path>')
  process.exit(2)
}

const outputPath = resolve(args.output)
await mkdir(dirname(outputPath), { recursive: true })

async function writeResult(data) {
  await writeFile(outputPath, JSON.stringify(data, null, 2))
}

// Map Strands' accumulatedUsage to Harbor's token fields. Strands reports four
// mutually-exclusive counts (uncached input, cache read, cache write, output);
// Harbor's n_input_tokens is defined as "including cache", so input_tokens is
// the full input side and cache_tokens is all cache activity. Raw usage is
// still dumped alongside for the exact read/write split.
function tokenFields(usage) {
  usage = usage ?? {}
  const uncached = usage.inputTokens ?? 0
  const cacheRead = usage.cacheReadInputTokens ?? 0
  const cacheWrite = usage.cacheWriteInputTokens ?? 0
  return {
    input_tokens: uncached + cacheRead + cacheWrite,
    output_tokens: usage.outputTokens ?? null,
    cache_tokens: cacheRead + cacheWrite,
  }
}

// Import the user's agent module
let agentModule
try {
  const agentPath = resolve(args.agent)
  agentModule = await import(pathToFileURL(agentPath).href)
} catch (err) {
  console.error('Failed to import agent module:', err)
  await writeResult({ error: String(err), stop_reason: 'import_error' })
  process.exit(1)
}

// Get the agent instance via createAgent()
const createAgent = agentModule.createAgent
if (typeof createAgent !== 'function') {
  const msg = `Agent module must export a createAgent() function`
  console.error(msg)
  await writeResult({ error: msg, stop_reason: 'import_error' })
  process.exit(1)
}

const agent = await createAgent()

// Check for optional custom invoke function
const invokeAgent = typeof agentModule.invokeAgent === 'function' ? agentModule.invokeAgent : null

// Build invoke options
const invokeOptions = {}
if (args['max-turns']) {
  const turns = parseInt(args['max-turns'], 10)
  if (!isNaN(turns) && turns > 0) {
    invokeOptions.limits = { turns }
  }
}

// Run the agent
let result
try {
  if (invokeAgent) {
    result = await invokeAgent(agent, args.instruction, invokeOptions)
  } else {
    result = await agent.invoke(args.instruction, invokeOptions)
  }
} catch (err) {
  console.error('Agent invocation failed:', err)
  const metrics = agent.metrics
  const usage = metrics?.accumulatedUsage ?? {}
  await writeResult({
    error: String(err),
    stop_reason: 'error',
    ...tokenFields(usage),
    cycle_count: metrics?.cycleCount ?? null,
    accumulated_usage: usage,
  })
  // Write partial conversation
  await writeConversation(agent)
  process.exit(1)
}

// Success — write full metrics
const metrics = result.metrics ?? agent.metrics
const usage = metrics?.accumulatedUsage ?? {}

// Strands SDK version
let strandsVersion = null
try {
  const pkgPath = join(dirname(resolve(args.agent)), 'node_modules', '@strands-agents', 'sdk', 'package.json')
  const pkg = JSON.parse(await readFile(pkgPath, 'utf8'))
  strandsVersion = pkg.version
} catch {
  // best-effort
}

// Per-tool usage stats
const toolStats = {}
const rawToolMetrics = metrics?.toolMetrics ?? {}
for (const [name, tm] of Object.entries(rawToolMetrics)) {
  toolStats[name] = {
    call_count: tm.callCount,
    success_count: tm.successCount,
    error_count: tm.errorCount,
    total_time: Math.round(tm.totalTime) / 1000, // ms -> seconds
  }
}

await writeResult({
  ...tokenFields(usage),
  stop_reason: result.stopReason ?? null,
  cycle_count: metrics?.cycleCount ?? null,
  accumulated_usage: usage,
  strands_version: strandsVersion,
  tool_stats: toolStats,
})

// Write conversation for ATIF trajectory conversion
await writeConversation(agent)

// Capture git patch of all changes the agent made
await capturePatch()

async function capturePatch() {
  // The task repo is NOT at the runner's cwd — it lives at the task workdir
  // (e.g. /testbed for SWE-bench, /app). Probe common locations plus any git
  // repo near the root, and diff the first with uncommitted changes.
  const { execSync } = await import('node:child_process')
  const diffRepo = (repo) => {
    try {
      execSync('git add -AN', { cwd: repo, timeout: 15000 })  // -N so new files show
      const out = execSync('git diff HEAD', { cwd: repo, encoding: 'utf8', timeout: 30000 })
      return out.trim() ? out : null
    } catch { return null }
  }
  const candidates = ['/testbed', '/app', process.cwd()]
  try {
    const found = execSync('find / -maxdepth 3 -name .git -type d 2>/dev/null | head -5',
      { encoding: 'utf8', timeout: 20000, shell: '/bin/bash' })
    for (const p of found.split('\n')) {
      if (p.endsWith('/.git')) candidates.push(p.slice(0, -5))
    }
  } catch { /* best-effort */ }
  const seen = new Set()
  for (const repo of candidates) {
    if (!repo || seen.has(repo)) continue
    seen.add(repo)
    const diff = diffRepo(repo)
    if (diff) {
      await writeFile(join(dirname(outputPath), 'patch.diff'), diff)
      return
    }
  }
}

async function writeConversation(agent) {
  try {
    const conversationPath = join(dirname(outputPath), 'conversation.json')
    const messages = agent.messages?.map(m => m.toJSON?.() ?? m) ?? []
    await writeFile(conversationPath, JSON.stringify(messages, null, 2))
  } catch {
    // best-effort
  }
}
