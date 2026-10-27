"""Admin-only self-check: do the bundled agent libraries load and run here?

GET /services/aitk_local/diagnostics
"""

import importlib
import platform

from aitk_local import bundled, splunkd

REQUIRED_CAPABILITY = "admin_all_objects"
MODULES = [
    "langgraph",
    "langgraph.checkpoint.sqlite",
    "langchain_core",
    "langchain_aws",
    "langchain_openai",
    "langchain_mcp_adapters",
]


def handle(request):
    if request["method"] != "GET":
        return {"status": 405, "payload": {"message": "Method not allowed"}}
    caps = splunkd.capabilities(request["server"]["rest_uri"], request["session"]["authtoken"])
    if REQUIRED_CAPABILITY not in caps:
        return {"status": 403, "payload": {"message": "Insufficient permissions"}}

    bundled.activate()
    imported, errors = [], {}
    for name in MODULES:
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

    class State(TypedDict):
        n: int

    graph = StateGraph(State)
    graph.add_node("inc", lambda s: {"n": s["n"] + 1})
    graph.add_edge(START, "inc")
    graph.add_edge("inc", END)
    try:
        with SqliteSaver.from_conn_string(":memory:") as saver:
            app = graph.compile(checkpointer=saver)
            result = app.invoke({"n": 1}, {"configurable": {"thread_id": "diagnostics"}})
            saved = saver.get({"configurable": {"thread_id": "diagnostics"}})
        return "ok" if result == {"n": 2} and saved is not None else f"unexpected result {result}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"
