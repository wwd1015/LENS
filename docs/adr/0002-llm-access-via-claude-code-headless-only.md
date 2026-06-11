# All LLM access goes through Claude Code headless mode, never the Anthropic SDK

Every runtime LLM call (wiki ingestion, RCA) shells out to `claude -p` as a
subprocess via `ClaudeCodeClient`; the `anthropic` Python SDK is deliberately not a
dependency. LENS environments authenticate through Claude Code SSO only — there are
no API keys to provision — so the subprocess path is the only one that works
everywhere LENS runs. Consumers depend on the `LLMClient` Protocol, not the client,
so tests inject stubs and a future SDK swap stays contained. Anyone tempted to "fix"
this by adding the SDK should know it was rejected, not overlooked.
