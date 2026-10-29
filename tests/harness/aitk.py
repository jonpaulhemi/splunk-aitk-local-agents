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
    """Create an agent ("triage" unless `name` is given) through AI Toolkit's AgentManager (and so our
    runtime); return its record."""
    manager = aitk.agent_manager.AgentManager(searchinfo(splunkd, admin))
    settings = {"name": "triage", "system_prompt": "You are terse.", **settings}
    assert manager.create_agent(settings)
    return manager.get_agent(settings["name"], is_auth_by_pass=True)


def run_aiagent(aitk, splunkd, admin, agent, llm_config, prompt, rows, mcps=(), skills=(), **params):
    """Run `| aiagent` over `rows` with AI Toolkit's own processor; return the result frame.

    `skills` are {name, description, skill_text}, as AI Toolkit's aiagent
    command resolves them onto the agent's latest version before running it.
    """
    import copy

    import pandas as pd

    agent = copy.deepcopy(agent)
    agent["details"]["versions"][0]["skills"] = list(skills)
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
