"""AI Toolkit's LLM connection (the invoke payload's `llm` block) as a LangChain chat model.

AI Toolkit's aiagent sends, besides model, response_variability, max_tokens,
request_timeout and reasoning_effort:
- a custom OpenAI-compatible connection: provider "Custom", api_key, and
  api_base (the endpoint minus /chat/completions)
- a Bedrock connection: provider "Bedrock", aws_region, aws_access_key_id,
  aws_access_token (the secret key) and aws_role_arn (optional). An
  instance-role connection has null keys and a role ARN.
"""

import re

from aitk_local import aws_credentials

CUSTOM = "Custom"
BEDROCK = "Bedrock"
REGION = re.compile(r"[a-z]{2}(-gov|-iso[a-z]*)?-[a-z]+-\d+")


class UnusableConnection(ValueError):
    """The connection can't be used as given; the message says why, without secrets."""


def chat_model(block):
    provider = block.get("provider")
    if provider not in (CUSTOM, BEDROCK):
        raise UnusableConnection(
            f"LLM provider '{provider}' isn't supported by the local agent runtime yet; "
            "use a Custom (OpenAI-compatible) or Bedrock connection"
        )
    if not block.get("model"):
        raise UnusableConnection("The agent's LLM connection has no model; pick one in the agent's settings")
    return _bedrock(block) if provider == BEDROCK else _openai_compatible(block)


def with_tools(model, tools):
    """`model` able to call `tools`.

    OpenAI connections bind them non-strict: LangChain sends some models
    (gpt-6 with tools) to the Responses API, which treats function tools as
    strict unless told otherwise, and strict mode rejects MCP schemas with
    optional arguments, such as the Splunk MCP Server's splunk_run_query.
    """
    if not tools:
        return model
    if _is_openai(model):
        return model.bind_tools(tools, strict=False)
    return model.bind_tools(tools)


def structured(model, json_schema):
    """`model` answering in `json_schema`'s shape through a tool call, which
    every provider here supports; the result is {"raw", "parsed", "parsing_error"}.
    Non-strict on OpenAI, for the reason in with_tools."""
    if _is_openai(model):
        return model.with_structured_output(json_schema, method="function_calling", strict=False, include_raw=True)
    return model.with_structured_output(json_schema, include_raw=True)


def _is_openai(model):
    return type(model).__name__ == "ChatOpenAI"


def _openai_compatible(block):
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


def _bedrock(block):
    region = block.get("aws_region")
    if not region:
        raise UnusableConnection("The Bedrock connection needs a region")
    if not REGION.fullmatch(region):
        raise UnusableConnection(f"The Bedrock connection's region '{region}' isn't an AWS region name "
                                 "such as us-gov-west-1")
    from botocore.config import Config
    from langchain_aws import ChatBedrockConverse

    timeout = block.get("request_timeout") or 60
    # agent.run bounds the whole run; these bound each call.
    config = Config(connect_timeout=timeout, read_timeout=timeout, retries={"total_max_attempts": 2})
    try:
        credentials = aws_credentials.for_connection(region, block.get("aws_access_key_id"),
                                                     block.get("aws_access_token"), block.get("aws_role_arn"),
                                                     config)
    except aws_credentials.IncompleteKeys:
        raise UnusableConnection("The Bedrock connection needs both an access key ID and a secret key, "
                                 "or neither to use the instance role") from None
    except aws_credentials.MissingRoleArn:
        raise UnusableConnection("The Bedrock connection has no access keys, so it uses the instance role "
                                 "and needs a role ARN") from None
    except aws_credentials.NoInstanceCredentials:
        raise UnusableConnection("The Bedrock connection uses the instance role, but no instance credentials "
                                 "were found: attach an instance profile and allow IMDSv2") from None
    except aws_credentials.AssumeRoleFailed as e:
        raise UnusableConnection(f"The Bedrock connection couldn't be used: {e}") from None
    return ChatBedrockConverse(
        model=block["model"],
        region_name=region,
        temperature=block.get("response_variability"),
        max_tokens=block.get("max_tokens"),
        config=config,
        **credentials,
    )
