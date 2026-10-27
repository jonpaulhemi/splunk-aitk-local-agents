# Spike #2: splunkd persistent REST handler holds long agent requests

**Verdict: GO.** Run the agent runtime inside splunkd as a persistent REST handler; the sidecar fallback is not needed.

Run 2026-10-27 on J's test search head (`splunk-test`), TLS verified against the host's real name, with `spike/aitk_spike_stream` v0.0.2 and `spike/spike_client.py`.

## Results
| Question | Result |
|---|---|
| Non-Splunk bearer reaches the handler (`requireAuthentication = false`, `passHttpHeaders = true`) | Yes. Right token gets 200; missing token and wrong token both reach our handler and get our JSON 401 (`auth_header_seen` false / true). splunkd does not try to validate the bearer. |
| AI Toolkit-shaped URL lands on the handler | Yes. `match = /aitkspike` serves `/services/aitkspike/slim-api/v1alpha1/agent/aws_agentcore/invoke`; handler sees `path_info = slim-api/v1alpha1/agent/aws_agentcore/invoke`. GET and POST both work; POST body arrives (`passPayload = true`). So `tenantName = services/<prefix>` style routing works. |
| Long requests | 30s, 120s, 300s and **600s** all returned 200 with the full body. No splunkd timeout hit with default settings. |
| Streaming | Not streamed: all NDJSON lines arrive together when the handler returns. That's fine for AI Toolkit, which reads lines until `completed`, but `thinking` keep-alives are pointless. |
| Concurrency | 4 parallel 30s requests finished in 30.1s wall time. splunkd spawned extra persistent handler processes (4 distinct PIDs), so requests don't queue behind one process. |

## Gotchas found
- A persistent handler stanza **must** have `script = <file>.py` as well as `handler = module.Class`. Without it, btool lists the stanza but splunkd never registers the endpoint, and you get 404 for authenticated callers and 401 for unauthenticated ones.
- The client must use the host name on splunkd's cert. `localhost` fails verification, which matches the design's choice of the real FQDN for `tenantApiHostname`.
- **`python.version = python3` ran the handler on Python 3.9.25**, not 3.13. AI Toolkit pins 3.13 with `python.required = 3.13`, so our handlers must do the same, or the cp313 wheels from #5 won't load. Verify this in #5.
- Each concurrent request can land in a different process, so per-process state (caches, loaded keys) can't be assumed shared. Durable state (checkpoints, runtime records) must live in SQLite or KV store, as already planned.

## Not tested
- Very large request bodies (real invoke payloads are a few KB, as tested).
- Behavior under splunkd restart mid-request.
- Concurrency above 4.
