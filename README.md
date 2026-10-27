# splunk-aitk-local-agents

A Splunk app that lets AI Toolkit 6.2.0 Agent Launchpad agents run on-prem without Cloud Connect. It stands in for the `splunk_cloud_connect` app and serves a local agent runtime (LangGraph) that uses the toolkit's own LLM and MCP connections.

Design: [`docs/DESIGN.md`](docs/DESIGN.md). Research behind it: [`docs/research/`](docs/research/). Spec: issue #13.

> **Cannot coexist with Splunk's Cloud Connect app.** This app uses Cloud Connect's app id (`splunk_cloud_connect`) because that is where AI Toolkit looks. Uninstall the real Cloud Connect app first, and don't install it later.

**Prerequisite:** AI Toolkit's own REST handlers won't load without the Python for Scientific Computing add-on (`Splunk_SA_Scientific_Python_linux_x86_64`), so install it as AI Toolkit's docs require.

## Checking an install

After installing and restarting splunkd, run these as an admin. `| rest` can't parse these endpoints, so use curl:

```
# The stand-in: expect 200, status "activated", tenantName "services/aitk_local"
curl -sk -u admin "https://localhost:8089/servicesNS/nobody/splunk_cloud_connect/v1alpha1/cloud-connection/splunk_ml_toolkit?output_mode=json" | sed -E 's/"token":"[^"]*"/"token":"REDACTED"/'

# AI Toolkit's relay for non-admin users: expect 200 with the same tenant
curl -sk -u admin "https://localhost:8089/servicesNS/admin/Splunk_ML_Toolkit/mltk/cloud_connect_details?output_mode=json" | sed -E 's/"scs_token":"[^"]*"/"scs_token":"REDACTED"/'
```

Failures are logged to `$SPLUNK_HOME/var/log/splunk/aitk_local.log`.

## Layout

- `splunk_cloud_connect/`: the Splunk app itself.
- `tests/`: contract tests that run AI Toolkit's own client code against this app's handlers, served by a fake splunkd.
- `spike/`: throwaway spike from #2, kept for re-running on other Splunk versions.

## Configuration

`aitk_local.conf` `[runtime]`:

- `advertised_host`: the name AI Toolkit uses to reach this search head. It must match splunkd's TLS certificate. The default is this machine's FQDN.
- `advertised_port`: the default is splunkd's management port.

## Running the contract tests

AI Toolkit is proprietary and is never committed here. Unpack `splunk-ai-toolkit_620.tgz` somewhere and point `AITK_APP` at the `Splunk_ML_Toolkit` directory:

```
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
AITK_APP=/path/to/Splunk_ML_Toolkit pytest
```

The tests refuse to run without `AITK_APP`.
