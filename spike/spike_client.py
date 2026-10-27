#!/usr/bin/env python3
"""Client for the issue #2 spike. Standard library only.

Run from any machine that can reach the search head's management port:

    python3 spike_client.py https://sh.example.internal:8089
    python3 spike_client.py https://sh.example.internal:8089 --durations 30 120 300 600 --parallel 4

It checks:
  1. Auth pass-through: a request with a non-Splunk bearer token reaches the
     handler (requireAuthentication = false), and a missing token gets our 401.
  2. AI Toolkit-shaped URL: /services/aitkspike/slim-api/v1alpha1/agent/aws_agentcore/invoke
  3. Long requests: one request per duration, recording when each line arrives.
  4. Concurrency: --parallel requests of the shortest duration at once.
"""

import argparse
import concurrent.futures
import http.client
import json
import ssl
import sys
import time
from urllib.parse import urlparse

TOKEN = "spike-token"
INVOKE_PATH = "/services/aitkspike/slim-api/v1alpha1/agent/aws_agentcore/invoke"


def connect(base, ctx, timeout):
    u = urlparse(base)
    return http.client.HTTPSConnection(u.hostname, u.port or 8089, context=ctx, timeout=timeout)


def call(base, ctx, seconds, lines=5, token=TOKEN, method="POST"):
    conn = connect(base, ctx, timeout=max(1000, seconds + 60))
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps({"payload": "x" * 2048, "salt": "spike"}) if method == "POST" else None
    t0 = time.time()
    conn.request(method, f"{INVOKE_PATH}?seconds={seconds}&lines={lines}", body=body, headers=headers)
    resp = conn.getresponse()
    first_byte = None
    arrivals, last = [], None
    while True:
        line = resp.readline()
        if not line:
            break
        now = round(time.time() - t0, 2)
        first_byte = first_byte if first_byte is not None else now
        arrivals.append(now)
        if line.strip():
            last = line.decode("utf-8", "replace").strip()
    conn.close()
    return {
        "requested_s": seconds,
        "http_status": resp.status,
        "content_type": resp.getheader("Content-Type"),
        "total_s": round(time.time() - t0, 2),
        "first_line_s": first_byte,
        "line_arrivals_s": arrivals,
        "streamed": len(set(arrivals)) > 1,
        "last_line": last,
    }


def safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:  # report, don't stop the run
        return {"error": f"{type(e).__name__}: {e}"}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("base", help="https://host:8089")
    p.add_argument("--durations", nargs="+", type=float, default=[30, 120, 300, 600])
    p.add_argument("--parallel", type=int, default=4)
    p.add_argument("--cafile", help="CA bundle for the search head's cert")
    p.add_argument("--insecure", action="store_true", help="skip TLS verification (test box only)")
    a = p.parse_args()

    ctx = ssl.create_default_context(cafile=a.cafile)
    if a.insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    report = {"base": a.base, "tls_verified": not a.insecure}
    print("1/4 auth pass-through...", file=sys.stderr)
    report["auth_ok"] = safe(call, a.base, ctx, 1, lines=1)
    report["auth_missing"] = safe(call, a.base, ctx, 1, lines=1, token=None)
    report["auth_wrong"] = safe(call, a.base, ctx, 1, lines=1, token="not-it")
    print("2/4 GET on the AI Toolkit-shaped path...", file=sys.stderr)
    report["get_path"] = safe(call, a.base, ctx, 1, lines=1, method="GET")

    print("3/4 long requests (this takes a while)...", file=sys.stderr)
    report["durations"] = []
    for s in a.durations:
        print(f"    {s:.0f}s...", file=sys.stderr)
        report["durations"].append(safe(call, a.base, ctx, s))

    s = min(a.durations)
    print(f"4/4 {a.parallel} parallel x {s:.0f}s...", file=sys.stderr)
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(a.parallel) as ex:
        results = list(ex.map(lambda _: safe(call, a.base, ctx, s), range(a.parallel)))
    report["parallel"] = {
        "n": a.parallel,
        "each_s": s,
        "wall_s": round(time.time() - t0, 2),
        "results": results,
    }

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
