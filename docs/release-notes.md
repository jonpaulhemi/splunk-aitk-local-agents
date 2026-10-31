# Release notes

## 0.13.0

Bedrock models and knowledge bases with no access keys. The acceptance and no-cloud-traffic reports go in `docs/releases/0.13.0/` once that run passes.

- **Instance-role connections:** a Bedrock model or knowledge base connection with a role ARN and no access keys.
  - The runtime takes the EC2 instance profile's credentials from IMDSv2 only, never from `AWS_*` environment variables or `~/.aws`.
  - It assumes the connection's role on the STS endpoint in the connection's region, on every run, and calls Bedrock as that role. The role is usually in another account.
  - The run log names the role.
  - Clear errors for a missing role ARN, no instance credentials, and a refused AssumeRole (naming the role and the AWS error code).
  - Connections with access keys work as before, and keys win when a connection has both.
- **Setup page:** this app is now visible in Splunk Web, with one admin-only page, **Instance-role connections**, backed by `/services/aitk_local/connections`.
  - It creates, edits, lists and deletes instance-role connections, writing them into AI Toolkit's own store through AI Toolkit's REST API with empty key fields and an `_instance_role` name suffix.
  - Each save is tested first, and the result says whether AssumeRole or the Bedrock call failed. **Save anyway** skips that.
  - Model connections can pick from the models the role can see.
  - Key-based connections are never touched.
- **`| ai`:** AI Toolkit's own `| ai` command works with instance-role model connections, unchanged (best effort; see the known issues).
- **Egress:** instance-role connections also reach the instance metadata service (link-local `169.254.169.254`) and STS in the connection's region. Still no Splunk cloud traffic.
- **New:** [`instance-role-connections.md`](instance-role-connections.md), covering IAM setup across two accounts, IMDS, the page and REST. The acceptance run gains `--instance-role`.

**Known issues**
- **AI Toolkit's Test connection button** always fails for instance-role connections, models and knowledge bases alike, since it needs access keys. Use the setup page's test.
- **Edit instance-role connections only on the setup page.** AI Toolkit's form asks for keys, and saving there can turn the connection into a key-based one.
- **`| ai ... kb_id=`** searches the knowledge base as the *model* connection's role, so that role needs `bedrock:Retrieve` on the knowledge base too.
- **Names:** AI Toolkit only accepts letters, digits and underscores, so the suffix is `_instance_role`, not "(instance role)" as first planned.
- GovCloud and C2S role ARNs are accepted, but only commercial AWS has been tested.
- The 0.12.0 known issues below still apply.

## 0.12.0

Skills and AWS knowledge bases. The acceptance and no-cloud-traffic reports are in [`docs/releases/0.12.0/`](releases/0.12.0/).

- **Skills:** the skills attached to an agent in AI Toolkit now work. The agent sees each skill's name and description and loads a skill's full instructions with `load_skill` when the task matches. Loads show in Run History and in the "Loaded skills" panel on the reply where they happened. Splunk-managed skills aren't available offline, since their catalog comes from Splunk's cloud.
- **Knowledge bases:** each AWS KB knowledge base attached to an agent is a search tool, `search_knowledge_base_<kb id>`, described by the connection's description. A search returns the best-matching chunks (5 by default, `[knowledge_base] chunks_per_search`) with their sources and scores, through Bedrock Agent Runtime's Retrieve. The agent writes the answer itself. Searches run as the connection's IAM role, assumed once per run. A role that can't be assumed fails the run with a message naming the role and the AWS error code. A failed or slow search (30 seconds, `[knowledge_base] search_timeout_seconds`) comes back to the agent as a tool error.
- **Egress:** knowledge base searches go to `bedrock-agent-runtime.<region>.amazonaws.com` (and STS for the role). Still no Splunk cloud traffic.
- **No-cloud-traffic check:** Splunk's platform telemetry (`quickdraw.splunk.com`, `telemetry-splkmobile.dataeng.splunk.com`) now fails the check instead of being listed as an exception. So does splunkd's Splunkbase app catalog download (`cdn.splunkbase.splunk.com`). Before a release capture, turn off **Settings > Instrumentation** and set `[applicationsManagement] allowInternetAccess = false` in server.conf. Setup changes neither, since both are server-wide; see `docs/install.md`.
- **New:** [`knowledge-base-setup.md`](knowledge-base-setup.md), a guide to making a small knowledge base for trying this out. The acceptance run gains a skills check, and a knowledge base check with `--knowledge-base`.

**Known issues**
- **AI Toolkit 6.2.0 "unexpected data received (73)":** still AI Toolkit's own, but now with a cause and a workaround. [`tools/http_debug_guard.py`](../tools/http_debug_guard.py) stops the HTTP debug output that corrupts AI Toolkit's REST replies; see "Known issue in AI Toolkit 6.2.0" in `docs/install.md`.
- **AI Toolkit's Test connection for knowledge bases** uses the access key directly and ignores the role ARN, so a connection can pass it and still fail an agent run if the key can't assume the role.
- **Knowledge base types:** only unstructured vector-store knowledge bases are supported. AI Toolkit's connection form can't save Bedrock Managed ones, because its test uses vector search settings they reject, and structured data stores (SQL) aren't supported.
- **Long-term memory** (across sessions) isn't supported. AI Toolkit's invoke doesn't say which user is chatting, so a memory per agent would be shared by everyone who uses it.
- The 0.11.0 known issues below still apply.

## 0.11.0

This is the first release checked end to end on a real search head. The acceptance and no-cloud-traffic reports go in `docs/releases/0.11.0/` once that run passes.

- Runs AI Toolkit 6.2.0 Agent Launchpad agents and `| aiagent` on the search head, with no Cloud Connect and no Splunk cloud traffic.
- **Connections:** Custom (OpenAI-compatible) LLM connections, and Bedrock, with access keys or an assumed role. GovCloud region names are accepted, but only commercial regions have been tested live.
- **Features:**
  - MCP tools over streamable HTTP, with traces in Run History
  - structured output
  - per-session memory
  - tool approvals
- **Setup:** one setup call turns off AI Toolkit's own cloud sync inputs and telemetry searches.
- **New:**
  - `tools/acceptance.py` and `tools/egress_watch.py`, the release acceptance run (see `docs/release.md`)
  - `tools/contract.py`, which runs the contract tests against an AI Toolkit package in one command
  - install and upgrade docs in `docs/install.md`
- **Logging:** the run log now records a Bedrock run's region.

**Known issues**
- **AI Toolkit 6.2.0 Launchpad error:** Launchpad intermittently fails a message with "Conversation search id was not returned", and splunkd logs `unexpected data received (73)`. This is AI Toolkit's own handler, and it happens with this app disabled too. Replies to agent create and delete calls can be corrupted the same way, though the change goes through. Sending again works, and `| aiagent` is unaffected. See `docs/install.md`.
- **Platform telemetry:** setup doesn't turn off Splunk's own platform telemetry to `quickdraw.splunk.com`. It's a server-wide setting, so turn it off under **Settings > Instrumentation** if you need to. The no-cloud-traffic check lists it but doesn't fail on it. See `docs/install.md`.
- **Approval emails:** AI Toolkit's approval emails haven't been tried live.
- **Search head clusters:** only a standalone search head is supported.
- **Platform:** the bundled libraries are built for Linux x86_64 only.
