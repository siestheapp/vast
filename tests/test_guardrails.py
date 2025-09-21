import re
from app import create_test_client  # adapt to your app

HEDGE = re.compile(r"\b(typically|usually|likely|generally|commonly|often|might|may)\b", re.I)

def ask(client, text):
    return client.post("/conversations/process", json={"message": text}).json()["response"]

def test_biggest_tables_grounded():
    c = create_test_client()
    r = ask(c, "what are the biggest tables")
    assert "| schema.table | size | rows |" in r and not HEDGE.search(r)

def test_db_identity_grounded():
    c = create_test_client()
    r = ask(c, "what database are you connected to?")
    assert "I am connected to" in r and "PostgreSQL" in r and not HEDGE.search(r)

def test_improvement_is_not_generic():
    c = create_test_client()
    r = ask(c, "how could the database be improved in your opinion")
    # must include at least one grounded section from health report
    assert "Largest tables" in r or "sequential scans" in r or "Unused / rarely used indexes" in r
    assert not HEDGE.search(r)
