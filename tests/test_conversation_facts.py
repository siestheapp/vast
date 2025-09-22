import types

from src.vast.facts import FactAnswer, ExecutionLog
from src.vast.conversation import ConversationContext, MessageRole, VastConversation


def test_conversation_short_circuits_for_db_size(monkeypatch):
    convo = VastConversation.__new__(VastConversation)
    convo.messages = []
    convo.last_actions = []
    convo.context = ConversationContext(
        database_url="postgresql://localhost/pagila",
        schema_summary="",
        last_fingerprint="fp"
    )
    convo._save_session = lambda: None
    convo._extract_context_updates = lambda response: None

    llm_called = False

    def fake_llm_response(_self, _user_input: str) -> str:
        nonlocal llm_called
        llm_called = True
        return "LLM"

    convo._get_llm_response = types.MethodType(fake_llm_response, convo)

    def fake_try_answer(_runtime, user_text: str):
        if user_text == "how large is it":
            return FactAnswer(
                payload={
                    "database_size_pretty": "118 MB",
                    "database_size_bytes": 123_456_789,
                    "source": "facts+live-sql",
                },
                content="Database size: **118 MB** (123456789 bytes).\n_Source: facts (live SQL)._",
                log_entries=[
                    ExecutionLog(
                        content="Facts: database size lookup",
                        metadata={
                            "success": True,
                            "type": "FACT",
                            "fact_key": "db_size",
                            "sql": "SELECT pg_database_size(...)",
                            "rows": [{"size_bytes": 123_456_789, "size_pretty": "118 MB"}],
                            "source": "facts+live-sql",
                        },
                    )
                ],
            )
        return None

    monkeypatch.setattr("src.vast.conversation.try_answer_with_facts", fake_try_answer)

    response = VastConversation.process(convo, "how large is it")

    assert "118 MB" in response
    assert "bytes" in response
    assert not llm_called

    exec_entries = [m for m in convo.messages if m.role == MessageRole.EXECUTION]
    assert exec_entries, "expected execution provenance entry"
    exec_meta = exec_entries[0].metadata
    assert exec_meta["type"] == "FACT"
    assert exec_meta["fact_key"] == "db_size"
    assert "sql" in exec_meta and exec_meta["sql"]

    assert convo.last_actions[0]["type"] == "FACT"
*** End Patch
