# Installing, upgrading and re-checking

For one standalone Splunk Enterprise search head on Linux x86_64. AI Toolkit 6.2.0 must be installed, along with the Python for Scientific Computing add-on it needs.

## Install

1. **Remove Splunk's Cloud Connect app, if it's there.** This app uses Cloud Connect's app id, `splunk_cloud_connect`, because that's where AI Toolkit looks. The two can't be installed together.
2. **Install the package.** In Splunk Web, go to **Apps > Manage Apps > Install app from file** and choose `splunk_cloud_connect-<version>.tgz`. Or run:
   ```
   $SPLUNK_HOME/bin/splunk install app splunk_cloud_connect-<version>.tgz
   ```
3. **Restart splunkd.** The app has no setup page, and Splunk Web won't ask for one.
4. **Check the install.** As an admin, run:
   ```
   curl -sk -u admin "https://localhost:8089/services/aitk_local/diagnostics?output_mode=json"
   ```
   Expect `"errors": {}`, `"graph_run": "ok"` and `"shadowed": []`. The README's "Checking an install" section has two more checks: the Cloud Connect stand-in, and AI Toolkit's relay.

## Setup

Run this once as an admin after installing. It turns off AI Toolkit's own cloud traffic: the two cloud sync inputs and five telemetry searches.

```
curl -sk -u admin -X POST "https://localhost:8089/services/aitk_local/offline_setup?output_mode=json"
```

Setup doesn't change Splunk's own platform telemetry, which sends usage data to `quickdraw.splunk.com`. That's a server-wide setting, not part of AI Toolkit, and some license types need license usage reporting. To turn it off, go to **Settings > Instrumentation**. The no-cloud-traffic check in [`docs/release.md`](release.md) lists this traffic but doesn't fail on it.

To also set AI Toolkit's MCP domain allowlist, add `-d allowed_domains=...`. The README explains what that list does and doesn't cover.

Next, set up connections and agents in AI Toolkit as usual:
- an LLM connection, either Custom (OpenAI-compatible) or Bedrock
- MCP connections, marking tools as requiring approval where needed
- skills, if your agents use them; Splunk-managed skills aren't available offline, since their catalog comes from Splunk's cloud
- knowledge base connections (AWS KB), if your agents search one; [knowledge-base-setup.md](knowledge-base-setup.md) walks through creating a small one
- agents in Agent Launchpad

## Upgrading this app

Install the new package over the old one, then restart:

```
$SPLUNK_HOME/bin/splunk install app splunk_cloud_connect-<version>.tgz -update 1 && $SPLUNK_HOME/bin/splunk restart
```

Your `local/` settings, the agents, and the session memory in `$SPLUNK_HOME/var/lib/aitk_local/` all carry over.

An in-place upgrade doesn't remove files that the new package leaves out. It also keeps the modes of existing files. Both are harmless. To make the installed copy match the package exactly:
1. Back up `local/`.
2. Remove `$SPLUNK_HOME/etc/apps/splunk_cloud_connect`.
3. Install the package.
4. Restore `local/`.
5. Restart splunkd.

See `docs/appinspect.md` for more.

## Upgrading AI Toolkit

This app implements the agent runtime API that AI Toolkit 6.2.0 calls, as described in [`docs/research/aitk-6.2.0-agentcore-contract.md`](research/aitk-6.2.0-agentcore-contract.md). A new AI Toolkit version may change that API. Check it before upgrading production search heads:

1. **Run the contract tests against the new package.** On a build machine, with the dev requirements and the bundled libraries built (see the README), run:
   ```
   python tools/contract.py ~/Downloads/splunk-ai-toolkit_<new>.tgz
   ```
   The tests drive the new AI Toolkit's own code against this app. A failure names the call that changed. Don't upgrade until it passes or the app has been updated.
2. **Upgrade AI Toolkit** on the search head and restart.
3. **Run setup again.** AI Toolkit's upgrade can re-enable its sync inputs and telemetry searches. A second run reports only what it changed.
4. **Repeat the acceptance run** in [`docs/release.md`](release.md) against the upgraded search head.

## Known issue in AI Toolkit 6.2.0

Launchpad sometimes fails a chat message with "Conversation search id was not returned", or the agent list fails to load. When this happens, splunkd logs `unexpected data received (73)` for AI Toolkit's own `/mltk/agent_converse` handler. Stray output from that handler's process corrupts the reply.

The same corruption can hit replies to creating or deleting an agent through AI Toolkit's agents API, seen in the 0.11.0 acceptance run. The change still goes through, so check the agent list before trying again.

This happens at the same rate with this app disabled, about 2 to 3 messages in 10. Sending the message again works. When it hits an approval decision, the decision isn't lost: no run is submitted twice. `| aiagent` isn't affected.
