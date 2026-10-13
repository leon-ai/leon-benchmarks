import fs from 'node:fs/promises'

/**
 * Preserve model-requested shell arguments before Core's script transport.
 * Execution keeps the original input object; only exported grading evidence changes.
 */
export async function nativeToolArguments(toolInput, requestedInputs = []) {
  const executionArguments = toolInput.parsedInput || { input: toolInput.toolInput || '' }

  if (toolInput.toolId === 'shell' && toolInput.functionName === 'executeCommand') {
    try {
      const script = await fs.readFile(executionArguments.command, 'utf8')
      const matches = requestedInputs
        .map((input) => ({ input, parsed: JSON.parse(input) }))
        .filter(({ parsed }) => typeof parsed.command === 'string' && script.endsWith(parsed.command + '\n'))
        .sort((left, right) => right.parsed.command.length - left.parsed.command.length)

      if (matches.length > 0) {
        const match = matches[0]
        requestedInputs.splice(requestedInputs.indexOf(match.input), 1)

        return { ...executionArguments, command: match.parsed.command }
      }
    } catch {
      // Invalid native input remains visible without inventing a command.
    }
  }

  return executionArguments
}
