import fs from 'node:fs/promises'

const MINIMUM_VALIDITY_MS = 1_800_000

/**
 * Borrow a short-lived access token from Leon's selected subscription account.
 * Refresh tokens and owner profile state remain with the native connection store.
 */
async function main(): Promise<void> {
  const outputPath = process.argv[2]
  process.argv = process.argv.slice(0, 2)

  if (!outputPath) {
    throw new Error('Expected a private subscription export path')
  }

  const { CONFIG_MANAGER } = await import('../../server/src/config')
  const { MODEL_ACCOUNT_STORE } = await import('../../server/src/core/llm-manager/llm-accounts')
  const { requireChatGPTCodexAccount } = await import('../../server/src/core/llm-manager/llm-accounts/chatgpt-account-config')
  const account = CONFIG_MANAGER.getProviderConfig('openai')?.account

  if (!account) {
    throw new Error('Leon must have a selected ChatGPT subscription account')
  }

  let credentials = await MODEL_ACCOUNT_STORE.getCredentials(account)
  if (!credentials) {
    throw new Error('Leon could not read its selected subscription account')
  }
  requireChatGPTCodexAccount(credentials, account)

  if (Number(credentials['expires_at']) <= Date.now() + MINIMUM_VALIDITY_MS) {
    credentials = await MODEL_ACCOUNT_STORE.getCredentials(account, undefined, true)
  }
  if (!credentials) {
    throw new Error('Leon could not refresh its selected subscription account')
  }
  requireChatGPTCodexAccount(credentials, account)

  const payload = JSON.parse(await fs.readFile(outputPath, 'utf8')) as Record<string, unknown>
  payload['leon'] = {
    auth_kind: credentials['auth_kind'],
    auth_flow: credentials['auth_flow'],
    access_token: credentials['access_token'],
    chatgpt_account_id: credentials['chatgpt_account_id'],
    expires_at: credentials['expires_at']
  }
  await fs.writeFile(outputPath, JSON.stringify(payload), { mode: 0o600 })
}

void main().catch(() => {
  console.error('Could not export Leon subscription authorization')
  process.exitCode = 1
})
