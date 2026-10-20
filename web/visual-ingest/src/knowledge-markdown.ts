import MarkdownIt from 'markdown-it'

// Only parser-generated HTML reaches the reader. Source HTML is always escaped.
const markdown = new MarkdownIt({ html: false, breaks: true, linkify: false, typographer: false })
// SOP labels often omit the space after **标签：**; preserve the original text.
markdown.inline.ruler.before('emphasis', 'compact_label', (state, silent) => {
  const match = /^\*\*(?!\*)([^\s*`\\\n][^*`\\\n]*?[：:])\*\*(?=[\p{L}\p{N}])/u.exec(state.src.slice(state.pos, state.posMax))
  if (!match) return false
  if (!silent) {
    state.push('strong_open', 'strong', 1)
    state.md.inline.parse(match[1], state.md, state.env, state.tokens)
    state.push('strong_close', 'strong', -1)
  }
  state.pos += match[0].length
  return true
})
markdown.validateLink = value => /^(?:https?:\/\/|mailto:|#)/i.test(value)
markdown.renderer.rules.link_open = (tokens, index, options, _env, renderer) => {
  tokens[index].attrSet('target', '_blank')
  tokens[index].attrSet('rel', 'noopener noreferrer')
  tokens[index].attrSet('referrerpolicy', 'no-referrer')
  return renderer.renderToken(tokens, index, options)
}
// Knowledge content must not load remote tracking pixels or authenticated URLs.
markdown.renderer.rules.image = (tokens, index, options, env, renderer) => {
  const alt = renderer.renderInlineAsText(tokens[index].children || [], options, env)
  return `<span class="markdown-image-note">[图片${alt ? `：${markdown.utils.escapeHtml(alt)}` : ''}]</span>`
}
markdown.renderer.rules.table_open = () => '<div class="markdown-table-wrap" role="region" aria-label="表格，可横向滚动" tabindex="0"><table>\n'
markdown.renderer.rules.table_close = () => '</table></div>\n'
// The portal disallows inline styles; keep Markdown table alignment in CSS classes.
for (const type of ['th_open', 'td_open']) {
  markdown.renderer.rules[type] = (tokens, index, options, _env, renderer) => {
    const token = tokens[index]
    const alignment = String(token.attrGet('style') || '').match(/^text-align:(left|center|right)$/)?.[1]
    token.attrs = (token.attrs || []).filter(([name]) => name !== 'style')
    if (alignment) token.attrJoin('class', `markdown-align-${alignment}`)
    return renderer.renderToken(tokens, index, options)
  }
}

export function renderKnowledgeMarkdown(content: string): string {
  return markdown.render(content)
}
