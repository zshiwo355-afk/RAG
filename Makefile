.PHONY: test test-unit test-offline lint

test: test-offline

test-unit:
	python3 -m pytest -m "not integration"

test-offline:
	python3 -m pytest -m "offline or not integration"

lint:
	python3 -c 'from pathlib import Path; [compile(Path(p).read_text(encoding="utf-8"), p, "exec") for p in ["scripts/plan_ingest.py", "scripts/ingest_content.py", "scripts/build_product_excel_docs.py", "scripts/build_product_excel_pipeline.py", "scripts/embed_ingest_build.py", "scripts/push_ingest_embeddings.py", "scripts/verify_retrieval.py", "scripts/plan_ingest_changes.py", "scripts/apply_ingest_change_plan.py", "scripts/check_existing_product_overlap.py", "scripts/plan_product_identity_mapping.py", "scripts/build_supplement_documents_from_mapping.py", "scripts/build_product_mapping_review.py", "scripts/reclassify_product_mapping_review.py", "scripts/build_new_product_documents_from_review.py", "scripts/build_final_49_split_documents.py", "scripts/plan_cleanup_outputs.py", "scripts/preview_product_bundle.py", "scripts/ingest_dashboard.py", "src/rag_app/ingest_manifest.py", "src/rag_app/retrieval_verify.py", "src/rag_app/ingest_dashboard_helpers.py", "src/rag_app/visual_ingest_api.py"]]'
