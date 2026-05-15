#!/usr/bin/env python3
"""OpenSearch vector edition client helpers used by read and push code."""

from __future__ import annotations

import os
from typing import Any

from .config import load_env


def ensure_runtime_config() -> dict[str, str]:
    load_env()
    config = {
        "endpoint": os.getenv("OPENSEARCH_ENDPOINT", "").strip(),
        "instance_id": os.getenv("OPENSEARCH_INSTANCE_ID", "").strip(),
        "username": os.getenv("OPENSEARCH_USERNAME", "").strip(),
        "password": os.getenv("OPENSEARCH_PASSWORD", "").strip(),
    }
    missing = [
        env_name
        for env_name, value in [
            ("OPENSEARCH_ENDPOINT", config["endpoint"]),
            ("OPENSEARCH_INSTANCE_ID", config["instance_id"]),
            ("OPENSEARCH_USERNAME", config["username"]),
            ("OPENSEARCH_PASSWORD", config["password"]),
        ]
        if not value
    ]
    if missing:
        raise RuntimeError(
            "缺少 OpenSearch 连接配置："
            + ", ".join(missing)
            + "。请在 .env 中补齐这些环境变量。"
        )
    config["endpoint"] = config["endpoint"].rstrip("/")
    return config


def import_sdk_modules() -> tuple[Any, Any, Any]:
    try:
        from alibabacloud_ha3engine_vector.client import Client
        from alibabacloud_ha3engine_vector.models import Config
        from alibabacloud_ha3engine_vector import models
    except ImportError as exc:
        raise RuntimeError(
            "未安装阿里云 OpenSearch 向量检索版 Python SDK。"
            "请先安装：pip install alibabacloud_ha3engine_vector==1.1.17"
        ) from exc
    return Client, Config, models


def resolve_protocol(endpoint_value: str) -> str:
    return "HTTPS" if endpoint_value.lower().startswith("https://") else "HTTP"


def create_client(config: dict[str, str] | None = None):
    runtime_config = config or ensure_runtime_config()
    Client, Config, models = import_sdk_modules()
    endpoint_value = runtime_config["endpoint"]
    protocol = resolve_protocol(endpoint_value)
    client = Client(
        Config(
            endpoint=endpoint_value,
            instance_id=runtime_config["instance_id"],
            protocol=protocol,
            access_user_name=runtime_config["username"],
            access_pass_word=runtime_config["password"],
        )
    )
    return client, models

