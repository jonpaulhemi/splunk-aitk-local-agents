"""The agent's knowledge bases: the invoke payload's `knowledge_bases` as search tools.

AI Toolkit sends each AWS_KB knowledge base attached to an agent as a
`vector_dbs` entry: {type, description, kb_id, connection_details: {region,
aws_access_key_id, aws_access_key_token (the secret key), role_arn}}. There's
no knowledge base name, so each tool is named after its kb_id and described
by its description, which is all the model learns about it.

A knowledge base search is Bedrock Agent Runtime's Retrieve: the best-matching
chunks and where they came from, never a generated answer. The agent's own
model does the writing. With a role_arn, searches run as that role, assumed
once when the run starts.
"""

import asyncio
import re

from aitk_local import aws_credentials, config, log

TYPE = "AWS_KB"
TOOL_PREFIX = "search_knowledge_base_"
MAX_RESULT_CHARS = 8000


class Unusable(Exception):
    """A knowledge base can't be used at all; the message names it and the AWS error code, never secrets."""


def tools(knowledge_bases):
    """One search tool per AWS_KB knowledge base in the payload's `knowledge_bases` block.

    Blocking: it assumes the knowledge bases' roles."""
    entries = (knowledge_bases or {}).get("vector_dbs") or []
    by_id = {}
    for entry in entries:
        kind = entry.get("type") if isinstance(entry, dict) else type(entry).__name__
        if kind != TYPE or not entry.get("kb_id"):
            log.get_logger().info("knowledge base of type %s%s isn't supported, ignoring it", kind,
                                  "" if kind != TYPE else " without a kb_id")
            continue
        by_id.setdefault(str(entry["kb_id"]), entry)
    return [_tool(kb_id, entry) for kb_id, entry in by_id.items()]


def _tool(kb_id, entry):
    from langchain_core.tools import StructuredTool, ToolException

    chunks, timeout = config.chunks_per_search(), config.search_timeout_seconds()
    client = _client(kb_id, entry.get("connection_details") or {}, timeout)

    async def search(query: str) -> str:
        if client is None:
            raise ToolException("The knowledge base connection needs a region, an access key ID and a secret key")
        try:
            results = await asyncio.wait_for(asyncio.to_thread(_search, client, kb_id, query, chunks),
                                             timeout=timeout)
        except Exception as e:
            if isinstance(e, TimeoutError) or type(e).__name__ in ("ReadTimeoutError", "ConnectTimeoutError"):
                raise ToolException(f"The knowledge base search timed out after {timeout}s") from None
            raise ToolException(f"The knowledge base search failed: {_reason(e)}") from None
        return _formatted(results)

    name = (TOOL_PREFIX + re.sub(r"[^A-Za-z0-9_-]", "_", kb_id))[:64]
    return StructuredTool.from_function(coroutine=search, name=name, handle_tool_error=True,
                                        description=entry.get("description") or f"Search knowledge base {kb_id}.")


def _client(kb_id, details, timeout):
    """A Bedrock Agent Runtime client for one knowledge base's connection, made
    once per run; None if the connection lacks credentials."""
    region = details.get("region")
    if not region:
        return None
    import boto3
    from botocore.config import Config

    # Each search is bounded by the tool's own timeout; one retry for throttling.
    boto_config = Config(connect_timeout=timeout, read_timeout=timeout, retries={"total_max_attempts": 2})
    try:
        credentials = aws_credentials.for_connection(region, details.get("aws_access_key_id"),
                                                     details.get("aws_access_key_token"), details.get("role_arn"),
                                                     boto_config)
    except aws_credentials.MissingKeys:
        return None
    except aws_credentials.AssumeRoleFailed as e:
        raise Unusable(f"Knowledge base {kb_id} couldn't be used: {e}") from None
    return boto3.client("bedrock-agent-runtime", region_name=region, config=boto_config, **credentials)


def _search(client, kb_id, query, chunks):
    response = client.retrieve(knowledgeBaseId=kb_id, retrievalQuery={"text": query},
                               retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": chunks}})
    return response.get("retrievalResults") or []


def _formatted(results):
    """The chunks for the model, in Bedrock's order, each with its source and score."""
    if not results:
        return "No chunks matched the query."
    parts = []
    for i, result in enumerate(results, 1):
        score = result.get("score")
        header = f"[{i}] {_source(result.get('location') or {})}"
        if score is not None:
            header += f" (score {score:.2f})"
        parts.append(f"{header}\n{(result.get('content') or {}).get('text') or ''}")
    text = "\n\n".join(parts)
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + (f"\n[Result cut to {MAX_RESULT_CHARS} of {len(text)} characters to fit the "
                                      "model's context. Search with a narrower query to see more.]")


def _source(location):
    for key, field in (("s3Location", "uri"), ("webLocation", "url"), ("confluenceLocation", "url"),
                       ("salesforceLocation", "url"), ("sharePointLocation", "url"), ("customDocumentLocation", "id")):
        value = (location.get(key) or {}).get(field)
        if value:
            return value
    return "unknown source"


def _reason(e):
    return aws_credentials.reason(e)
