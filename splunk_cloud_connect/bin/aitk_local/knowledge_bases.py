"""The agent's knowledge bases: the invoke payload's `knowledge_bases` as search tools.

AI Toolkit sends each AWS_KB knowledge base attached to an agent as a
`vector_dbs` entry: {type, description, kb_id, connection_details: {region,
aws_access_key_id, aws_access_key_token (the secret key), role_arn}}. There's
no knowledge base name, so each tool is named after its kb_id and described
by its description, which is all the model learns about it.

A knowledge base search is Bedrock Agent Runtime's Retrieve: the best-matching
chunks and where they came from, never a generated answer. The agent's own
model does the writing.
"""

import asyncio
import re

from aitk_local import config, log

TYPE = "AWS_KB"
TOOL_PREFIX = "search_knowledge_base_"
MAX_RESULT_CHARS = 8000


def tools(knowledge_bases):
    """One search tool per AWS_KB knowledge base in the payload's `knowledge_bases` block."""
    entries = (knowledge_bases or {}).get("vector_dbs") or []
    found = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != TYPE:
            log.get_logger().info("knowledge base of type %s isn't supported, ignoring it",
                                  entry.get("type") if isinstance(entry, dict) else type(entry).__name__)
            continue
        name = TOOL_PREFIX + re.sub(r"[^A-Za-z0-9_-]", "_", str(entry.get("kb_id") or ""))
        found.setdefault(name[:64], entry)
    return [_tool(name, entry) for name, entry in found.items()]


def _tool(name, entry):
    from langchain_core.tools import StructuredTool, ToolException

    chunks, timeout = config.chunks_per_search(), config.search_timeout_seconds()
    details = entry.get("connection_details") or {}

    async def search(query: str) -> str:
        try:
            found = await asyncio.wait_for(asyncio.to_thread(_retrieve, entry["kb_id"], details, query, chunks,
                                                             timeout), timeout=timeout)
        except TimeoutError:
            raise ToolException(f"The knowledge base search timed out after {timeout}s") from None
        except Exception as e:
            raise ToolException(f"The knowledge base search failed: {_reason(e)}") from None
        return _formatted(found)

    return StructuredTool.from_function(
        coroutine=search, name=name, handle_tool_error=True,
        description=entry.get("description") or f"Search knowledge base {entry.get('kb_id')}.")


def _retrieve(kb_id, details, query, chunks, timeout):
    import boto3
    from botocore.config import Config

    # Bounded by the tool's own timeout; one retry for throttling.
    client = boto3.client("bedrock-agent-runtime", region_name=details.get("region"),
                          aws_access_key_id=details.get("aws_access_key_id"),
                          aws_secret_access_key=details.get("aws_access_key_token"),
                          config=Config(connect_timeout=timeout, read_timeout=timeout,
                                        retries={"total_max_attempts": 2}))
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
        header = f"[{i}] {_source(result.get('location') or {})}" + (f" (score {score:.2f})" if score is not None
                                                                    else "")
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
    """The AWS error code, or the exception type: never the message, which can echo the request."""
    aws = getattr(e, "response", None)
    if isinstance(aws, dict) and aws.get("Error", {}).get("Code"):
        return aws["Error"]["Code"]
    return type(e).__name__
