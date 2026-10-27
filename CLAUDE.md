# splunk-aitk-local-agents

Splunk app (app id `splunk_cloud_connect`) that replaces Cloud Connect + Splunk's hosted AgentCore runtime so AI Toolkit agents run locally. Design decisions: `docs/DESIGN.md`. The AgentCore contract we implement: `docs/research/aitk-6.2.0-agentcore-contract.md`.

- Never modify AI Toolkit (`Splunk_ML_Toolkit`) code; this app is an overlay.
- Build tickets live in this repo's GitHub Issues.
