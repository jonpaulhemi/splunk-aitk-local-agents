"""Cloud Connect stand-in.

AI Toolkit decides whether agents are available by asking Cloud Connect for
its connection details, then builds the agent service URL as
https://{tenantApiHostname}/{tenantName}/slim-api/v1alpha1. We answer with
this search head and a tenant name that routes that URL to our runtime.
"""

from aitk_local import config, credentials, splunkd

# https://host:port/services/aitk_local/slim-api/v1alpha1 → this app's runtime.
TENANT_NAME = "services/aitk_local"
AITK_CONNECTION = "splunk_ml_toolkit"


def handle(request):
    if request["method"] != "GET" or request.get("path_info", "").strip("/") != AITK_CONNECTION:
        return {"status": 404, "payload": {"message": "Not found"}}

    # Like the real Cloud Connect, only admins may read the details. AI Toolkit
    # calls with the system token, and relays for its non-admin users itself.
    if not splunkd.is_admin(request):
        return {"status": 403, "payload": {"message": "Insufficient permissions"}}

    rest_uri = request["server"]["rest_uri"]

    token = credentials.runtime_token(rest_uri, request["system_authtoken"])
    return {
        "status": 200,
        "payload": {
            "status": "activated",
            "token": token,
            "tenantName": TENANT_NAME,
            "tenantApiHostname": config.advertised_host_and_port(rest_uri),
        },
    }
