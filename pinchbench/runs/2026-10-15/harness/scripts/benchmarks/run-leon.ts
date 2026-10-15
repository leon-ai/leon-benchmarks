import fs from 'node:fs/promises'
import { appendFileSync } from 'node:fs'
import path from 'node:path'
import { randomUUID } from 'node:crypto'
import { once } from 'node:events'

import YAML from 'yaml'

import { observeInference } from './inference-observer.mjs'
import { nativeToolArguments } from './native-tool-arguments.mjs'

const RESULT_FILENAME = 'leon-result.json'
const JOURNAL_FILENAME = 'trajectory-events.jsonl'
const PROFILE_NAME = 'benchmark'
const DEFAULT_MODEL = 'openai/gpt-6.1-sol'
const DEFAULT_REASONING = 'high'
const DEFAULT_SPEED = 'fast'

interface TrajectoryStep {
  step_id: number
  source: 'user' | 'agent'
  message: string
  timestamp: string
  tool_calls?: {
    tool_call_id: string
    function_name: string
    arguments: Record<string, unknown>
  }[]
  observation?: {
    results: { source_call_id: string, content: string }[]
  }
}

/**
 * Run a benchmark instruction through Leon's production agent and tool workers.
 * The caller supplies a fresh, container-owned home for each independent trial.
 */
async function main(): Promise<void> {
  const instructionPath = process.argv[2]
  const outputDirectory = process.argv[3]
  const leonHome = process.env['LEON_HOME']

  if (!instructionPath || !outputDirectory || !leonHome) {
    throw new Error('Expected instruction path, output directory, and LEON_HOME')
  }

  const model = process.env['LEON_BENCHMARK_MODEL'] || DEFAULT_MODEL
  const reasoning = process.env['LEON_BENCHMARK_REASONING'] || DEFAULT_REASONING
  const speed = process.env['LEON_BENCHMARK_SPEED'] || DEFAULT_SPEED
  const profilePath = path.join(leonHome, 'profiles', PROFILE_NAME)
  const input = await fs.readFile(instructionPath, 'utf8')

  // Bridge modules parse their own worker arguments when imported by Core.
  // Remove this runner's paths so they cannot be mistaken for skill intents.
  process.argv = process.argv.slice(0, 2)

  process.env['LEON_PROFILE'] = PROFILE_NAME
  process.env['LEON_LLM'] = model
  process.env['LEON_NODE_ENV'] = 'testing'

  await fs.mkdir(profilePath, { recursive: true })
  await fs.mkdir(outputDirectory, { recursive: true })
  await fs.writeFile(path.join(profilePath, 'config.yml'), YAML.stringify({
    routing: { mode: 'agent' },
    runtime: { pulse_enabled: false },
    telemetry_enabled: false,
    llm: {
      default: model,
      providers: { openai: { account: 'openai.benchmark' } },
      model_settings: { [model]: { reasoning, speed } }
    }
  }))

  const { getProfilePaths } = await import('../../server/src/core/profile-runtime/profile-paths')
  const { CACHE_PATH, LEON_TOOLKITS_PATH, MODELS_PATH, TMP_PATH } = await import('../../server/src/constants')
  const paths = getProfilePaths()

  // Runtime imports stay anchored to Leon's codebase while the agent process
  // follows the task environment's working directory.
  if (process.env['LEON_BENCHMARK_WORKDIR']) {
    process.chdir(process.env['LEON_BENCHMARK_WORKDIR'])
  }

  await Promise.all([
    paths.context, paths.memory, paths.logs, paths.sessions,
    paths.nativeSkills, paths.agentSkills, paths.tools,
    CACHE_PATH, LEON_TOOLKITS_PATH, MODELS_PATH, TMP_PATH
  ].map((directory) => fs.mkdir(directory, { recursive: true })))

  const { default: generateProfileToken } = await import('../generate/generate-profile-token.js')
  await generateProfileToken()

  const settings = JSON.parse(
    await fs.readFile(new URL('./pilot.json', import.meta.url), 'utf8')
  ) as { task_skills_dir: string, service_tier: string }
  const taskSkillsExist = await fs.stat(settings.task_skills_dir)
    .then((entry) => entry.isDirectory())
    .catch(() => false)

  // Benchmark guidance must be discoverable through Leon's native skill loader.
  if (taskSkillsExist) {
    await fs.cp(settings.task_skills_dir, paths.agentSkills, { recursive: true })
  }

  const { SkillDomainHelper } = await import('../../server/src/helpers/skill-domain-helper')
  const discoveredSkills = SkillDomainHelper.listProfileSkillDescriptorsSync()
  const taskSkillFolders = taskSkillsExist
    ? await fs.readdir(settings.task_skills_dir, { withFileTypes: true })
    : []
  const requiredSkillPaths: string[] = []

  for (const folder of taskSkillFolders) {
    if (!folder.isDirectory()) {
      continue
    }

    const guidePath = path.join(settings.task_skills_dir, folder.name, 'SKILL.md')
    const hasGuide = await fs.stat(guidePath)
      .then((entry) => entry.isFile())
      .catch(() => false)

    if (hasGuide) {
      requiredSkillPaths.push(path.join(paths.agentSkills, folder.name))
    }
  }

  // Copied task guidance must be visible to the native loader before inference.
  const undiscoveredSkills = requiredSkillPaths.filter(
    (skillPath) => !discoveredSkills.some((skill) => skill.path === skillPath)
  )

  await fs.writeFile(path.join(outputDirectory, 'task-skills.json'), JSON.stringify({
    registered: taskSkillsExist,
    source: settings.task_skills_dir,
    target: paths.agentSkills,
    discovered: discoveredSkills.map((skill) => ({ id: skill.id, path: skill.path })),
    undiscovered: undiscoveredSkills
  }, null, 2))

  if (undiscoveredSkills.length > 0) {
    throw new Error('Benchmark task guidance is not discoverable by the native loader')
  }

  const subscriptionPath = process.env['LEON_BENCHMARK_SUBSCRIPTION_PATH']
  if (!subscriptionPath) {
    throw new Error('A ChatGPT subscription authorization is required')
  }
  const subscription = JSON.parse(await fs.readFile(subscriptionPath, 'utf8')) as {
    leon: Record<string, unknown>
  }
  const { MODEL_ACCOUNT_STORE } = await import('../../server/src/core/llm-manager/llm-accounts')
  await MODEL_ACCOUNT_STORE.save({
    provider: 'openai.benchmark',
    auth_type: 'oauth',
    credentials: subscription.leon
  })

  // Core's image endpoints use global fetch rather than its chat transport.
  // Enforce the same subscription policy while keeping media usage separate.
  process.env['LEON_BENCHMARK_REASONING_AUDITED'] = '1'
  await import('./audit-openai.mjs')

  const { OpenAIResponsesTransport } = await import('../../server/src/core/llm-manager/llm-providers/openai-responses-transport')
  const originalFetch = OpenAIResponsesTransport.prototype.fetch

  // Keep auxiliary inference, including compaction, at the declared settings.
  // Prompts and the native protocol remain intact; credentials are never recorded.
  OpenAIResponsesTransport.prototype.fetch = async function (request, init): Promise<Response> {
    const payload = JSON.parse(String(init?.body || '{}')) as Record<string, unknown>

    if (!String(request).startsWith('https://chatgpt.com/backend-api/codex/')) {
      throw new Error('Benchmark inference requires the ChatGPT subscription route')
    }

    if (payload['model'] !== model.split('/')[1]) {
      throw new Error('Benchmark inference cannot substitute another model')
    }

    if (payload['model'] === model.split('/')[1]) {
      payload['reasoning'] = {
        ...(payload['reasoning'] as Record<string, unknown> || {}),
        effort: reasoning
      }
      payload['service_tier'] = settings.service_tier
      init = { ...init, body: JSON.stringify(payload) }
    }

    return observeInference(
      () => originalFetch.call(this, request, init),
      payload,
      String(request),
      path.join(outputDirectory, 'requests.jsonl')
    )
  }

  const { CONVERSATION_LOGGER, HTTP_SERVER, LLM_PROVIDER, TOOL_EXECUTOR, TOOL_WORKER_MANAGER } = await import('../../server/src/core')
  const { ReActLLMDuty } = await import('../../server/src/core/llm-manager/llm-duties/react-llm-duty')
  const { runWithConversationSession } = await import('../../server/src/core/session-manager/session-context')
  const { CONVERSATION_SESSION_MANAGER } = await import('../../server/src/core/session-manager')
  const { readInferenceUsage } = await import('../../server/src/core/llm-manager/llm-usage/usage-ledger')
  const sessionId = CONVERSATION_SESSION_MANAGER.createSession().id
  const startedAt = Date.now()
  const steps: TrajectoryStep[] = [{
    step_id: 1,
    source: 'user',
    message: input,
    timestamp: new Date(startedAt).toISOString()
  }]
  const originalExecuteTool = TOOL_EXECUTOR.executeTool.bind(TOOL_EXECUTOR)
  const requestedShellInputs: string[] = []
  type ExecuteTool = typeof TOOL_EXECUTOR.executeTool
  const journalPath = path.join(outputDirectory, JOURNAL_FILENAME)

  // Persist each event before the native deadline can terminate the process.
  // Append-only records avoid repeatedly writing growing tool responses.
  await fs.writeFile(journalPath, JSON.stringify({
    type: 'metadata',
    session_id: sessionId,
    agent: { name: 'leon', version: process.env['LEON_BENCHMARK_VERSION'] || 'source', model_name: model }
  }) + '\n')
  await fs.appendFile(journalPath, JSON.stringify({ type: 'step', step: steps[0] }) + '\n')

  // Preserve native function names so graders can distinguish each agent's
  // actual integrations from work performed through a general shell.
  TOOL_EXECUTOR.executeTool = async (
    toolInput: Parameters<ExecuteTool>[0]
  ): Promise<Awaited<ReturnType<ExecuteTool>>> => {
    const callId = randomUUID()
    const step: TrajectoryStep = {
      step_id: steps.length + 1,
      source: 'agent',
      message: '',
      timestamp: new Date().toISOString(),
      tool_calls: [{
        tool_call_id: callId,
        function_name: [toolInput.toolkitId, toolInput.toolId, toolInput.functionName]
          .filter(Boolean).join('.'),
        arguments: await nativeToolArguments(toolInput, requestedShellInputs)
      }]
    }

    steps.push(step)
    await fs.appendFile(journalPath, JSON.stringify({ type: 'step', step }) + '\n')

    const result = await originalExecuteTool(toolInput)
    step.observation = {
      results: [{ source_call_id: callId, content: JSON.stringify(result) }]
    }
    await fs.appendFile(journalPath, JSON.stringify({
      type: 'observation', step_id: step.step_id, observation: step.observation
    }) + '\n')

    return result
  }

  try {
    if (!await LLM_PROVIDER.init()) {
      throw new Error(`Could not initialize ${model}`)
    }

    // Ordinary tools resolve profile-owned provider requests through Core's
    // authenticated service, including subscription-backed hosted search.
    const listening = once(HTTP_SERVER.httpServer, 'listening')
    await HTTP_SERVER.init()
    await listening

    const result = await runWithConversationSession({ sessionId }, async () => {
      await CONVERSATION_LOGGER.push({
        who: 'owner', message: input, isAddedToHistory: true
      })

      const duty = new ReActLLMDuty({
        input,
        onProgressEvent: (event): void => {
          if (
            event.type === 'tool_call' &&
            event.toolCall.name.endsWith('.shell.executeCommand') &&
            event.toolCall.status === 'running' &&
            typeof event.toolCall.input === 'string'
          ) {
            requestedShellInputs.push(event.toolCall.input)
          }

          if (event.type !== 'progress_message') {
            return
          }

          // Native public messages are grading evidence even when a turn ends
          // before its final answer. Persist them without exposing reasoning.
          const step: TrajectoryStep = {
            step_id: steps.length + 1,
            source: 'agent',
            message: event.message.content,
            timestamp: new Date(event.message.createdAt).toISOString()
          }

          steps.push(step)
          appendFileSync(journalPath, JSON.stringify({ type: 'step', step }) + '\n')
        }
      })
      await duty.init({ force: true })

      return duty.execute()
    })

    steps.push({
      step_id: steps.length + 1,
      source: 'agent',
      message: typeof result?.output === 'string'
        ? result.output
        : JSON.stringify(result?.output || ''),
      timestamp: new Date().toISOString()
    })

    const usage = []
    for await (const record of readInferenceUsage({ sessionId })) {
      usage.push(record)
    }

    await fs.writeFile(path.join(outputDirectory, RESULT_FILENAME), JSON.stringify({
      model, reasoning, speed, sessionId, startedAt,
      finishedAt: Date.now(), result, usage
    }, null, 2))

    if (!result || result.data?.['finalIntent'] === 'error') {
      throw new Error('Leon did not complete the benchmark turn; see leon-result.json')
    }
  } finally {
    await fs.writeFile(path.join(outputDirectory, 'trajectory.json'), JSON.stringify({
      schema_version: 'ATIF-v1.7',
      session_id: sessionId,
      agent: { name: 'leon', version: process.env['LEON_BENCHMARK_VERSION'] || 'source', model_name: model },
      steps
    }, null, 2))
    await fs.unlink(journalPath)
    await TOOL_WORKER_MANAGER.dispose()
    if (HTTP_SERVER.httpServer.listening) {
      await new Promise<void>((resolve, reject) => {
        HTTP_SERVER.httpServer.close((error) => {
          if (error) {
            reject(error)
            return
          }

          resolve()
        })
      })
    }
    TOOL_EXECUTOR.executeTool = originalExecuteTool
    OpenAIResponsesTransport.prototype.fetch = originalFetch
  }
}

void main().then(() => {
  process.exit(0)
}).catch((error: unknown) => {
  console.error(error)
  process.exit(1)
})
