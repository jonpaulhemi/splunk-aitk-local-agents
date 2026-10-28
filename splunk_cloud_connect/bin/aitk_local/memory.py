"""Session memory: a chat's earlier turns, kept between runs (#22).

LangGraph's SQLite checkpointer keeps each session's conversation in
$SPLUNK_HOME/var/lib/aitk_local/memory.db, so every splunkd handler process
sees it and it survives restarts. A session is the agent's memory_id plus AI
Toolkit's session_id, as in AgentCore, so two agents given the same
session_id don't share it. Sessions unused for longer than
[memory] session_retention_hours are deleted at the start of each run.
"""

import contextlib
import time

from aitk_local import config, records

# What the model sees of each tool result from earlier turns; the current
# turn's are cut to [agent] max_tool_result_chars by mcp_tools.
EARLIER_TOOL_RESULT_CHARS = 2000


@contextlib.asynccontextmanager
async def session(payload):
    """(checkpointer, LangGraph config) for the payload's session."""
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    thread_id = f"{payload.get('memory_id') or payload.get('runtime_id')}/{payload.get('session_id')}"
    # Other handler processes may be writing; wait for them rather than fail.
    async with aiosqlite.connect(records.state_file("memory.db"), timeout=30) as db:
        saver = AsyncSqliteSaver(db)
        await saver.setup()
        await db.execute("CREATE TABLE IF NOT EXISTS sessions (thread_id TEXT PRIMARY KEY, last_used REAL NOT NULL)")
        await _expire(db, saver)
        await db.execute("INSERT INTO sessions VALUES (?, ?) ON CONFLICT(thread_id) DO UPDATE SET last_used = excluded.last_used",
                         (thread_id, time.time()))
        await db.commit()
        before = await _latest_checkpoint(db, thread_id)
        try:
            yield saver, {"configurable": {"thread_id": thread_id}}
        except BaseException:
            # A failed or timed-out turn isn't remembered, so it can't leave the
            # session ending in tool calls that were never answered.
            await _forget_since(db, thread_id, before)
            raise


# Checkpoint ids sort in the order they were written; LangGraph's SQLite
# checkpointer relies on that too, and has no call to drop a single checkpoint.
async def _latest_checkpoint(db, thread_id):
    (latest,) = await (await db.execute("SELECT max(checkpoint_id) FROM checkpoints WHERE thread_id = ?",
                                        (thread_id,))).fetchone()
    return latest or ""


async def _forget_since(db, thread_id, checkpoint_id):
    for table in ("checkpoints", "writes"):
        await db.execute(f"DELETE FROM {table} WHERE thread_id = ? AND checkpoint_id > ?", (thread_id, checkpoint_id))
    await db.commit()


async def _expire(db, saver):
    cutoff = time.time() - config.session_retention_hours() * 3600
    expired = await db.execute_fetchall("SELECT thread_id FROM sessions WHERE last_used < ?", (cutoff,))
    for (thread_id,) in expired:
        await saver.adelete_thread(thread_id)
    await db.execute("DELETE FROM sessions WHERE last_used < ?", (cutoff,))


def model_view(messages):
    """`messages` as the model is sent them: earlier turns' tool results cut
    short, so a long investigation doesn't crowd the context of every turn after it."""
    from langchain_core.messages import HumanMessage, ToolMessage

    turn_start = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=0)
    return [_cut(m) if i < turn_start and isinstance(m, ToolMessage) else m for i, m in enumerate(messages)]


def _cut(message):
    text = message.text
    if len(text) <= EARLIER_TOOL_RESULT_CHARS:
        return message
    return message.model_copy(update={"content": text[:EARLIER_TOOL_RESULT_CHARS] + (
        f"\n[Result from an earlier turn, cut to {EARLIER_TOOL_RESULT_CHARS} of {len(text)} characters. "
        "Run the tool again to see more.]")})
