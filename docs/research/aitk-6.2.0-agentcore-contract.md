# Why Agent Launchpad needs Cloud Connect (AI Toolkit 6.2.0)

Static read of `bin/`; nothing has been run yet.

## The short version
Agents do **not** run inside Splunk. AI Toolkit stores the agent definition in KV store, then ships it, together with your LLM connection (including its credentials), MCP connections, skills and knowledge-base config, to Splunk Cloud Services ("SCS" / "SLIM"), which runs the agent on **AWS Bedrock AgentCore**. Cloud Connect is only how Splunk finds the SCS URL and gets a token. So "agents without Cloud Connect" means **supplying our own agent runtime**, not just flipping a setting.

## How AI Toolkit finds the cloud (on-prem path)
1. `is_cloud_stack()` reads `/services/server/info` `instance_type`; on-prem → false.
2. `fetch_cloud_connect_details*()` calls `GET /servicesNS/nobody/splunk_cloud_connect/v1alpha1/cloud-connection/splunk_ml_toolkit` and expects `{"status":"activated","token":..., "tenantName":..., "tenantApiHostname":...}` (`bin/util/ai_commander_util.py:1498-1560`). Non-admin callers go through AI Toolkit's own relay `/mltk/cloud_connect_details`.
3. Base URL = `https://{tenantApiHostname}/{tenantName}/slim-api/v1alpha1`; bearer = `token`.

## The cloud contract AI Toolkit speaks (all under that base URL)
| Call | Where in AI Toolkit | Expects |
|---|---|---|
| `POST /agent/aws_agentcore` `{agent_name, app_version, agent_type}` | `agent_manager.py:514` (create) | 200/201 with `runtime_id`, `version`, `memory_id` |
| `DELETE /agent/aws_agentcore/{runtime_id}` | `agent_manager.py:711` | 200/202/204, `status` |
| `GET /agent/aws_agentcore?runtime_ids=..&memory_ids=..` | `AgentStatusProcessor.py:118` | runtime/memory state |
| `GET /encryption/public-key` | `AiAgentProcessor.py:180` | RSA public key (PEM) |
| `POST /agent/aws_agentcore/invoke` | `AiAgentProcessor.py:604` | newline-delimited JSON stream; final line `status:"completed"` with `success`, `response`, `tool_invocations`, `hitl_status`, `hitl_pending`, `skills` |

Invoke body is encrypted: payload → Fernet key from PBKDF2-SHA256(token, salt, 100k iters); salt RSA-OAEP-SHA256 encrypted with the public key from `/encryption/public-key`. Whoever serves the public key and the token can decrypt it. Decrypted payload keys: `system_prompt, task_prompt, session_id, runtime_id, memory_id, version, agent_timeout, skills, structured_output?, hitl_response?` plus the MCP, KB and LLM config blocks.

TLS: AI Toolkit verifies the endpoint with certifi plus the OS trust store (`util/agent_ssl_context.py`), so a local endpoint needs a cert chained to a CA in one of those.

## The UI
The agent pages (`appserver/static/agents.js` etc.) don't check Cloud Connect themselves; the failure shows up from the backend calls above. If those calls succeed, the Launchpad UI, run history and HITL pages should work unchanged.

## Implication
A companion app can supply both halves without touching AI Toolkit code:
1. a stand-in for the Cloud Connect endpoint that reports `activated` and points `tenantApiHostname` at a local runtime, and
2. a local runtime implementing the five calls above, running the agent loop against your configured LLM connection (custom OpenAI-compatible or Bedrock GovCloud) and MCP connections.
Risk: this is an undocumented internal contract that can change between AI Toolkit versions.
