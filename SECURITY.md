# Security

This bot places orders with exchange API keys. Please report anything that could leak a key or Telegram token,
bypass the live gate (`MODE=live` + `LIVE_CONFIRM`), use live keys outside live mode, skip the risk manager or
send an order that was not intended **privately** through
[GitHub security advisories](https://github.com/thanatchon36/goldshop-mm/security/advisories/new), not as a
public issue.

Keys are read only from the environment, `.env` or Docker secrets, and `.env` is git-ignored. Never paste keys,
secrets or account identifiers into issues, PRs or logs you share.
