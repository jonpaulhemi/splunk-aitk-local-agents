"""Later turns of a session remember the earlier ones (#22).

AI Toolkit reuses one session_id across a Launchpad chat's turns, and across
`| aiagent` searches given `session_id=`. Each invoke runs in a fresh worker
process, so a turn remembered here was remembered across processes, as it
must be across splunkd's handler processes and restarts.
"""

import sqlite3
import threading
import time
import uuid

import pytest

from harness.aitk import create_agent, run_aiagent
from harness.fake_llm import FakeLLM, last_user_message

ADMIN_CAPS = ["admin_all_objects", "apply_ai_commander_command"]


@pytest.fixture
def admin(splunkd):
    return splunkd.add_user("admin", ADMIN_CAPS, roles=["admin"])


@pytest.fixture
def llm():
    server = FakeLLM().start()
    server.script = lambda request: f"You said: {last_user_message(request)}"
    yield server
    server.stop()


@pytest.fixture
def agent(aitk, splunkd, admin):
    return create_agent(aitk, splunkd, admin)


def _llm_block(llm):
    return {"llm": {"provider": "Custom", "model": "qwen3:4b", "api_key": "sk-fake", "api_base": f"{llm.url}/v1",
                    "request_timeout": 30, "reasoning_effort": "NONE"}}


def _session():
    return f"chat-{uuid.uuid4().hex}"


def _turn(aitk, splunkd, admin, agent, llm, prompt, session_id):
    df = run_aiagent(aitk, splunkd, admin, agent, _llm_block(llm), prompt, [{"x": 1}], session_id=session_id)
    assert df.iloc[0]["status"] == "success", df.iloc[0].get("error")
    return df.iloc[0]["result_1"]


def _conversation(llm, prompt):
    """The messages the model was sent on the turn that asked `prompt`."""
    body = next(r["body"] for r in reversed(llm.requests) if last_user_message(r["body"]) == prompt)
    return [(m["role"], m["content"]) for m in body["messages"]]


def test_a_later_turn_in_the_same_session_sees_the_earlier_ones(aitk, splunkd, admin, agent, llm):
    session = _session()

    _turn(aitk, splunkd, admin, agent, llm, "My name is Ada.", session)
    _turn(aitk, splunkd, admin, agent, llm, "I work on the SOC team.", session)
    _turn(aitk, splunkd, admin, agent, llm, "What is my name?", session)

    assert _conversation(llm, "What is my name?") == [
        ("system", "You are terse."),
        ("user", "My name is Ada."),
        ("assistant", "You said: My name is Ada."),
        ("user", "I work on the SOC team."),
        ("assistant", "You said: I work on the SOC team."),
        ("user", "What is my name?"),
    ]


def test_another_session_does_not_see_it(aitk, splunkd, admin, agent, llm):
    _turn(aitk, splunkd, admin, agent, llm, "My name is Ada.", _session())

    _turn(aitk, splunkd, admin, agent, llm, "What is my name?", _session())

    assert _conversation(llm, "What is my name?") == [("system", "You are terse."), ("user", "What is my name?")]


def test_another_agent_with_the_same_session_id_does_not_see_it(aitk, splunkd, admin, agent, llm):
    # `| aiagent session_id=` takes any string, so two agents can be given the same one.
    other = create_agent(aitk, splunkd, admin, name="other", system_prompt="You are other.")
    session = _session()
    _turn(aitk, splunkd, admin, agent, llm, "My name is Ada.", session)

    _turn(aitk, splunkd, admin, other, llm, "What is my name?", session)

    assert _conversation(llm, "What is my name?") == [("system", "You are other."), ("user", "What is my name?")]


def test_rows_without_a_session_id_each_start_afresh(aitk, splunkd, admin, agent, llm):
    run_aiagent(aitk, splunkd, admin, agent, _llm_block(llm), "Check {host}", [{"host": "a"}])

    run_aiagent(aitk, splunkd, admin, agent, _llm_block(llm), "Check {host}", [{"host": "b"}])

    assert _conversation(llm, "Check b") == [("system", "You are terse."), ("user", "Check b")]


def test_concurrent_sessions_keep_to_themselves(aitk, splunkd, admin, agent, llm):
    llm.delay = 0.3
    sessions = {name: _session() for name in ("a", "b", "c", "d")}
    errors = []

    def chat(name):
        try:
            _turn(aitk, splunkd, admin, agent, llm, f"I am {name}.", sessions[name])
            _turn(aitk, splunkd, admin, agent, llm, f"Who am I, {name}?", sessions[name])
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=chat, args=(name,)) for name in sessions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert llm.max_in_flight > 1
    for name in sessions:
        assert _conversation(llm, f"Who am I, {name}?")[1:] == [
            ("user", f"I am {name}."),
            ("assistant", f"You said: I am {name}."),
            ("user", f"Who am I, {name}?"),
        ]


def test_concurrent_turns_in_one_session_are_both_remembered(aitk, splunkd, admin, agent, llm):
    # A double submit in Launchpad, or two searches given the same session_id.
    llm.delay = 0.5
    session = _session()
    threads = [threading.Thread(target=_turn, args=(aitk, splunkd, admin, agent, llm, prompt, session))
               for prompt in ("My name is Ada.", "I work on the SOC team.")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    _turn(aitk, splunkd, admin, agent, llm, "Who am I?", session)

    remembered = _conversation(llm, "Who am I?")
    assert ("user", "My name is Ada.") in remembered and ("user", "I work on the SOC team.") in remembered

def test_a_failed_turn_is_not_remembered(aitk, splunkd, admin, agent, llm):
    session = _session()
    _turn(aitk, splunkd, admin, agent, llm, "My name is Ada.", session)
    llm.script = lambda request: (500, {"error": {"message": "model exploded"}})
    df = run_aiagent(aitk, splunkd, admin, agent, _llm_block(llm), "Tell me a story.", [{"x": 1}], session_id=session)
    assert df.iloc[0]["status"] == "error"
    llm.script = lambda request: "Ada."

    _turn(aitk, splunkd, admin, agent, llm, "What is my name?", session)

    assert _conversation(llm, "What is my name?")[1:] == [
        ("user", "My name is Ada."),
        ("assistant", "You said: My name is Ada."),
        ("user", "What is my name?"),
    ]


def test_sessions_unused_past_the_retention_period_are_deleted(aitk, splunkd, admin, agent, llm, runtime_conf,
                                                               splunk_home):
    runtime_conf.write_text(runtime_conf.read_text() + "\n[memory]\nsession_retention_hours = 0.0005\n")
    session = _session()
    _turn(aitk, splunkd, admin, agent, llm, "My name is Ada.", session)
    time.sleep(2.5)  # past 1.8 seconds

    _turn(aitk, splunkd, admin, agent, llm, "What is my name?", session)

    assert _conversation(llm, "What is my name?") == [("system", "You are terse."), ("user", "What is my name?")]
    # Deleted from disk, not just hidden.
    db = sqlite3.connect(splunk_home / "var" / "lib" / "aitk_local" / "memory.db")
    try:
        stored = [blob for (blob,) in db.execute("SELECT checkpoint FROM checkpoints WHERE thread_id LIKE ?",
                                                 (f"%{session}",))]
    finally:
        db.close()
    assert stored and not any(b"My name is Ada." in blob for blob in stored)
