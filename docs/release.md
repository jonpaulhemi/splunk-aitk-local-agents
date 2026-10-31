# Making a release

Each release proves on the test search head that every agent feature works. Do it again after every AI Toolkit upgrade.

There's no outbound-traffic capture any more. The 0.11.0 and 0.12.0 releases showed that nothing reaches Splunk's cloud (their `egress.json` reports are in `docs/releases/`), so from 0.13.0 the release run is the acceptance script alone. `tools/egress_watch.py` stays in the repo for anyone who wants to check by hand.

## 1. Build and check the package

```
python tools/bundle.py build        # if requirements/bundle.lock changed, or lib/ is missing
python tools/contract.py /path/to/Splunk_ML_Toolkit      # or the AI Toolkit .tgz
python tools/bundle.py package      # dist/splunk_cloud_connect-<version>.tgz
```

The version comes from `default/app.conf`. Change it in both the `[launcher]` and `[id]` stanzas. Then run AppInspect as described in [`appinspect.md`](appinspect.md).

## 2. Install on the test search head

Use Splunk Web's **Install app from file**, then restart splunkd. Follow [`install.md`](install.md). Installing is a manual step on purpose: it's the path an admin takes, and the acceptance script checks the result, but it doesn't install anything itself.

## 3. Run the acceptance script

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

Without `--knowledge-base`, the knowledge base check is reported as skipped. From 0.13.0, add `--instance-role` when the Bedrock agent's connection and the knowledge base are instance-role connections made on the setup page ([`instance-role-connections.md`](instance-role-connections.md)). The run then also checks that the setup endpoint lists them, and that this run's invokes assumed their roles with the instance credentials. [`knowledge-base-setup.md`](knowledge-base-setup.md) walks through making a small knowledge base for it.

The three agents are templates, and they aren't changed. The script copies their connections into new `aitk_acceptance_*` agents and checks the following:
- the install and the Cloud Connect stand-in
- setup, run twice: the second run must find both sync inputs and all five telemetry searches already off, so AI Toolkit's scheduled cloud traffic stays off
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

The script exits non-zero on failure.

## 4. Record it

The report holds no prompts, answers or tool results. Before committing, check them for host names you'd rather not publish.

1. Commit `acceptance.json` to `docs/releases/<version>/`.
2. Add the release's entry to [`release-notes.md`](release-notes.md).
3. Attach the .tgz to the release.
