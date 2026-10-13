import fs from 'node:fs'
import { zstdDecompressSync } from 'node:zlib'
import { observeInference } from './inference-observer.mjs'

const originalFetch = globalThis.fetch
const OUTPUT_PATH = process.env.LEON_BENCHMARK_AUDIT_PATH || '/logs/agent/requests.jsonl'
const OPENAI_URL = 'https://api.openai.com/v1/'
const SUBSCRIPTION_URL = 'https://chatgpt.com/backend-api/codex/'
const OPENROUTER_URL = 'https://openrouter.ai/'
const settings = JSON.parse(fs.readFileSync(new URL('./pilot.json', import.meta.url), 'utf8'))

/**
 * Keep approved image rendering separate from the campaign's reasoning model.
 */
export function imageRequest(url) {
  return (settings.media_generation?.subscription_endpoints || []).some(
    (endpoint) => url === `${SUBSCRIPTION_URL}${endpoint}`
  )
}

/**
 * Normalize subscription JSON bodies while preserving native fetch options.
 */
export async function inferenceRequest(input, init) {
  const url = typeof input === 'string' ? input : String(input.url || input)

  if (!url.startsWith(SUBSCRIPTION_URL)) {
    return { url, init }
  }

  let body = init?.body
  const encoding = new Headers(init?.headers ?? input.headers).get('content-encoding')

  if (body === undefined && input.body && typeof input.clone === 'function') {
    body = Buffer.from(await input.clone().arrayBuffer())
  }

  if (body != null && typeof body !== 'string') {
    // Guarded native transports can present an already serialized JSON body
    // as a stream. Replace the consumed stream with the same text for dispatch.
    const bytes = Buffer.from(await new Response(body).arrayBuffer())
    init = { ...init, body: bytes }
    body = (encoding === 'zstd' ? zstdDecompressSync(bytes) : bytes).toString('utf8')
  } else if (typeof body === 'string') {
    init = { ...init, body }
  }

  return { url, init, body, encoding }
}

/**
 * Audit the declared inference route without persisting prompts or credentials.
 */
export function auditFetch(fetchImplementation) {
  return async function (input, init) {
    const request = await inferenceRequest(input, init)
    const { url } = request
    init = request.init

    if (url.startsWith(OPENAI_URL) || url.startsWith(OPENROUTER_URL)) {
      throw new Error('Benchmark inference requires the ChatGPT subscription route')
    }

    if (url.startsWith(SUBSCRIPTION_URL) && typeof request.body === 'string') {
      const payload = JSON.parse(request.body)

      const media = imageRequest(url)
      const allowed = media
        ? settings.media_generation.image_models.includes(payload.model)
        : payload.model === settings.model.split('/')[1]

      if (!allowed) {
        throw new Error('Benchmark inference cannot substitute another model')
      }

      for (const tool of payload.tools || []) {
        if (tool.type === 'image_generation' &&
          !settings.media_generation?.image_models.includes(tool.model)) {
          throw new Error('Benchmark image generation requires the approved image model')
        }
      }
    }

    if (url.startsWith(SUBSCRIPTION_URL) && typeof request.body === 'string') {
      const payload = JSON.parse(request.body)
      const media = imageRequest(url)

      // Leon's native Responses transport already records reasoning calls.
      // Observe global image requests without counting those calls twice.
      if (!media && process.env.LEON_BENCHMARK_REASONING_AUDITED === '1') {
        return fetchImplementation.call(this, input, init)
      }

      return observeInference(
        () => fetchImplementation.call(this, input, init),
        payload,
        url,
        media ? OUTPUT_PATH.replace('requests.jsonl', 'image-requests.jsonl') : OUTPUT_PATH
      )
    }

    return fetchImplementation.call(this, input, init)
  }
}

globalThis.fetch = auditFetch(originalFetch)
