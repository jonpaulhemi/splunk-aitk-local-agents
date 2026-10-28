"""AI Toolkit's LLM connection (the invoke payload's `llm` block) as a LangChain chat model.

AI Toolkit's aiagent sends, besides model, response_variability, max_tokens,
request_timeout and reasoning_effort:
- a custom OpenAI-compatible connection: provider "Custom", api_key, and
  api_base (the endpoint minus /chat/completions)
- a Bedrock connection: provider "Bedrock", aws_region, aws_access_key_id,
  aws_access_token (the secret key) and aws_role_arn (optional)
"""

import re

CUSTOM = "Custom"
BEDROCK = "Bedrock"
# AI Toolkit's own Bedrock calls use this session name when assuming the role.
ROLE_SESSION_NAME = "splunk-ai-commander-session-name"
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
    if type(model).__name__ == "ChatOpenAI":
        return model.bind_tools(tools, strict=False)
    return model.bind_tools(tools)


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
    key_id = block.get("aws_access_key_id")
    secret = block.get("aws_access_token")
    if not (region and key_id and secret):
        raise UnusableConnection("The Bedrock connection needs a region, an access key ID and a secret key")
    if not REGION.fullmatch(region):
        raise UnusableConnection(f"The Bedrock connection's region '{region}' isn't an AWS region name "
                                 "such as us-gov-west-1")
    from botocore.config import Config
    from langchain_aws import ChatBedrockConverse

    timeout = block.get("request_timeout") or 60
    # agent.run bounds the whole run; these bound each call.
    config = Config(connect_timeout=timeout, read_timeout=timeout, retries={"total_max_attempts": 2})
    credentials = {"aws_access_key_id": key_id, "aws_secret_access_key": secret, "aws_session_token": None}
    if block.get("aws_role_arn"):
        credentials = _assumed_role(region, key_id, secret, block["aws_role_arn"], config)
    return ChatBedrockConverse(
        model=block["model"],
        region_name=region,
        temperature=block.get("response_variability"),
        max_tokens=block.get("max_tokens"),
        config=config,
        **credentials,
    )


def _assumed_role(region, key_id, secret, role_arn, config):
    """Temporary credentials for `role_arn`, as AI Toolkit gets them for its own Bedrock calls."""
    import boto3

    session = boto3.Session(aws_access_key_id=key_id, aws_secret_access_key=secret, region_name=region)
    found = session.client("sts", config=config).assume_role(RoleArn=role_arn, RoleSessionName=ROLE_SESSION_NAME)
    return {
        "aws_access_key_id": found["Credentials"]["AccessKeyId"],
        "aws_secret_access_key": found["Credentials"]["SecretAccessKey"],
        "aws_session_token": found["Credentials"]["SessionToken"],
    }
