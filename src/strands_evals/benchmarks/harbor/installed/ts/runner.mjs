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
    input_tokens: usage.inputTokens ?? null,
    output_tokens: usage.outputTokens ?? null,
    cache_tokens: usage.cacheReadInputTokens ?? null,
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
  input_tokens: usage.inputTokens ?? null,
  output_tokens: usage.outputTokens ?? null,
  cache_tokens: usage.cacheReadInputTokens ?? null,
  stop_reason: result.stopReason ?? null,
  cycle_count: metrics?.cycleCount ?? null,
  accumulated_usage: usage,
  strands_version: strandsVersion,
  tool_stats: toolStats,
})

// Write conversation for ATIF trajectory conversion
await writeConversation(agent)

async function writeConversation(agent) {
  try {
    const conversationPath = join(dirname(outputPath), 'conversation.json')
    const messages = agent.messages?.map(m => m.toJSON?.() ?? m) ?? []
    await writeFile(conversationPath, JSON.stringify(messages, null, 2))
  } catch {
    // best-effort
  }
}
