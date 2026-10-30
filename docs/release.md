# Making a release

Each release proves on the test search head that every agent feature works and that nothing reaches Splunk's cloud. Do it again after every AI Toolkit upgrade.

## 1. Build and check the package

```
python tools/bundle.py build        # if requirements/bundle.lock changed, or lib/ is missing
python tools/contract.py /path/to/Splunk_ML_Toolkit      # or the AI Toolkit .tgz
python tools/bundle.py package      # dist/splunk_cloud_connect-<version>.tgz
```

The version comes from `default/app.conf`. Change it in both the `[launcher]` and `[id]` stanzas. Then run AppInspect as described in [`appinspect.md`](appinspect.md).

## 2. Install on the test search head

Use Splunk Web's **Install app from file**, then restart splunkd. Follow [`install.md`](install.md). Installing is a manual step on purpose: it's the path an admin takes, and the acceptance script checks the result, but it doesn't install anything itself.

## 3. Watch outbound traffic

On the search head, as root, start the capture. It needs `tcpdump`:

```
sudo python3 tools/egress_watch.py --out egress.json
```

Leave it running for the next step. It records the following, and fails if any name belongs to Splunk's cloud:
- every DNS lookup
- every TLS server name or HTTP proxy CONNECT host
- every new outbound connection

The names it lists should be your LLM endpoint, AWS Bedrock (and STS, if a connection assumes a role), Bedrock Agent Runtime (`bedrock-agent-runtime.<region>.amazonaws.com`) if you check a knowledge base, and your MCP servers. Nothing else should appear. Splunk's own platform telemetry fails the check like any other Splunk cloud traffic, and so does splunkd's Splunkbase app catalog and update check. Before the capture, turn off **Settings > Instrumentation** and set `allowInternetAccess = false` under `[applicationsManagement]` in server.conf (see `docs/install.md`). The capture sees every process on the host, so close any browser running there too.

It can only fail on names it sees. A connection made by IP address, or one whose name the capture missed, goes under `connections_without_a_name` instead. Look each one up (`dig -x`, or your firewall's logs) before calling the run clean. The setup check in step 4 covers the scheduled traffic that wouldn't fire during a short capture: the sync inputs and telemetry searches must all be off.

## 4. Run the acceptance script

From any machine that can reach port 8089, run:

```
SPLUNK_PASSWORD=... python3 tools/acceptance.py https://<search head FQDN>:8089 \
    --expect-version <version> \
    --openai-agent <OpenAI-compatible agent with an ungated Splunk MCP tool> \
    --bedrock-agent <Bedrock agent with an ungated Splunk MCP tool> \
    --approval-agent <agent whose Splunk MCP tool requires approval> \
    [--knowledge-base <AWS KB connection name> --kb-question "<a question only its documents answer>" \
     --kb-expect "<a word the answer must contain>"] \
    --out acceptance.json
```

Without `--knowledge-base`, the knowledge base check is reported as skipped. [`knowledge-base-setup.md`](knowledge-base-setup.md) walks through making a small knowledge base for it.

The three agents are templates, and they aren't changed. The script copies their connections into new `aitk_acceptance_*` agents and checks the following:
- the install and the Cloud Connect stand-in
- setup, run twice: the second run must find all two sync inputs and five telemetry searches already off
- diagnostics
- creating agents, each through AI Toolkit's own API
- a plain answer, an MCP tool call, and session memory, each on both connections
- structured output
- an approval pause, followed by an approval that runs the tool exactly once
- skills: a skill the script creates, which the agent has to load to answer, shows as loaded with a `load_skill` call in the trace
- with `--knowledge-base`, a knowledge base search that succeeds, and an answer containing the `--kb-expect` text
- that the Bedrock runs reached this app's runtime, and in which AWS region, using `aitk_local.log` in `index=_internal`
- that no prompt, and no skill's text, appears in that log

At the end, the script deletes its agents and its skill.

Then stop the capture with Ctrl-C. Both scripts exit non-zero on failure.

## 5. Record it

Neither report holds prompts, answers or tool results. Before committing, check them for host names you'd rather not publish.

1. Commit `acceptance.json` and `egress.json` to `docs/releases/<version>/`.
2. Add the release's entry to [`release-notes.md`](release-notes.md).
3. Attach the .tgz to the release.
