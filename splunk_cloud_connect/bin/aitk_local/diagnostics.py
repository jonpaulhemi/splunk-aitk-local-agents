"""Admin-only self-check: do the bundled agent libraries load and run here?

GET /services/aitk_local/diagnostics

Imports every top-level module in lib/ (so every compiled extension gets
loaded by this Python), then runs a one-step SQLite-checkpointed graph.
"""

import importlib
import platform

from aitk_local import bundled, splunkd

# Submodules the agent engine needs that importing the top level doesn't load.
SUBMODULES = ["langgraph.checkpoint.sqlite", "langgraph.graph", "langchain_mcp_adapters.client"]


def handle(request):
    if request["method"] != "GET" or request.get("path_info"):
        return {"status": 404, "payload": {"message": "Not found"}}
    if not splunkd.is_admin(request):
        return {"status": 403, "payload": {"message": "Insufficient permissions"}}

    bundled.activate()
    imported, errors = [], {}
    for name in bundled.top_level_modules() + SUBMODULES:
        try:
            importlib.import_module(name)
            imported.append(name)
        except Exception as e:
            errors[name] = f"{type(e).__name__}: {e}"
    return {
        "status": 200,
        "payload": {
            "python": platform.python_version(),
            "imported": imported,
            "errors": errors,
            "graph_run": _run_graph() if not errors else "skipped",
            "shadowed": bundled.shadowed(),
        },
    }


def _run_graph():
    """One step of a checkpointed LangGraph, which exercises the compiled deps."""
    from typing import TypedDict

    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import END, START, StateGraph

    class Counter(TypedDict):
        count: int

    graph = StateGraph(Counter)
    graph.add_node("inc", lambda state: {"count": state["count"] + 1})
    graph.add_edge(START, "inc")
    graph.add_edge("inc", END)
    try:
        with SqliteSaver.from_conn_string(":memory:") as saver:
            app = graph.compile(checkpointer=saver)
            result = app.invoke({"count": 1}, {"configurable": {"thread_id": "diagnostics"}})
            saved = saver.get({"configurable": {"thread_id": "diagnostics"}})
        return "ok" if result == {"count": 2} and saved is not None else f"unexpected result {result}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"
