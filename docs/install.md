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

Setup doesn't change Splunk's own platform telemetry, which sends usage data to Splunk (seen: `quickdraw.splunk.com`, `telemetry-splkmobile.dataeng.splunk.com`). That's a server-wide setting, not part of AI Toolkit, and some license types need license usage reporting. To turn it off, go to **Settings > Instrumentation** and turn off every kind of sharing, or set `sendAnonymizedUsage`, `sendSupportUsage`, `sendLicenseUsage` and `sendAnonymizedWebAnalytics` to false through `/servicesNS/nobody/splunk_instrumentation/admin/telemetry/general`, then restart. The no-cloud-traffic check in [`docs/release.md`](release.md) fails on this traffic like any other Splunk cloud traffic.

splunkd's app manager also reaches Splunkbase on its own schedule, to download the app catalog from `cdn.splunkbase.splunk.com` and check `apps.splunk.com` for app updates. Setup doesn't change this either, since it's a server-wide setting. To turn it off, add this to `$SPLUNK_HOME/etc/system/local/server.conf` and restart:

```
[applicationsManagement]
allowInternetAccess = false
```

Splunk Web can then no longer browse or install apps from Splunkbase; install them from a file instead.

To also set AI Toolkit's MCP domain allowlist, add `-d allowed_domains=...`. The README explains what that list does and doesn't cover.

Next, set up connections and agents in AI Toolkit as usual:
- an LLM connection, either Custom (OpenAI-compatible) or Bedrock
- MCP connections, marking tools as requiring approval where needed
- skills, if your agents use them; Splunk-managed skills aren't available offline, since their catalog comes from Splunk's cloud
- knowledge base connections (AWS KB), if your agents search one; [knowledge-base-setup.md](knowledge-base-setup.md) walks through creating a small one
  - searches go to AWS Bedrock Agent Runtime (`bedrock-agent-runtime.<region>.amazonaws.com`), plus STS when the connection has a role; that's AWS traffic, not Splunk cloud
  - searches run as the connection's role ARN, assumed with its access key when each run starts; AI Toolkit's own **Test connection** uses the access key directly and ignores the role, so a connection can pass that test and still fail an agent run if the key can't assume the role. The run's error names the role and the AWS error code.
- on an EC2 search head, Bedrock model and knowledge base connections can skip access keys: make them on this app's **Instance-role connections** page instead of in AI Toolkit's form (0.13.0 and later). See [instance-role-connections.md](instance-role-connections.md) for the IAM setup. The runtime also reaches the instance metadata service (`169.254.169.254`, link-local) for those connections.
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

Launchpad sometimes fails a chat message with "Conversation search id was not returned", or the agent list fails to load. When this happens, splunkd logs `unexpected data received (73)` for AI Toolkit's own `/mltk/agent_converse` handler. Replies to creating or deleting an agent through AI Toolkit's agents API can fail the same way; the change still goes through, so check the agent list before trying again. Without the workaround below, expect roughly 2 to 4 failures in 10, whether this app is installed or not. Sending the message again works. When it hits an approval decision, the decision isn't lost: no run is submitted twice. `| aiagent` isn't affected.

**Cause.** AI Toolkit's `bin/agent_manager/agent_manager.py` (line 96) turns on Python's HTTP debug output (`http.client.HTTPConnection.debuglevel = 1`) for its whole REST handler process. Each HTTP call AI Toolkit makes there then prints `send: ...`, `reply: ...` and `header: ...` lines, including the user's `Authorization` header, to stdout. In a splunkd persistent REST handler, stdout is the pipe splunkd reads replies from, so splunkd rejects some replies. The 73 is the byte 0x73, the "s" of "send:". splunkd discards the rejected text and returns only its own error, and a check of a test search head found no session key in any log, search artifact or index.

**Workaround.** [`tools/http_debug_guard.py`](../tools/http_debug_guard.py) installs a small startup hook into Splunk's own Python 3.13 that throws away those debug prints, in persistent REST workers only. It doesn't change AI Toolkit's files or any other Python process. On a test search head it took chat failures from 21 in 50 to 0 in 50. Copy the script to the search head and run, as root or the user splunkd runs as:

```
sudo python3 http_debug_guard.py install
sudo systemctl restart Splunkd      # or $SPLUNK_HOME/bin/splunk restart, if splunkd isn't run by systemd
sudo python3 http_debug_guard.py status
```

`status` should say the hook is installed and active. The restart matters: splunkd keeps REST workers running for hours, and only workers started after the install have the hook.

The hook lives in `$SPLUNK_HOME/lib/python3.13/site-packages`, not in an app, so:

- **After a Splunk upgrade,** run `status`; if the upgrade removed the hook, run `install` again and restart.
- **To remove it,** run `uninstall` and restart. Do this once AI Toolkit ships a version without the debug switch.
