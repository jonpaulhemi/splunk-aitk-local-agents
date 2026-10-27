"""AI Toolkit's LLM connection (the invoke payload's `llm` block) as a LangChain chat model.

AI Toolkit's aiagent sends, for a custom OpenAI-compatible connection:
    {provider: "Custom", model, api_key, api_base (endpoint minus /chat/completions),
     response_variability, max_tokens, request_timeout, reasoning_effort, ...}
"""

CUSTOM = "Custom"


class UnsupportedProvider(ValueError):
    pass


def chat_model(block):
    provider = block.get("provider")
    if provider != CUSTOM:
        raise UnsupportedProvider(
            f"LLM provider '{provider}' isn't supported by the local agent runtime yet; "
            "use a Custom (OpenAI-compatible) connection"
        )
    if not block.get("model"):
        raise UnsupportedProvider("The agent's LLM connection has no model; pick one in the agent's settings")
    from langchain_openai import ChatOpenAI

    reasoning = str(block.get("reasoning_effort") or "NONE").lower()
    return ChatOpenAI(
        model=block["model"],
        api_key=block.get("api_key") or "unused",  # local servers like Ollama need none
        base_url=block.get("api_base"),
        temperature=block.get("response_variability"),
        max_tokens=block.get("max_tokens"),
        timeout=block.get("request_timeout"),
        reasoning_effort=None if reasoning == "none" else reasoning,
        max_retries=1,
    )
