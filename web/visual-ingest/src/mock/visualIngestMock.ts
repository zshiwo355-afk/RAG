export const mockParseResult = {
  batch_id: 'mock_batch',
  document_count: 2,
  documents: [
    {
      document_id: 'doc_0001',
      title: '示例产品资料',
      source_id: 'mock_source_001',
      text_length: 168,
      metadata: { category: '产品资料', tag: 'mock' }
    },
    {
      document_id: 'doc_0002',
      title: '示例补充资料',
      source_id: 'mock_source_002',
      text_length: 96,
      metadata: { category: '补充资料', tag: 'mock' }
    }
  ],
  warnings: [],
  errors: []
}

export const mockChunkResult = {
  batch_id: 'mock_batch',
  chunk_count: 3,
  stats: { empty_text: 0, too_short: 0, too_long: 0, duplicate_hashes: 0 },
  chunks: [
    {
      chunk_id: 'doc_0001__chunk_0001',
      source_id: 'mock_source_001',
      char_count: 88,
      hash: 'hash-a',
      text: '这是示例 chunk，展示产品基础介绍。',
      metadata: { doc_type: 'product_field' },
      flags: { empty: false, too_short: false, too_long: false, duplicate: false }
    },
    {
      chunk_id: 'doc_0001__chunk_0002',
      source_id: 'mock_source_001',
      char_count: 80,
      hash: 'hash-b',
      text: '这是示例 chunk，展示包装和卖点。',
      metadata: { doc_type: 'product_field' },
      flags: { empty: false, too_short: false, too_long: false, duplicate: false }
    },
    {
      chunk_id: 'doc_0002__chunk_0001',
      source_id: 'mock_source_002',
      char_count: 96,
      hash: 'hash-c',
      text: '这是示例补充资料 chunk。',
      metadata: { doc_type: 'supplement' },
      flags: { empty: false, too_short: false, too_long: false, duplicate: false }
    }
  ]
}

export const mockPreflightResult = {
  batch_id: 'mock_batch',
  status: 'pass',
  checks: [
    { name: 'batch_id', status: 'pass', message: 'batch_id 已生成' },
    { name: 'target', status: 'pass', message: '目标知识库已设置' },
    { name: 'run_mode', status: 'pass', message: '当前为 dry-run' },
    { name: 'chunks', status: 'pass', message: 'chunk 数：3' }
  ],
  will_call_embedding: false,
  will_write_vector_db: false,
  will_push: false,
  will_delete: false,
  will_overwrite: false
}

export const mockRunResult = {
  batch_id: 'mock_batch',
  status: 'success',
  message: 'dry-run 完成，未写入向量库',
  vector_db_written: false,
  embedding_called: false,
  push_executed: false,
  delete_executed: false,
  answer_called: false
}

export const mockSearchResult = {
  ok: true,
  query: '示例查询',
  products: [
    {
      product_id: 'prod_mock_001',
      final_score: 0.92,
      best_text: '示例产品命中摘要',
      doc_hits: [
        { doc_id: 'doc_hit_1', score: 0.9, source_text: '命中 chunk 文本', metadata: { source_id: 'mock_source_001' } }
      ],
      product_bundle: {
        product_id: 'prod_mock_001',
        basic_info: [{ doc_id: 'basic_1', source_text: '基础资料' }],
        packaging: [{ doc_id: 'pkg_1', source_text: '包装资料' }],
        selling_points: [{ doc_id: 'sell_1', source_text: '卖点资料' }],
        gift_attributes: [],
        quality_reports: [{ doc_id: 'qr_1', source_text: '质检报告摘要' }],
        image_texts: [{ doc_id: 'img_1', source_text: '图片语义' }],
        supplements: [{ doc_id: 'supp_1', source_text: '补充资料' }],
        conflicts: [],
        source_doc_ids: ['mock_source_001'],
        doc_ids: ['basic_1', 'qr_1', 'supp_1']
      }
    }
  ],
  product_bundles: [
    {
      product_id: 'prod_mock_001',
      basic_info: [{ doc_id: 'basic_1', source_text: '基础资料' }],
      packaging: [{ doc_id: 'pkg_1', source_text: '包装资料' }],
      selling_points: [{ doc_id: 'sell_1', source_text: '卖点资料' }],
      gift_attributes: [],
      quality_reports: [{ doc_id: 'qr_1', source_text: '质检报告摘要' }],
      image_texts: [{ doc_id: 'img_1', source_text: '图片语义' }],
      supplements: [{ doc_id: 'supp_1', source_text: '补充资料' }],
      conflicts: []
    }
  ]
}
