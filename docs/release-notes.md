# Release notes

## 0.11.0

This is the first release checked end to end on a real search head. The acceptance and no-cloud-traffic reports go in `docs/releases/0.11.0/` once that run passes.

- Runs AI Toolkit 6.2.0 Agent Launchpad agents and `| aiagent` on the search head, with no Cloud Connect and no Splunk cloud traffic.
- **Connections:** Custom (OpenAI-compatible) LLM connections, and Bedrock, including GovCloud regions and assumed roles.
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
- **Logging:** the run log now records a Bedrock run's region, so a GovCloud run can be shown.

**Known issues**
- **AI Toolkit 6.2.0 Launchpad error:** Launchpad intermittently fails a message with "Conversation search id was not returned", and splunkd logs `unexpected data received (73)`. This is AI Toolkit's own handler, and it happens with this app disabled too. Sending again works, and `| aiagent` is unaffected. See `docs/install.md`.
- **Approval emails:** AI Toolkit's approval emails haven't been tried live.
- **Search head clusters:** only a standalone search head is supported.
- **Platform:** the bundled libraries are built for Linux x86_64 only.
