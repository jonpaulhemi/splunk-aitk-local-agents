#!/usr/bin/env python3
"""Release acceptance run against a real search head (#24). Standard library only.

Run it from any machine that can reach the search head's management port,
after installing the release .tgz and restarting splunkd:

    SPLUNK_PASSWORD=... python3 acceptance.py https://sh.example.internal:8089 \\
        --openai-agent AitkLocal070OpenAIMCP --bedrock-agent MyBedrockAgent \\
        --approval-agent AitkLocal0100Approvals --out acceptance.json

The three agents are templates you already have in Agent Launchpad:

- --openai-agent: an OpenAI-compatible connection with a Splunk MCP tool that
  doesn't need approval.
- --bedrock-agent: a Bedrock connection with the same kind of tool.
- --approval-agent: an agent whose MCP tool requires approval.

The script copies their connections into new agents named
aitk_acceptance_<time>_..., runs every check through `| aiagent`, and deletes
the new agents at the end, even when a check fails. The templates aren't
changed.

The report holds pass/fail, counts, ids and timings, never prompts, answers or
tool results. The user needs AI Toolkit's mltk_admin role (or
apply_ai_commander_command) as well as admin. Authenticate with
SPLUNK_PASSWORD (with --user) or SPLUNK_TOKEN. TLS is verified against the
system trust store, or --ca-file; use the host name on splunkd's certificate.
"""

import argparse
import base64
import getpass
import json
import os
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

AGENTS = "/services/mltk/agents"
STAND_IN = "/servicesNS/nobody/splunk_cloud_connect/v1alpha1/cloud-connection/splunk_ml_toolkit"
RESUME_PROMPT = "Tool approval decisions submitted."
DEFAULT_MCP_QUESTION = ("Use the Splunk search tool to count the events in index=_internal over the last "
                        "15 minutes. Reply with just the number.")
STRUCTURED_OUTPUT = {"fields": {
    "greeting": {"type": "string", "required": True, "is_array": False},
    "word_count": {"type": "integer", "required": True, "is_array": False},
}}


class Failed(Exception):
    pass


class Splunkd:
    def __init__(self, base, user=None, password=None, token=None, ca_file=None):
        self.base = base.rstrip("/")
        self.auth = f"Bearer {token}" if token else \
            "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.context = ssl.create_default_context(cafile=ca_file)

    def call(self, method, path, query=None, form=None, body=None, timeout=120, raw=False):
        """(status, parsed JSON, or the text when `raw` or not JSON) for one REST call."""
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"Authorization": self.auth}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=timeout) as response:
                return response.status, _parse(response.read(), raw)
        except urllib.error.HTTPError as e:
            return e.code, _parse(e.read(), raw)

    def search(self, spl, hide=(), earliest="-24h", timeout=900):
        """The final result rows of a search, run with search/jobs/export.

        Splunk's error text can repeat the search, so the search and `hide`
        (prompts) are cut out of any error before it reaches the report.
        """
        status, text = self.call("POST", "/services/search/jobs/export", form={
            "search": spl, "output_mode": "json", "earliest_time": earliest, "preview": "false"},
            timeout=timeout, raw=True)
        if status != 200:
            raise Failed(f"search returned HTTP {status}: {_scrub(text, spl, *hide)}")
        rows, errors = [], []
        for line in text.splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("result") and not item.get("preview"):
                rows.append(item["result"])
            errors += [m.get("text", "") for m in item.get("messages", []) if m.get("type") in ("ERROR", "FATAL")]
        if errors and not rows:
            raise Failed("search failed: " + "; ".join(_scrub(e, spl, *hide) for e in errors))
        return rows


def _parse(raw, as_text=False):
    text = raw.decode("utf-8", "replace")
    if as_text:
        return text
    try:
        return json.loads(text)
    except ValueError:
        return text


def _scrub(text, *secrets):
    """`text`, shortened, with each of `secrets` cut out."""
    text = text if isinstance(text, str) else json.dumps(text)
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        text = text.replace(secret, "[...]").replace(_quote(secret), "[...]")
    return _short(text)


def _short(value, limit=300):
    text = value if isinstance(value, str) else json.dumps(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def aiagent(splunkd, agent, prompt, session_id=None, hitl_input=None):
    """One `| aiagent` row for `prompt`, as a dict of AI Toolkit's result fields."""
    spl = f"| makeresults | aiagent agent_name={_quote(agent)} prompt={_quote(prompt)} conversation_tool=true"
    if session_id:
        spl += f" session_id={_quote(session_id)}"
    if hitl_input is not None:
        spl += f" hitl_input={_quote(json.dumps(hitl_input))}"
    rows = splunkd.search(spl, hide=(prompt,))
    if len(rows) != 1:
        raise Failed(f"expected 1 aiagent row, got {len(rows)}")
    row = rows[0]
    if row.get("status") != "success":
        raise Failed(f"aiagent status {row.get('status')!r}: {_scrub(row.get('error') or '', prompt)}")
    return row


def _trace(row):
    try:
        return json.loads(row.get("trace") or "[]")
    except ValueError:
        return []


def _succeeded_calls(row):
    """Tool calls in the row's trace that ran and succeeded."""
    return [t for t in _trace(row) if t.get("tool_call_status") == "success"]


class Run:
    def __init__(self, splunkd, args):
        self.splunkd = splunkd
        self.args = args
        self.prefix = "aitk_acceptance_" + time.strftime("%Y%m%d%H%M%S", time.gmtime())
        self.created = []
        self.checks = {}
        self.runtime_ids = {}
        # Given in memory checks, then looked for in aitk_local.log.
        self.code_words = []

    def check(self, name, fn):
        started = time.monotonic()
        try:
            outcome = {"pass": True, **(fn() or {})}
        except Failed as e:
            outcome = {"pass": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001 (a broken check must not stop cleanup)
            # Only the type: the text of an unexpected error could hold anything.
            outcome = {"pass": False, "error": type(e).__name__}
        self.checks[name] = {**outcome, "seconds": round(time.monotonic() - started, 1)}
        result = "PASS" if self.checks[name]["pass"] else "FAIL"
        print(f"{result} {name}" + ("" if self.checks[name]["pass"] else f": {self.checks[name]['error']}"),
              file=sys.stderr)
        return self.checks[name]["pass"]

    # Install and setup

    def installed(self):
        status, body = self.splunkd.call("GET", "/services/apps/local/splunk_cloud_connect",
                                         {"output_mode": "json"})
        if status != 200:
            raise Failed(f"splunk_cloud_connect isn't installed (HTTP {status})")
        content = body["entry"][0]["content"]
        if self.args.expect_version and content.get("version") != self.args.expect_version:
            raise Failed(f"installed version {content.get('version')}, expected {self.args.expect_version}")
        if str(content.get("disabled")).lower() in ("1", "true"):
            raise Failed("the app is disabled")
        status, body = self.splunkd.call("GET", STAND_IN, {"output_mode": "json"})
        if status != 200 or _first_content(body).get("status") != "activated":
            raise Failed(f"the Cloud Connect stand-in didn't answer as activated (HTTP {status})")
        return {"version": content.get("version")}

    def setup(self):
        """Run setup, then again: the second run must find AI Toolkit's two
        sync inputs and five telemetry searches all already off."""
        replies = []
        for _ in range(2):
            status, body = self.splunkd.call("POST", "/services/aitk_local/offline_setup", {"output_mode": "json"},
                                             form={})
            if status != 200 or not isinstance(body, dict):
                raise Failed(f"offline_setup returned HTTP {status}: {_short(body)}")
            replies.append(body)
        first, second = replies
        if second.get("changed") or second.get("missing") or len(second.get("unchanged") or []) < 7:
            raise Failed(f"a second setup run didn't find everything off: {_short(second)}")
        return {"changed": [c.get("setting") for c in first.get("changed") or []],
                "already_off": second.get("unchanged")}

    def diagnostics(self):
        status, body = self.splunkd.call("GET", "/services/aitk_local/diagnostics", {"output_mode": "json"})
        if status != 200:
            raise Failed(f"diagnostics returned HTTP {status}")
        if body.get("errors") or body.get("graph_run") != "ok" or body.get("shadowed"):
            raise Failed(f"diagnostics: errors={body.get('errors')} graph_run={body.get('graph_run')} "
                         f"shadowed={body.get('shadowed')}")
        return {"python": body.get("python"), "imported": len(body.get("imported") or [])}

    # Agents

    def create(self, kind, template, with_tools=True, **extra):
        status, body = self.splunkd.call("GET", AGENTS, {"name": template})
        if status != 200:
            raise Failed(f"template agent {template!r} not found (HTTP {status})")
        latest = body["versions"][0]
        name = f"{self.prefix}_{kind}"
        settings = {
            "name": name,
            "description": f"Release acceptance run, copied from {template}; deleted afterwards.",
            "system_prompt": latest.get("system_prompt") or "You are a helpful assistant.",
            "llm": latest.get("llm", {}),
            "mcps": (latest.get("tools") or {}).get("mcps", []) if with_tools else [],
            "agent_timeout": latest.get("agent_timeout", 300),
            **extra,
        }
        if with_tools and not settings["mcps"]:
            raise Failed(f"template agent {template!r} has no MCP connection")
        status, body = self.splunkd.call("POST", AGENTS, body=settings, timeout=660)
        if status != 200:
            raise Failed(f"creating {name} returned HTTP {status}: {_short(body)}")
        self.created.append(name)
        deadline = time.monotonic() + self.args.ready_timeout
        while time.monotonic() < deadline:
            status, body = self.splunkd.call("GET", AGENTS, {"name": name})
            version = body["versions"][0] if status == 200 else {}
            if version.get("state") == "Available":
                self.runtime_ids[kind] = (version.get("runtime_params") or {}).get("runtime_id")
                return name
            if version.get("state") == "Failed":
                raise Failed(f"{name} failed to become available")
            time.sleep(3)
        raise Failed(f"{name} wasn't available after {self.args.ready_timeout}s")

    def delete_all(self):
        left = []
        for name in self.created:
            status, _ = self.splunkd.call("DELETE", f"{AGENTS}/{urllib.parse.quote(name)}", timeout=660)
            deadline = time.monotonic() + 120
            while status in (200, 202) and time.monotonic() < deadline:
                status, _ = self.splunkd.call("GET", AGENTS, {"name": name})
                if status == 404:
                    break
                time.sleep(3)
            if status != 404:
                left.append(name)
        if left:
            raise Failed(f"not deleted: {left}")
        return {"deleted": len(self.created)}

    # Checks on one agent

    def answers(self, agent):
        row = aiagent(self.splunkd, agent, "Reply with exactly the word READY and nothing else.")
        if "ready" not in (row.get("result_1") or "").lower():
            raise Failed("the answer didn't contain READY")
        return {}

    def uses_mcp(self, agent):
        row = aiagent(self.splunkd, agent, self.args.mcp_question)
        ran = _succeeded_calls(row)
        if not ran:
            raise Failed(f"no MCP tool call succeeded (trace has {len(_trace(row))} entries)")
        if not (row.get("result_1") or "").strip():
            raise Failed("empty answer after the tool call")
        return {"tool_calls": len(ran), "tools": sorted({t.get("name") for t in ran})}

    def remembers(self, agent):
        word = "kestrel" + secrets.token_hex(3)
        self.code_words.append(word)
        session = f"{self.prefix}_{secrets.token_hex(4)}"
        aiagent(self.splunkd, agent, f"Remember this code word for later: {word}. Reply OK.", session)
        row = aiagent(self.splunkd, agent, "What code word did I give you? Reply with just the word.", session)
        if word not in (row.get("result_1") or "").lower():
            raise Failed("the second turn didn't recall the code word")
        return {}

    def structured(self, agent):
        row = aiagent(self.splunkd, agent, "Greet me in one short sentence, and count the words in your greeting.")
        try:
            answer = json.loads(row.get("result_1") or "")
        except ValueError:
            raise Failed("result_1 isn't JSON") from None
        if not (isinstance(answer, dict) and isinstance(answer.get("greeting"), str)
                and isinstance(answer.get("word_count"), int)):
            raise Failed(f"result_1 doesn't match the schema (keys {sorted(answer) if isinstance(answer, dict) else type(answer).__name__})")
        return {}

    def approval(self, agent):
        session = f"{self.prefix}_{secrets.token_hex(4)}"
        paused = aiagent(self.splunkd, agent, self.args.mcp_question, session)
        if paused.get("hitl_status") != "waiting_for_approval":
            raise Failed(f"the run didn't pause (hitl_status={paused.get('hitl_status')!r})")
        pending = json.loads(paused.get("hitl_pending") or "[]")
        if not pending or _succeeded_calls(paused):
            raise Failed(f"expected pending calls and none run; pending={len(pending)} "
                         f"ran={len(_succeeded_calls(paused))}")
        ids = [p["tool_call_id"] for p in pending]
        decisions = {"decisions": [{"tool_call_id": i, "approved": True} for i in ids]}
        # The model may ask for another gated call afterwards; that's allowed,
        # but each approved call must have run exactly once.
        resumed = aiagent(self.splunkd, agent, RESUME_PROMPT, session, hitl_input=decisions)
        ran = [t.get("tool_call_id") for t in _succeeded_calls(resumed)]
        if sorted(i for i in ran if i in ids) != sorted(ids):
            raise Failed(f"approved calls ran {sum(i in ids for i in ran)} times, expected {len(ids)}")
        return {"pending": len(ids), "tools": sorted({p.get("name") for p in pending})}

    # Evidence from aitk_local.log (indexed into _internal)

    def bedrock_region(self):
        runtime_id = self.runtime_ids.get("bedrock")
        spl = (f"search index=_internal source=*aitk_local.log \"invoke completed\" runtime_id={runtime_id} "
               "provider=Bedrock | rex \"region=(?<region>\\S+)\" | stats values(region) as regions")
        regions = []
        deadline = time.monotonic() + 90  # the log is indexed a little after it's written
        while not regions and time.monotonic() < deadline:
            rows = self.splunkd.search(spl)
            regions = rows[0].get("regions") if rows else None
            regions = [regions] if isinstance(regions, str) else (regions or [])
            if not regions:
                time.sleep(10)
        if not regions:
            raise Failed("no Bedrock invoke with a region found in aitk_local.log (index=_internal)")
        return {"regions": regions}

    def log_privacy(self):
        if not self.code_words:
            raise Failed("no memory check ran, so there's nothing to look for")
        # Wait until this run's invokes are indexed, so an empty result means something.
        runtime_ids = " OR ".join(f"runtime_id={r}" for r in self.runtime_ids.values() if r)
        deadline = time.monotonic() + 90
        while True:
            rows = self.splunkd.search(f"search index=_internal source=*aitk_local.log \"invoke completed\" "
                                       f"({runtime_ids}) | stats count")
            if rows and int(rows[0].get("count", 0)):
                break
            if time.monotonic() > deadline:
                raise Failed("this run's invokes never showed up in index=_internal")
            time.sleep(10)
        terms = " OR ".join(f"\"{w}\"" for w in self.code_words)
        rows = self.splunkd.search(f"search index=_internal source=*aitk_local.log ({terms}) | stats count")
        count = int(rows[0].get("count", 0)) if rows else 0
        if count:
            raise Failed(f"a prompt's code word appears in aitk_local.log {count} times")
        return {}


def _first_content(body):
    if isinstance(body, dict) and body.get("entry"):
        return body["entry"][0].get("content", {})
    return body if isinstance(body, dict) else {}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base", help="https://<search head host>:8089")
    parser.add_argument("--openai-agent", required=True)
    parser.add_argument("--bedrock-agent", required=True)
    parser.add_argument("--approval-agent", required=True)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--ca-file")
    parser.add_argument("--expect-version", help="fail unless this app version is installed")
    parser.add_argument("--mcp-question", default=DEFAULT_MCP_QUESTION)
    parser.add_argument("--ready-timeout", type=int, default=180)
    parser.add_argument("--out", help="write the JSON report here as well as printing it")
    args = parser.parse_args()

    token = os.environ.get("SPLUNK_TOKEN")
    password = None if token else (os.environ.get("SPLUNK_PASSWORD") or getpass.getpass(f"{args.user} password: "))
    run = Run(Splunkd(args.base, args.user, password, token, args.ca_file), args)
    started = time.time()

    if run.check("installed", run.installed) and run.check("setup", run.setup):
        run.check("diagnostics", run.diagnostics)
        agents = {}
        for kind, template, extra in (
                ("openai", args.openai_agent, {}),
                ("bedrock", args.bedrock_agent, {}),
                ("approval", args.approval_agent, {}),
                ("structured", args.openai_agent, {"with_tools": False, "structured_output": STRUCTURED_OUTPUT})):
            def create(kind=kind, template=template, extra=extra):
                agents[kind] = run.create(kind, template, **extra)
                return {"agent": agents[kind]}
            run.check(f"create_{kind}", create)
        for kind in ("openai", "bedrock"):
            if kind in agents:
                run.check(f"{kind}_answer", lambda: run.answers(agents[kind]))
                run.check(f"{kind}_mcp_tool", lambda: run.uses_mcp(agents[kind]))
                run.check(f"{kind}_session_memory", lambda: run.remembers(agents[kind]))
        if "structured" in agents:
            run.check("structured_output", lambda: run.structured(agents["structured"]))
        if "approval" in agents:
            run.check("approval", lambda: run.approval(agents["approval"]))
        if "bedrock" in agents:
            run.check("bedrock_region", run.bedrock_region)
        run.check("log_privacy", run.log_privacy)
    if run.created:
        run.check("delete_agents", run.delete_all)

    report = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "duration_s": round(time.time() - started, 1),
        "search_head": urllib.parse.urlparse(args.base).hostname,
        "templates": {"openai": args.openai_agent, "bedrock": args.bedrock_agent, "approval": args.approval_agent},
        "pass": all(c["pass"] for c in run.checks.values()),
        "checks": run.checks,
    }
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
    sys.exit(0 if report["pass"] else 1)


if __name__ == "__main__":
    main()
