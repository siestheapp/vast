from src.vast import config


def test_write_url_prefers_override(monkeypatch):
    monkeypatch.setattr(config.settings, "write_url_override", "postgresql://override")
    monkeypatch.setattr(config.settings, "database_url_rw", "postgresql://rw")
    assert config.write_url() == "postgresql://override"


def test_write_url_falls_back_to_rw(monkeypatch):
    monkeypatch.setattr(config.settings, "write_url_override", None)
    monkeypatch.setattr(config.settings, "database_url_rw", "postgresql://rw")
    assert config.write_url() == "postgresql://rw"


def test_write_url_missing(monkeypatch):
    monkeypatch.setattr(config.settings, "write_url_override", None)
    monkeypatch.setattr(config.settings, "database_url_rw", None)
    monkeypatch.setattr(config.settings, "database_url_ro", "postgresql://ro")

    assert config.write_url() == "postgresql://ro"
