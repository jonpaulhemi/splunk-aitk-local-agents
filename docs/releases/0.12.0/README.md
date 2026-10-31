# 0.12.0 acceptance

Run on 2026-10-31 against a standalone search head (Splunk 10.4.3), following [`docs/release.md`](../../release.md), on AI Toolkit 6.2.0. The release package's sha256 starts `962204a39095bd8a`.

- `acceptance.json`: 24 of 24 checks passed, on an OpenAI-compatible connection and commercial Bedrock (us-east-1), including the new skills check and a knowledge base search (an unstructured vector store in us-east-2). Every temporary agent and the temporary skill were removed. No replies needed recovering: the run had the `tools/http_debug_guard.py` workaround for AI Toolkit's `unexpected data received (73)` installed and active.
- `egress.json`: no Splunk cloud names. The other names are the LLM, Bedrock, Bedrock Agent Runtime and STS endpoints, local names (`splunk-test`, `_grpc_config.localhost`), and `chatgpt.com`, which the developer tool running the tests on the host contacted, not Splunk. The UDP port 123 connections are the host's NTP. MCP traffic stayed on the host, so it isn't captured.

The search head was set up as `docs/install.md` describes for an offline install: Settings > Instrumentation all off, and `[applicationsManagement] allowInternetAccess = false` in server.conf. Earlier captures in this release cycle, with those settings on, failed on `telemetry-splkmobile.dataeng.splunk.com` (platform telemetry) and `cdn.splunkbase.splunk.com` (splunkd's Splunkbase app catalog).
