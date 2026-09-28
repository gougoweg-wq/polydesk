import pytest
from polydesk.config import settings


@pytest.fixture(autouse=True)
def maker_on(monkeypatch):
    """Тесты стратегии не должны зависеть от .env (там maker может быть выключен)."""
    try:
        monkeypatch.setattr(settings, "maker_enabled", True)
    except (AttributeError, TypeError):   # frozen dataclass
        object.__setattr__(settings, "maker_enabled", True)
        yield
        return
    yield
