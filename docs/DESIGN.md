# AITK Local Agent Runtime: settled design (grilling, 2026-10-27)

## Goal
Run AI Toolkit 6.2.0 Agent Launchpad agents on a Splunk Enterprise search head **without Cloud Connect**, using the LLM connection already configured in the toolkit (custom OpenAI-compatible endpoint, or Bedrock in GovCloud). LLM egress to GovCloud is allowed. Cloud Connect and Splunk cloud services are not. Internal use only.

## Shape
- **Overlay, not a fork.** AI Toolkit code stays untouched. Everything ships in one companion app.
- **App id `splunk_cloud_connect`**, label "AITK Local Agent Runtime". It stands in for Cloud Connect, so the real Cloud Connect app can never be installed alongside it.
- **Separate repo** from the mTLS bridge. Unrelated to it, since agents use the normal Splunk MCP Server.

## Components
1. **Cloud Connect stand-in.** Serves `GET /servicesNS/nobody/splunk_cloud_connect/v1alpha1/cloud-connection/splunk_ml_toolkit` and returns `status: activated`, `tenantApiHostname` = the search head's real FQDN:8089, a `tenantName` that makes `https://{host}/{tenant}/slim-api/v1alpha1` land on our REST endpoint, and `token` = a random secret generated at install and kept in Splunk's credential store (passwords.conf).
2. **Local runtime**, a persistent REST endpoint inside splunkd that implements the five calls: create, delete, status, `encryption/public-key`, and `invoke`, which streams NDJSON. It rejects requests that don't carry the token and decrypts the invoke payload with its own RSA key. Fallback if splunkd's REST handling can't support 300s runs: a sidecar service.
3. **Agent loop in LangGraph.** Models through `ChatBedrockConverse` (GovCloud region from the saved connection) and `ChatOpenAI` (custom base URL). MCP tools through `langchain-mcp-adapters` over streamable HTTP.
4. **Approvals (HITL)** use LangGraph `interrupt()`, with checkpoints in SQLite on the search head. A KV-store checkpointer comes later if you move to a cluster.
5. **Config overrides in `local/`:** disable the SLIM template/skill sync inputs and the five `outputtelemetry` saved searches, and narrow `[ai:AllowedDomains]` to internal hosts plus the GovCloud endpoints. These must be applied to `Splunk_ML_Toolkit/local/` or done through REST at setup, since one app can't override another app's `local/` from its own `default/`.

## v1 scope
In: MCP tools, structured output, HITL approvals, memory within a session. Out: skills, knowledge bases, long-term memory, CDTSM forecasting, SageMaker/Vertex.

## Platform
Single search head, Linux x86_64, Splunk's Python 3.13. Dependencies bundled under `lib/`, built for cp313 manylinux. splunkd uses an org-CA cert that the OS trust store trusts, so AI Toolkit's certifi + OS-store check passes.

## Risks
- The AgentCore contract is undocumented and internal, so a contract test runs AI Toolkit's own `AiAgentProcessor` / `AgentManager` code against our runtime for each new toolkit version.
- splunkd REST timeouts on long agent runs; prove this first.

References: `research/aitk-6.2.0-egress-map.md`, `research/aitk-6.2.0-agentcore-contract.md`.
