# Spike #2: can splunkd hold a 300s agent request?

Throwaway. Answers issue #2 before we commit to running the agent runtime inside splunkd.

## What it tests
1. **Auth pass-through.** AI Toolkit sends its own bearer token, not a Splunk one. With `requireAuthentication = false`, does the `Authorization` header reach our handler, and do we get to answer 401 ourselves?
2. **URL shape.** AI Toolkit builds `https://{host}/{tenant}/slim-api/v1alpha1/...`. Does a handler matched at `/aitkspike` serve `/services/aitkspike/slim-api/v1alpha1/agent/aws_agentcore/invoke`?
3. **Long requests.** 30s, 120s, 300s and 600s, recording when each NDJSON line arrives (and whether it streams or arrives in one buffered chunk; buffered is fine for AI Toolkit).
4. **Concurrency.** Several long requests at once. Do they run in parallel or queue behind one persistent process?

## Run it
1. Package and install on the **test** search head, then restart:
   ```
   tar czf aitk_spike_stream.tgz aitk_spike_stream
   # Apps > Manage Apps > Install app from file, or copy to $SPLUNK_HOME/etc/apps/
   ```
2. From a machine that can reach port 8089:
   ```
   python3 spike_client.py https://<search-head>:8089 > spike-result.json
   ```
   Add `--cafile <your CA>` if the cert isn't in the OS store, or `--insecure` on a test box. The full default run takes about 20 minutes.
3. Share `spike-result.json` plus the splunkd version (`$SPLUNK_HOME/bin/splunk version`) and anything in `index=_internal sourcetype=splunkd aitk_spike_stream` that looks like an error.
4. Remove the app afterwards.
