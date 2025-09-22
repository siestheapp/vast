from types import MethodType

from src.vast.conversation import ConversationContext, MessageRole, VastConversation


def test_ops_plan_includes_standard_headings(monkeypatch, tmp_path):
    conv = VastConversation.__new__(VastConversation)
    conv.session_name = "ops_test"
    conv.session_file = tmp_path / "session.json"
    conv.messages = []
    conv.context = ConversationContext(database_url="postgresql://test", schema_summary="summary")
    conv.last_actions = []
    conv.client = None
    conv.engine = None
    conv.system_ops = None
    conv._save_session = lambda: None

    def fake_llm(self, _):
        return "We should introduce a new attribute and backfill data accordingly."

    conv._get_llm_response = MethodType(fake_llm, conv)

    response = conv.process("Please backfill a new attribute and outline the plan")

    for heading in ("Plan:", "Staging:", "Validation:", "Rollback:"):
        assert heading in response

    assert conv.messages[-1].role is MessageRole.ASSISTANT
