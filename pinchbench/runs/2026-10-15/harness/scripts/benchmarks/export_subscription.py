"""Borrow Hermes subscription access without copying its rotating refresh token."""

import json
import os
import sys
from pathlib import Path

from hermes_cli.auth import resolve_codex_runtime_credentials
from hermes_cli.auth_constants import _decode_jwt_claims

MINIMUM_VALIDITY_SECONDS = 1_800
ACCOUNT_CLAIM = "https://api.openai.com/auth"


def main() -> None:
    """Let Hermes own refreshes and export only a bounded trial authorization."""
    credentials = resolve_codex_runtime_credentials(
        refresh_skew_seconds=MINIMUM_VALIDITY_SECONDS
    )
    if credentials.get("auth_mode") != "chatgpt":
        raise RuntimeError("Hermes must use its ChatGPT subscription")
    token = credentials["api_key"]
    claims = _decode_jwt_claims(token)
    account = claims.get(ACCOUNT_CLAIM, {}).get("chatgpt_account_id")
    if not account:
        raise RuntimeError("Hermes subscription authorization lacks a workspace")
    output = Path(sys.argv[1])
    output.write_text(
        json.dumps(
            {
                "hermes": {
                    "access_token": token,
                    "chatgpt_account_id": account,
                    "expires_at": claims["exp"] * 1_000,
                    "base_url": credentials["base_url"],
                }
            }
        )
    )
    os.chmod(output, 0o600)


if __name__ == "__main__":
    main()
