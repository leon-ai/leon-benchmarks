import fs from 'node:fs'
import path from 'node:path'
import { createHash, randomUUID } from 'node:crypto'

const MAX_ARGUMENT_CHARACTERS = 262_144
const MAX_EVENT_CHARACTERS = 4_194_304
const MAX_IMAGE_RESPONSE_CHARACTERS = 67_108_864
const catalogs = new Set()

/**
 * Record inference metadata while forwarding the original stream unchanged.
 * Observe calls locally so measurement requires no additional model requests.
 */
export async function observeInference(dispatch, payload, endpoint, outputPath) {
  const directory = path.dirname(outputPath)
  const id = randomUUID()
  const started = performance.now()
  const media = outputPath.endsWith('/image-requests.jsonl')
  const eventFilename = media ? 'image-inference-events.jsonl' : 'inference-events.jsonl'
  const append = (filename, value) => {
    fs.appendFileSync(path.join(directory, filename), `${JSON.stringify(value)}\n`)
  }
  const schemas = (payload.tools || []).map((tool) => ({
    name: tool.name || tool.function?.name,
    type: tool.type,
    parameters: tool.parameters || tool.function?.parameters
  }))
  const schemaId = createHash('sha256').update(JSON.stringify(schemas)).digest('hex')

  if (!catalogs.has(schemaId)) {
    append('tool-schemas.jsonl', { schemaId, tools: schemas })
    catalogs.add(schemaId)
  }

  const evidence = {
    inferenceId: id,
    startedAt: new Date().toISOString(),
    model: payload.model,
    reasoning: payload.reasoning,
    serviceTier: payload.service_tier,
    authMode: 'chatgpt_subscription',
    endpoint,
    requestKind: media ? 'image_generation' :
      (payload.tools || []).some((tool) => tool.type === 'image_generation')
        ? 'hosted_image_generation' : 'reasoning',
    schemaId,
    status: 0,
    completed: false,
    observerMilliseconds: 0
  }
  append(eventFilename, { inferenceId: id, event: 'started', at: evidence.startedAt })
  const decoder = new TextDecoder()
  let pending = ''
  let saved = false
  const finish = () => {
    if (!saved) {
      saved = true
      evidence.elapsedSeconds = (performance.now() - started) / 1_000

      if (media && !evidence.oversizedEvents) {
        try {
          const result = JSON.parse(pending)
          evidence.completed = evidence.status >= 200 && evidence.status < 300 &&
            Array.isArray(result.data) && result.data.length > 0 && !result.error
          evidence.imageCount = Array.isArray(result.data) ? result.data.length : 0
          evidence.usage = result.usage
          evidence.returnedModel = result.model
          evidence.reportedQuality = result.quality
          evidence.reportedSize = result.size
          evidence.responseSeconds = evidence.elapsedSeconds
        } catch {
          evidence.unparsedEvents = (evidence.unparsedEvents || 0) + 1
        }
      }

      fs.appendFileSync(outputPath, `${JSON.stringify(evidence)}\n`)
    }
  }
  let response

  try {
    response = await dispatch()
  } catch (error) {
    evidence.errorType = error?.name || 'TransportError'
    finish()
    throw error
  }

  evidence.status = response.status
  evidence.headersSeconds = (performance.now() - started) / 1_000

  if (!response.ok || !response.body) {
    finish()
    return response
  }

  const reader = response.body.getReader()
  const names = new Map()
  const inspect = (bytes) => {
    const observing = performance.now()

    // Images endpoints return JSON containing binary data. Preserve it for the
    // native consumer, but retain only usage and delivery metadata in the audit.
    if (media) {
      if (!evidence.oversizedEvents) {
        pending += decoder.decode(bytes, { stream: true })

        if (pending.length > MAX_IMAGE_RESPONSE_CHARACTERS) {
          pending = ''
          evidence.oversizedEvents = 1
        }
      }

      evidence.observerMilliseconds += performance.now() - observing
      return
    }

    pending += decoder.decode(bytes, { stream: true })
    let boundary = pending.indexOf('\n')

    while (boundary >= 0) {
      const line = pending.slice(0, boundary).trim()
      pending = pending.slice(boundary + 1)

      if (line.startsWith('data: ') && line !== 'data: [DONE]') {
        try {
          const event = JSON.parse(line.slice(6))
          const seconds = (performance.now() - started) / 1_000

          if (event.type === 'response.output_text.delta') {
            evidence.firstTextSeconds ??= seconds
          }

          if (event.type === 'response.output_item.added' && event.item?.type === 'function_call') {
            names.set(event.item.id, event.item)
          }

          if (event.type === 'response.function_call_arguments.done') {
            const item = names.get(event.item_id) || {}
            evidence.firstToolActionSeconds ??= seconds
            append('model-tool-calls.jsonl', {
              inferenceId: id,
              schemaId,
              callId: item.call_id,
              name: event.name || item.name,
              arguments: event.arguments?.length <= MAX_ARGUMENT_CHARACTERS ? event.arguments : null,
              argumentsOmitted: event.arguments?.length > MAX_ARGUMENT_CHARACTERS,
              readySeconds: seconds
            })
          }

          if (['response.completed', 'response.failed', 'response.incomplete'].includes(event.type)) {
            evidence.completed = event.type === 'response.completed'
            evidence.terminalEvent = event.type
            evidence.returnedModel = event.response?.model
            evidence.returnedServiceTier = event.response?.service_tier
            evidence.usage = event.response?.usage
            evidence.toolUsage = event.response?.tool_usage
            evidence.responseSeconds = seconds
          }
        } catch {
          evidence.unparsedEvents = (evidence.unparsedEvents || 0) + 1
        }
      }

      boundary = pending.indexOf('\n')
    }

    if (pending.length > MAX_EVENT_CHARACTERS) {
      pending = ''
      evidence.oversizedEvents = (evidence.oversizedEvents || 0) + 1
    }

    evidence.observerMilliseconds += performance.now() - observing
  }
  const body = new ReadableStream({
    async pull(controller) {
      try {
        const next = await reader.read()

        if (next.done) {
          finish()
          controller.close()
          return
        }

        inspect(next.value)
        controller.enqueue(next.value)
      } catch (error) {
        evidence.errorType = error?.name || 'StreamError'
        finish()
        controller.error(error)
      }
    },
    async cancel(reason) {
      evidence.canceled = true
      finish()
      await reader.cancel(reason)
    }
  })

  return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers })
}
