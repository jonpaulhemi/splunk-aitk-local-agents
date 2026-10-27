# splunk-aitk-local-agents

A Splunk app that lets AI Toolkit 6.2.0 Agent Launchpad agents run on-prem without Cloud Connect. It stands in for the `splunk_cloud_connect` app and serves a local agent runtime (LangGraph) that uses the toolkit's own LLM and MCP connections.

Design: [`docs/DESIGN.md`](docs/DESIGN.md). Research behind it: [`docs/research/`](docs/research/).
