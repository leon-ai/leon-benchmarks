import fs from 'node:fs'
import path from 'node:path'

const workspace = process.env.PINCHBENCH_WORKSPACE
const codebase = process.env.LEON_CODEBASE_PATH
const runtimeIndex = process.argv.indexOf('--runtime')
const isToolWorker = runtimeIndex >= 0 && process.argv[runtimeIndex + 1] === 'tool'

if (isToolWorker && workspace && codebase) {
  const originalChdir = process.chdir.bind(process)

  // Native workers reset their directory to the codebase. Task-relative files
  // must instead resolve inside the same isolated workspace used by every agent.
  process.chdir = (directory) => {
    const target = path.resolve(directory) === path.resolve(codebase)
      ? workspace
      : directory

    originalChdir(target)

    if (target === workspace) {
      fs.appendFileSync('/logs/agent/worker-cwd.jsonl', JSON.stringify({
        session_id: process.env.LEON_SESSION_ID,
        toolkit: process.argv[process.argv.indexOf('--toolkit') + 1],
        tool: process.argv[process.argv.indexOf('--tool') + 1],
        cwd: process.cwd()
      }) + '\n')
    }
  }
}
