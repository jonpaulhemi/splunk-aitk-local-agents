# 0.11.0 acceptance

Run on 2026-10-29 against a standalone search head, following [`docs/release.md`](../../release.md), on AI Toolkit 6.2.0.

- `acceptance.json`: 18 of 18 checks passed, on an OpenAI-compatible connection and commercial Bedrock (us-east-1). The script recovered 3 create replies and 3 delete replies that AI Toolkit's known `unexpected data received (73)` issue corrupted. Each change had gone through, and every temporary agent was removed.
- `egress.json`: no Splunk cloud names. The other names are the LLM and Bedrock/STS endpoints, the search head's own name, and `chatgpt.com`, which a separate developer tool on the host contacted, not Splunk. The UDP port 123 connections are the host's NTP. MCP traffic stayed on the host, so it isn't captured.
- An earlier capture saw `quickdraw.splunk.com`, Splunk's platform telemetry. The check now lists that under `platform_telemetry` without failing (see `docs/install.md`). It didn't appear in this capture.
