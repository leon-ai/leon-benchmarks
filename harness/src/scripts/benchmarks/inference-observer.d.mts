/**
 * Passively observe a Responses stream without changing its payload or bytes.
 */
export declare function observeInference(
  dispatch: () => Promise<Response>,
  payload: Record<string, unknown>,
  endpoint: string,
  outputPath: string
): Promise<Response>
