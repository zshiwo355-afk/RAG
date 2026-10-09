import os
import socket

import pytest

from rag_app.config import load_env
from rag_app.knowledge_store import KnowledgeStore


def test_dotenv_cannot_turn_local_catalogue_into_cloud_storage(tmp_path):
    (tmp_path / ".env").write_text("KNOWLEDGE_BODY_STORAGE=oss\nOSS_ACCESS_KEY_ID=sentinel-only\n")
    load_env(tmp_path)
    assert "OSS_ACCESS_KEY_ID" not in os.environ
    assert KnowledgeStore(tmp_path / "catalogue.db").body_storage == "inline"


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_unit_test_transport_is_blocked_before_connecting(method):
    with socket.socket() as connection, pytest.raises(RuntimeError, match="integration opt-in"):
        getattr(connection, method)(("127.0.0.1", 9))
