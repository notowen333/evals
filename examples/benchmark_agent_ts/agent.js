/**
 * Strands benchmark agent (TypeScript/JS) for Harbor (Terminal-Bench).
 *
 * Run with:
 *   strands-evals benchmark --runtime typescript ./examples/benchmark_agent_ts --dataset NovitaAI/tb21-file-recovery
 */

import { Agent, BedrockModel, tool } from '@strands-agents/sdk'
import { z } from 'zod'
import { execSync } from 'node:child_process'
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { dirname } from 'node:path'

const SYSTEM_PROMPT = `You are an expert software engineer working in a sandboxed Linux container.
You will be given a task. Complete it by modifying files and running commands.

## Workflow

1. **Orient** — Before doing anything, understand the environment:
   - Run \`pwd\` to confirm your working directory (usually /app)
   - Run \`find . -type f | head -50\` to see the project structure
   - Read the key files relevant to the task

2. **Understand the goal** — Read any test files or verification scripts to understand
   exactly what success looks like. The verifier will run tests against your changes.

3. **Plan** — Think about what changes are needed. Consider edge cases.

4. **Implement** — Make focused changes using shell commands or write_file.

5. **Verify** — Run the project's tests or check your output.
   If tests fail, read the error, fix it, re-run.

6. **Iterate** — Keep going until tests pass. Don't stop after one attempt.

## Rules

- Always \`pwd\` first — don't assume you're in /app
- Always read a file before editing it
- Write to ABSOLUTE paths (e.g. /app/output.txt, not output.txt)
- After writing a file, verify it exists: \`ls -la /path/to/file\`
- If a command fails, read the error — don't just retry the same thing
- If tests reference specific paths, use those exact paths`

const shell = tool({
  name: 'shell',
  description: 'Execute a bash command and return its output.',
  inputSchema: z.object({
    command: z.string().describe('The bash command to run'),
  }),
  callback: (input) => {
    try {
      const output = execSync(input.command, { encoding: 'utf8', timeout: 180000 })
      return output || '(no output)'
    } catch (err) {
      return `Error (exit ${err.status}): ${err.stderr || err.message}`
    }
  },
})

const readFile = tool({
  name: 'read_file',
  description: 'Read the contents of a file.',
  inputSchema: z.object({
    path: z.string().describe('Absolute path to the file'),
  }),
  callback: (input) => {
    try {
      return readFileSync(input.path, 'utf8')
    } catch (err) {
      return `Error reading ${input.path}: ${err.message}`
    }
  },
})

const writeFile = tool({
  name: 'write_file',
  description: 'Write content to a file, creating parent directories as needed.',
  inputSchema: z.object({
    path: z.string().describe('Absolute path to the file'),
    content: z.string().describe('Content to write'),
  }),
  callback: (input) => {
    try {
      mkdirSync(dirname(input.path), { recursive: true })
      writeFileSync(input.path, input.content)
      return 'ok'
    } catch (err) {
      return `Error writing ${input.path}: ${err.message}`
    }
  },
})

export async function createAgent() {
  const modelId = process.env.STRANDS_MODEL || 'us.anthropic.claude-sonnet-4-6'
  const model = new BedrockModel({ modelId })

  return new Agent({
    model,
    systemPrompt: SYSTEM_PROMPT,
    tools: [shell, readFile, writeFile],
    printer: false,
  })
}
