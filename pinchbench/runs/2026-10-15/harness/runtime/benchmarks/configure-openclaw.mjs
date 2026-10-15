import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { isMainThread } from 'node:worker_threads'
import { pathToFileURL } from 'node:url'
import { zstdCompressSync } from 'node:zlib'
import { auditFetch, inferenceRequest } from './audit-openai.mjs'

const settings = JSON.parse(fs.readFileSync(new URL('./pilot.json', import.meta.url), 'utf8'))
const auditedFetch = globalThis.fetch
const SUBSCRIPTION_URL = 'https://chatgpt.com/backend-api/codex/'
const SKILLS_EVIDENCE = '/logs/agent/task-skills.json'

// Register task guidance before OpenClaw builds its native skill catalog.
if (process.env.OPENCLAW_STATE_DIR && !fs.existsSync(SKILLS_EVIDENCE)) {
  const target = path.join(process.env.OPENCLAW_STATE_DIR, 'skills')
  const registered = fs.existsSync(settings.task_skills_dir)

  if (registered) {
    fs.cpSync(settings.task_skills_dir, target, { recursive: true })
  }

  fs.writeFileSync(SKILLS_EVIDENCE, JSON.stringify({
    registered,
    source: settings.task_skills_dir,
    target
  }, null, 2))
}

// Apply the declared settings to main and auxiliary inference before auditing.
function configureFetch(fetchImplementation) {
  return async function (input, init) {
    const request = await inferenceRequest(input, init)
    const { url } = request
    init = request.init

    if (url.startsWith(SUBSCRIPTION_URL) && typeof request.body === 'string') {
      const payload = JSON.parse(request.body)
      const hostedImage = payload.tools?.length === 1 &&
        payload.tools[0].type === 'image_generation' &&
        payload.tool_choice?.type === 'image_generation' &&
        settings.media_generation?.image_models.includes(payload.tools[0].model)

      // Native image providers choose a separate host model. The campaign's
      // declared reasoning model also applies to that hosted image request.
      if (hostedImage && payload.model !== settings.model.split('/')[1]) {
        const directory = path.dirname(process.env.LEON_BENCHMARK_AUDIT_PATH ||
          '/logs/agent/requests.jsonl')
        fs.appendFileSync(path.join(directory, 'image-host-routing.jsonl'), `${JSON.stringify({
          nativeModel: payload.model,
          configuredModel: settings.model.split('/')[1],
          imageModel: payload.tools[0].model
        })}\n`)
        payload.model = settings.model.split('/')[1]
      }

      if (payload.model === settings.model.split('/')[1]) {
        payload.service_tier = settings.service_tier
        payload.reasoning = { ...payload.reasoning, effort: settings.reasoning }
        const body = JSON.stringify(payload)
        const headers = new Headers(init?.headers ?? input.headers)
        headers.delete('content-length')
        init = {
          ...init,
          headers,
          body: request.encoding === 'zstd' ? zstdCompressSync(Buffer.from(body)) : body
        }
      }
    }

    return fetchImplementation.call(this, input, init)
  }
}

globalThis.fetch = configureFetch(auditedFetch)

// Native provider transports use their own Undici fetch implementation. Keep
// its dispatcher and stream behavior while applying the same inference audit.
if (process.env.LEON_BENCHMARK_OPENCLAW_ENTRYPOINT) {
  const entrypoint = fs.realpathSync(process.env.LEON_BENCHMARK_OPENCLAW_ENTRYPOINT)
  const require = createRequire(entrypoint)
  const undici = require('undici')

  undici.fetch = configureFetch(auditFetch(undici.fetch))
}

// The native Gateway already inventories model, media, tool and persistence
// work. Observe that inventory so a container outlives background completions.
const activityPath = process.env.LEON_BENCHMARK_GATEWAY_ACTIVITY_PATH

if (activityPath && isMainThread && process.argv[2] === 'gateway' && process.argv[3] === 'run') {
  const entrypoint = fs.realpathSync(process.env.LEON_BENCHMARK_OPENCLAW_ENTRYPOINT)
  const directory = path.join(path.dirname(entrypoint), 'dist')
  const files = fs.readdirSync(directory).filter(
    (filename) => filename.startsWith('gateway-active-work-') && filename.endsWith('.mjs')
  )

  if (files.length !== 1) {
    throw new Error('Native Gateway activity inventory is unavailable')
  }

  const native = await import(pathToFileURL(path.join(directory, files[0])).href)
  const inspect = Object.values(native).find(
    (value) => typeof value === 'function' && value.name === 'createGatewayActiveWorkSnapshot'
  )

  if (!inspect) {
    throw new Error('Native Gateway activity inventory cannot be resolved')
  }

  const peaks = {}
  const observe = () => {
    const snapshot = inspect()
    const temporary = `${activityPath}.part`

    for (const [key, count] of Object.entries(snapshot.counts)) {
      peaks[key] = Math.max(peaks[key] || 0, count)
    }

    fs.writeFileSync(temporary, JSON.stringify({
      observedAt: Date.now(),
      pid: process.pid,
      idle: snapshot.idle,
      counts: snapshot.counts,
      peakCounts: peaks
    }))
    fs.renameSync(temporary, activityPath)
  }

  observe()
  setInterval(observe, settings.media_generation.gateway_activity_poll_ms).unref()
}
