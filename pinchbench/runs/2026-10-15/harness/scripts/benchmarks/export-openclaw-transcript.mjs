import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

// Read through the pinned runtime's SQLite projection so grading receives the
// same native messages and tool results as the agent, without another inference.
const directory = '/opt/openclaw/node_modules/openclaw/dist'
const filename = fs.readdirSync(directory).find((name) => (
  name.startsWith('session-accessor.sqlite-read-') && name.endsWith('.mjs')
))
const module = await import(pathToFileURL(path.join(directory, filename)).href)
const read = Object.values(module).find((value) => (
  typeof value === 'function' && value.name === 'loadTranscriptEventsSync'
))

if (!read) {
  throw new Error('Pinned OpenClaw native transcript reader is unavailable')
}

const sessions = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))
const rows = []

for (const session of sessions.sessions) {
  rows.push(...read({
    agentId: session.agentId || 'main',
    sessionKey: session.key,
    sessionId: session.sessionId,
    storePath: sessions.path
  }))
}

fs.writeFileSync(process.argv[3], rows.map((row) => JSON.stringify(row)).join('\n') + '\n')
