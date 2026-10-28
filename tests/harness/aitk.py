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


def create_agent(aitk, splunkd, admin, **settings):
    """Create agent "triage" through AI Toolkit's AgentManager (and so our runtime); return its record."""
    manager = aitk.agent_manager.AgentManager(searchinfo(splunkd, admin))
    assert manager.create_agent({"name": "triage", "system_prompt": "You are terse.", **settings})
    return manager.get_agent("triage", is_auth_by_pass=True)


def run_aiagent(aitk, splunkd, admin, agent, llm_config, prompt, rows, mcps=(), **params):
    """Run `| aiagent` over `rows` with AI Toolkit's own processor; return the result frame."""
    import pandas as pd

    options = {
        "params": {"agent_name": agent["name"], "prompt": prompt, **params},
        "agent_config": agent,
        "mcp_configs": {"mcps": list(mcps)},
        "kb_configs": {"knowledge_bases": {"vector_dbs": []}},
        "llm_config": llm_config,
    }
    processor = aitk.ai_agent.AiAgentProcessor(options, searchinfo(splunkd, admin))
    processor.df = pd.DataFrame(rows)
    processor.process()
    return processor.df
