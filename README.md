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

# AI Toolkit's relay: expect 200 with the same tenant. The caller needs
# apply_ai_commander_command (e.g. AI Toolkit's mltk_admin role), else 403.
curl -sk -u admin "https://localhost:8089/servicesNS/admin/Splunk_ML_Toolkit/mltk/cloud_connect_details?output_mode=json" | sed -E 's/"scs_token":"[^"]*"/"scs_token":"REDACTED"/'
```

Failures are logged to `$SPLUNK_HOME/var/log/splunk/aitk_local.log`.

## Building the package

The agent libraries (LangGraph, LangChain, MCP adapters and their dependencies) ship inside the app in `splunk_cloud_connect/lib/`, so nothing is installed on the search head. They are pinned with hashes in `requirements/bundle.lock`, always as CPython 3.13 manylinux x86_64 wheels, and aren't committed.

```
python tools/bundle.py build      # lib/ from bundle.lock (needs PyPI access)
python tools/bundle.py package    # dist/splunk_cloud_connect-<version>.tgz
```

To change the library set, edit `requirements/bundle.in`, then run `lock`, `build` and `inventory` (which rewrites [`docs/bundled-libraries.md`](docs/bundled-libraries.md): licenses, compiled packages, sizes).

After installing, `GET /services/aitk_local/diagnostics` (admin only) imports every bundled library inside splunkd, runs a one-step checkpointed graph, and reports the Python version and any library that was loaded from outside `lib/`.

## Turning off AI Toolkit's cloud traffic

AI Toolkit syncs agent templates and managed skills from Splunk's cloud every 6 hours and schedules five telemetry searches. As an admin, run this once after installing, and again after each AI Toolkit upgrade:

```
curl -sk -u admin -X POST "https://localhost:8089/services/aitk_local/offline_setup?output_mode=json"
```

It disables the two sync inputs and unschedules the telemetry searches through splunkd's REST API, and replies with what it changed. A second run reports nothing changed.

Add `-d allowed_domains=mcp.corp.example,*.internal.example` to also set AI Toolkit's domain allowlist (`mlspl.conf [ai:AllowedDomains]`) and turn its enforcement on. In AI Toolkit 6.2.0 the allowlist only limits which MCP server URLs agents may use. LLM endpoints aren't checked against it, and MCP URLs must be HTTPS and can't be `localhost`, whatever the list says. Without `allowed_domains` the allowlist is left as it is.

## Layout

- `splunk_cloud_connect/`: the Splunk app itself.
- `requirements/`, `tools/bundle.py`: the bundled-library build.
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
python tools/bundle.py build
AITK_APP=/path/to/Splunk_ML_Toolkit pytest
```

The tests refuse to run without `AITK_APP`.
