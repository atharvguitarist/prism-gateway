# Security policy

Prism is a capstone project and ships with **demo credentials** (`prism-admin-demo`, the seeded `prism-sk-*` keys). Do not expose it to the internet as-is.

## Before any non-local use

- Set `PRISM_ADMIN_TOKEN` to a long random value; the console and `/admin/*` rely on it alone.
- Replace or delete `data/seed_keys.json`, and issue keys through `POST /admin/keys`.
- Supply real provider credentials only via `PRISM_PROVIDER_<NAME>_API_KEY` environment variables; they are never returned, logged, or included in `repr()`, and a test asserts it.
- Terminate TLS in front of the gateway.
- Run a single worker (rate-limit state is in process memory).

## What the gateway deliberately does not store

Prompt and response bodies are not written to the request log. Unknown virtual keys presented by callers are not persisted (they may be mistyped real secrets). Cached answers are stored per tenant and never served across tenants.

## Reporting

Open a private security advisory on this repository (Security → Report a vulnerability) or contact the maintainer through the GitHub profile.
