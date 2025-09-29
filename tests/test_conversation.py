from types import SimpleNamespace


def test_auto_execute_ro_path_returns_scalar(monkeypatch, tmp_path):
    from src.vast.conversation import ConversationContext, VastConversation

    conv = VastConversation.__new__(VastConversation)
    conv.session_name = "test-auto-ro"
    conv.session_file = tmp_path / "session.json"
    conv.messages = []
    conv.context = ConversationContext(
        database_url="postgresql://test",
        schema_summary="schema",
        last_fingerprint="cached-fp",
    )
    conv.last_actions = []
    conv.client = None
    conv.engine = None
    conv.system_ops = None
    conv._save_session = lambda: None

    monkeypatch.setattr("src.vast.conversation.list_tables", lambda: [])

    class _FakeFacts(SimpleNamespace):
        def __init__(self, **kwargs):
            super().__init__(schema_fingerprint=kwargs.get("schema_fingerprint", "live-fp"))

    monkeypatch.setattr("src.vast.conversation.FactsRuntime", _FakeFacts)
    monkeypatch.setattr("src.vast.conversation.try_answer_with_facts", lambda *args, **kwargs: None)

    class _Store:
        def capture_schema_snapshot(self):
            return None

        def search(self, *_, **__):
            return []

    monkeypatch.setattr("src.vast.conversation.get_knowledge_store", lambda: _Store())

    def fake_plan_and_execute(**kwargs):
        assert kwargs.get("allow_writes") is False
        return {
            "execution": {
                "rows": [{"table_count": 5}],
                "row_count": 1,
            }
        }

    monkeypatch.setattr("src.vast.conversation.service.plan_and_execute", fake_plan_and_execute)
    monkeypatch.setattr(
        VastConversation,
        "_get_llm_response",
        lambda self, _: "Placeholder response that will be replaced.",
    )

    result = conv.process("how many tables", auto_execute=True)

    assert result
    upper = result.upper()
    for keyword in ("DROP", "UPDATE", "INSERT", "DELETE"):
        assert keyword not in upper
    # Ensure scaffold headings are not present for read intent responses
    for heading in ("PLAN:", "STAGING:", "VALIDATION:", "ROLLBACK:"):
        assert heading not in upper
