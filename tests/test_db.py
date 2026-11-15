import pytest

from cloud_agent.config import Settings
from cloud_agent.db import init_db, make_engine, require_schema


def test_schema_must_be_explicitly_initialized(tmp_path):
    engine = make_engine(Settings(database_url=f"sqlite:///{tmp_path / 'new.db'}"))
    with pytest.raises(RuntimeError, match="cloud-agent-db"):
        require_schema(engine)
    init_db(engine)
    require_schema(engine)
    init_db(engine)
    engine.dispose()
