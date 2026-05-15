#!/usr/bin/env bash
set -e

cd "$(dirname "$0")/.."

python3 -m uvicorn rag_api:app --host "${RAG_API_HOST:-0.0.0.0}" --port "${RAG_API_PORT:-8000}"

