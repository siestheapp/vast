from types import MethodType, SimpleNamespace

from src.vast.conversation import ConversationContext, MessageRole, VastConversation


def test_database_size_query_executes(monkeypatch, tmp_path):
    recorded = {}

    def fake_safe_execute(sql, *args, **kwargs):
        recorded["sql"] = sql
        return [SimpleNamespace(_mapping={"size_bytes": 2048, "size_pretty": "2 kB"})]

    monkeypatch.setattr("src.vast.conversation.safe_execute", fake_safe_execute)

    conv = VastConversation.__new__(VastConversation)
    conv.session_name = "test_size"
    conv.session_file = tmp_path / "session.json"
    conv.messages = []
    conv.context = ConversationContext(database_url="postgresql://test", schema_summary="summary")
    conv.last_actions = []
    conv.client = None
    conv.engine = None
    conv.system_ops = None
    conv._save_session = lambda: None

    def fail_llm(self, *args, **kwargs):
        raise AssertionError("LLM should not be called for database size requests")

    conv._get_llm_response = MethodType(fail_llm, conv)

    response = conv.process("How big is the database right now?")

    assert "2 kB" in response
    assert "2048" in response
    assert "pg_database_size" in recorded["sql"]
    assert conv.messages[0].role is MessageRole.USER
    assert conv.messages[1].role is MessageRole.EXECUTION
    assert conv.messages[2].role is MessageRole.ASSISTANT

    metadata = conv.last_actions[0]
    assert metadata["success"] is True
    assert metadata["rows"] == [{"size_bytes": 2048, "size_pretty": "2 kB"}]
