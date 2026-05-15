from __future__ import annotations

import pytest

import build_new_product_documents_from_review as build_new
import reclassify_product_mapping_review as reclassify


def review_row(**overrides):
    row = {
        "品牌": "大民族",
        "来源名称": "大民族品牌产品信息表.xlsx",
        "来源文件路径": "大民族品牌产品信息表.xlsx",
        "来源文件ID": "product_excel__sha",
        "本地产品ID": "local-a",
        "本地产品名称": "大民族酒·师藏年鉴（张怀仁版）",
        "本地规格": "500ml*6",
        "本地生产酒厂": "仁怀酒厂",
        "候选生成说明": "线上 text_docs 未按核心名称命中；旧候选仅供参考。",
        "候选1匹配依据": "同品牌:大民族; 无核心名称命中；旧候选仅供参考，不建议直接确认",
    }
    row.update(overrides)
    return row


def source_doc(local_id="local-a", doc_type="product_full"):
    return {
        "page_content": "产品名称：大民族酒·师藏年鉴（张怀仁版）\n规格：500ml*6",
        "metadata": {
            "doc_id": f"{local_id}__{doc_type}",
            "product_id": local_id,
            "product_name": "大民族酒·师藏年鉴（张怀仁版）",
            "spec": "500ml*6",
            "manufacturer": "仁怀酒厂",
            "brand": "大民族",
            "doc_type": doc_type,
            "source_doc_id": "product_excel__sha",
            "source_sha1": "sha",
            "source_path": "大民族品牌产品信息表.xlsx",
        },
    }


@pytest.mark.offline
def test_no_core_name_hit_is_new_product_candidate() -> None:
    result = reclassify.reclassify_row(review_row(), "prd_000286")

    assert result["建议动作"] == "new_product_candidate"
    assert result["是否建议作为新产品"] == "是"
    assert result["我的选择"] == "作为新产品"
    assert result["suggested_new_product_id"] == "prd_000286"


@pytest.mark.offline
def test_reliable_core_candidate_is_not_marked_new_product() -> None:
    row = review_row(
        候选生成说明="线上 text_docs 核心名称命中。",
        候选1匹配依据="核心名称命中; 产品名高相似:0.96",
    )

    result = reclassify.reclassify_row(row, "prd_000286")

    assert result["建议动作"] == "map_to_existing_or_review"
    assert result["是否建议作为新产品"] == "否"


@pytest.mark.offline
def test_new_product_documents_are_not_supplements() -> None:
    rows = {
        "local-a": {
            **review_row(),
            "suggested_new_product_id": "prd_000286",
            "我的选择": "作为新产品",
        }
    }

    docs, stats = build_new.build_new_product_documents([source_doc("local-a"), source_doc("local-a", "product_field")], rows)

    assert stats["new_product_document_count"] == 2
    assert {doc["doc_type"] for doc in docs} == {"product_full", "product_field"}
    assert all(doc["doc_type"] != "supplement" for doc in docs)
    assert all(doc["product_id"] == "prd_000286" for doc in docs)
    assert all(doc["canonical_product_id"] == "prd_000286" for doc in docs)
    assert all(doc["source_doc_id"] == "product_excel__sha" for doc in docs)
    assert all(doc["source_sha1"] == "sha" for doc in docs)
    assert all(doc["source_type"] == "product_excel_new_product" for doc in docs)
