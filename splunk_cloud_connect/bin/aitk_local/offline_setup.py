"""Turn off AI Toolkit's own traffic to Splunk's cloud (#17).

AI Toolkit syncs agent templates and managed skills from Splunk's cloud every
6 hours and schedules five telemetry searches. One app can't override another
app's settings from its own default/, so an admin runs this once (and again
after an AI Toolkit upgrade) to change them through splunkd's REST API, as
the calling admin. Re-running it changes nothing that is already set.

Optionally it also sets AI Toolkit's domain allowlist. In 6.2.0 that list only
limits which MCP server URLs agents may use; LLM endpoints aren't checked.
"""

from urllib.parse import parse_qsl, quote

from aitk_local import log, splunkd

AITK = "/servicesNS/nobody/Splunk_ML_Toolkit"
SYNC_INPUTS = [("slim_agent_templates", "managed_templates"), ("splunk_managed_skills", "catalog")]
TELEMETRY_SEARCHES = [
    "Splunk_ML_Toolkit - ML-SPL - Telemetry Gen",
    "Splunk_ML_Toolkit - FineTuneCDTSM - Telemetry Gen",
    "Splunk_ML_Toolkit - FineTuneCDTSM - Lineage Telemetry Gen",
    "Splunk_ML_Toolkit - AI Agent - Telemetry Gen",
    "Splunk_ML_Toolkit - ML-SPL - Sourcetype Inference",
]
ALLOWLIST = f"{AITK}/configs/conf-mlspl/{quote('ai:AllowedDomains', safe='')}"


def handle(request):
    if request["method"] != "POST":
        return {"status": 405, "payload": {"message": "POST to run the offline setup"}}
    if not splunkd.is_admin(request):
        return {"status": 403, "payload": {"message": "Insufficient permissions"}}
    form = dict(request.get("form") or [])
    report = _Report(request["server"]["rest_uri"], request["session"]["authtoken"])

    domains = ",".join(d.strip() for d in form.get("allowed_domains", "").split(",") if d.strip())
    try:
        for kind, name in SYNC_INPUTS:
            path = f"{AITK}/data/inputs/{kind}/{quote(name, safe='')}"
            report.setting(f"input {kind}://{name}", path, "disabled", True, f"{path}/disable", {})
        for name in TELEMETRY_SEARCHES:
            path = f"{AITK}/saved/searches/{quote(name, safe='')}"
            report.setting(f"scheduled search {name}", path, "is_scheduled", False, path, {"is_scheduled": "0"})
        if domains:
            report.setting("allowed domains", ALLOWLIST, "allowed_domains", domains,
                           ALLOWLIST, {"allowed_domains": domains})
            report.setting("allowed domains enforcement", ALLOWLIST, "enforce_domain_validation", "true",
                           ALLOWLIST, {"enforce_domain_validation": "true"})
    except splunkd.SplunkdError as e:
        # Keep what was already changed in the reply; a re-run picks up the rest.
        log.get_logger().error("offline setup by %s stopped: %s; changed so far %s",
                               request["session"]["user"], e, [c["setting"] for c in report.changed])
        return {"status": 502, "payload": {"message": f"Offline setup stopped: {e}. Re-run it once fixed.",
                                           **report.as_dict()}}

    log.get_logger().info("offline setup by %s: changed %s, missing %s", request["session"]["user"],
                          [c["setting"] for c in report.changed], report.missing)
    return {"status": 200, "payload": report.as_dict()}


class _Report:
    def __init__(self, rest_uri, token):
        self.rest_uri, self.token = rest_uri, token
        self.changed, self.unchanged, self.missing = [], [], []

    def setting(self, label, path, key, wanted, update_path, update_form):
        """Bring `key` of the entity at `path` to `wanted` by POSTing `update_form`
        to `update_path`, recording what happened."""
        status, body = splunkd.call(self.rest_uri, self.token, "GET", path)
        if status == 404:
            self.missing.append(label)  # e.g. renamed in a later AI Toolkit
            return
        if status != 200:
            raise splunkd.SplunkdError(status, f"reading {label} failed")
        current = body["entry"][0]["content"].get(key)
        if _same(current, wanted):
            self.unchanged.append(label)
            return
        status, _ = splunkd.call(self.rest_uri, self.token, "POST", update_path, postargs=update_form)
        if status not in (200, 201):
            raise splunkd.SplunkdError(status, f"updating {label} failed")
        self.changed.append({"setting": label, "from": _shown(current), "to": _shown(wanted)})

    def as_dict(self):
        return {"changed": self.changed, "unchanged": self.unchanged, "missing": self.missing}


def _same(current, wanted):
    if isinstance(wanted, bool):
        return _truthy(current) == wanted
    return str(current).strip().lower() == wanted.lower()


def _truthy(value):
    return str(value).strip().lower() in ("1", "true", "t", "yes", "y", "on")


def _shown(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return value
