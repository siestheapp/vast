import re
import time
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
        return (
            "Plan - Roll out the new attribute across stores\n"
            "Staging - Populate staging tables first\n"
            "Validation - Compare counts\n"
            "Rollback - Drop staging tables if needed"
        )

    conv._get_llm_response = MethodType(fake_llm, conv)

    response = conv.process("Please backfill a new attribute and outline the plan")

    # Text includes headings since this is an explicit plan request.
    for heading in ("Plan:", "Staging:", "Validation:", "Rollback:"):
        assert heading in response
    assert re.search(r"(?i)(schema|ddl)", response)

    assert conv.messages[-1].role is MessageRole.ASSISTANT
def test_ops_plan_timeout_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("VAST_PLANNER_TIMEOUT_SEC", "1")

    conv = VastConversation.__new__(VastConversation)
    conv.session_name = "ops_timeout"
    conv.session_file = tmp_path / "session.json"
    conv.messages = []
    conv.context = ConversationContext(database_url="postgresql://test", schema_summary="summary")
    conv.last_actions = []
    conv.client = None
    conv.engine = None
    conv.system_ops = None
    conv._save_session = lambda: None

    def slow_llm(self, _):
        time.sleep(2)
        return "This should not be returned due to timeout"

    conv._get_llm_response = MethodType(slow_llm, conv)

    response = conv.process("Please provide a backfill plan for the new attribute")

    for heading in ("Plan:", "Staging:", "Validation:", "Rollback:"):
        assert heading in response
    assert re.search(r"(?i)(schema|ddl)", response)

    audit_entry = conv.last_actions[-1]
    assert audit_entry["type"] == "OPS_PLAN"
    assert audit_entry["source"] == "fallback-timeout"
