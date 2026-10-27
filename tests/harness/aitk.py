"""Helpers for calling AI Toolkit's own code from tests."""


def searchinfo(splunkd, token, username="admin", with_system_token=True):
    """The searchinfo dict AI Toolkit builds for a command or REST call."""
    return {
        "splunkd_uri": splunkd.uri,
        "session_key": token,
        "admin_session_key": splunkd.system_token if with_system_token else None,
        "app": "Splunk_ML_Toolkit",
        "username": username,
    }


def runtime_base_url(splunkd):
    """Where AI Toolkit should send agent calls: this app's local runtime."""
    return f"https://localhost:{splunkd.port}/services/aitk_local/slim-api/v1alpha1"
