export function nativeToolArguments(toolInput: {
  toolId: string
  functionName?: string
  toolInput?: string
  parsedInput?: Record<string, unknown>
}, requestedInputs?: string[]): Promise<Record<string, unknown>>
