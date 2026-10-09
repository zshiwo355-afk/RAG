import type { GraphEdge, GraphNode } from '../api/knowledgePortal'

export type GraphPoint = { x: number; y: number }
export const GRAPH_LIMIT = 250

export function zoomKnowledge(scale: number, pan: GraphPoint, requestedScale: number, anchor: GraphPoint) {
  const next = Math.max(.25, Math.min(5, requestedScale))
  const ratio = next / scale
  return { scale: next, pan: { x: anchor.x - (anchor.x - pan.x) * ratio, y: anchor.y - (anchor.y - pan.y) * ratio } }
}

export function fitKnowledge(points: GraphPoint[], width = 920, height = 620) {
  if (!points.length) return { scale: 1, x: 0, y: 0 }
  const xs = points.map(point => point.x)
  const ys = points.map(point => point.y)
  const left = Math.min(...xs) - 100
  const right = Math.max(...xs) + 100
  const top = Math.min(...ys) - 45
  const bottom = Math.max(...ys) + 65
  const scale = Math.min(width / (right - left), height / (bottom - top), 3.5)
  return { scale, x: width / 2 - (left + right) / 2 * scale, y: height / 2 - (top + bottom) / 2 * scale }
}

export function chooseKnowledgeLabels(nodes: GraphNode[], edges: GraphEdge[], points: Map<string, GraphPoint>, selected = '', hovered = '', focused = '', displayScale = 1) {
  const scale = Number.isFinite(displayScale) && displayScale > 0 ? displayScale : 1
  const degree = new Map<string, number>()
  const neighbors = new Set<string>()
  for (const edge of edges) {
    degree.set(edge.source, (degree.get(edge.source) || 0) + 1)
    degree.set(edge.target, (degree.get(edge.target) || 0) + 1)
    if (edge.source === selected) neighbors.add(edge.target)
    if (edge.target === selected) neighbors.add(edge.source)
  }
  const priority = (id: string) => id === hovered ? 4 : id === focused ? 3 : id === selected ? 2 : neighbors.has(id) ? 1 : 0
  const sorted = [...nodes].sort((a, b) => priority(b.knowledge_id) - priority(a.knowledge_id) || (degree.get(b.knowledge_id) || 0) - (degree.get(a.knowledge_id) || 0) || a.knowledge_id.localeCompare(b.knowledge_id))
  const labels = new Map<string, number>()
  const boxes: { left: number; right: number; top: number; bottom: number }[] = []
  for (const node of sorted) {
    const point = points.get(node.knowledge_id)
    if (!point) continue
    const chars = Array.from(node.title)
    // Labels keep their screen size as the network zooms. More titles fit when
    // nodes spread apart; collision checks, rather than a fixed count, decide.
    const width = (chars.slice(0, 15).reduce((sum, char) => sum + ((char.codePointAt(0) || 0) > 127 ? 14 : 8), chars.length > 15 ? 14 : 0) + 12) / scale
    const radius = node.knowledge_id === selected ? 9 : 5.5 + Math.min(4, (degree.get(node.knowledge_id) || 0) / 3)
    for (const offset of [radius + 18 / scale, -radius - 10 / scale]) {
      const box = { left: point.x - width / 2, right: point.x + width / 2, top: point.y + offset - 17 / scale, bottom: point.y + offset + 10 / scale }
      if (boxes.some(existing => box.left < existing.right && box.right > existing.left && box.top < existing.bottom && box.bottom > existing.top)) continue
      labels.set(node.knowledge_id, offset); boxes.push(box); break
    }
  }
  return labels
}

export function neighborhood(root: string, edges: GraphEdge[], depth: number) {
  const visited = new Set([root])
  let frontier = new Set([root])
  for (let hop = 0; hop < depth; hop++) {
    const next = new Set<string>()
    for (const edge of edges) {
      if (frontier.has(edge.source) && !visited.has(edge.target)) next.add(edge.target)
      if (frontier.has(edge.target) && !visited.has(edge.source)) next.add(edge.source)
    }
    for (const id of next) visited.add(id)
    frontier = next
  }
  return visited
}

// A bounded, deterministic spring layout settles once instead of animating continuously.
export function layoutKnowledge(nodes: GraphNode[], edges: GraphEdge[], previous: Map<string, GraphPoint> = new Map(), rearrangeExisting = false) {
  const positions = new Map<string, GraphPoint>()
  const sorted = [...nodes].sort((a, b) => a.knowledge_id.localeCompare(b.knowledge_id))
  sorted.forEach((node, index) => {
    const old = previous.get(node.knowledge_id)
    const angle = index * Math.PI * (3 - Math.sqrt(5))
    const radius = 38 * Math.sqrt(index)
    positions.set(node.knowledge_id, old ? { ...old } : { x: 460 + Math.cos(angle) * radius, y: 310 + Math.sin(angle) * radius * .68 })
  })
  const fresh = sorted.filter(node => !previous.has(node.knowledge_id)).length
  if (!fresh && sorted.length && !rearrangeExisting) return positions
  const links = edges.filter(edge => positions.has(edge.source) && positions.has(edge.target))
  const indexes = new Map(sorted.map((node, index) => [node.knowledge_id, index]))
  for (let step = 0; step < 90; step++) {
    const forces = sorted.map(() => ({ x: 0, y: 0 }))
    for (let a = 0; a < sorted.length; a++) {
      const pa = positions.get(sorted[a].knowledge_id)!
      forces[a].x += (460 - pa.x) * .006
      forces[a].y += (310 - pa.y) * .006
      for (let b = a + 1; b < sorted.length; b++) {
        const pb = positions.get(sorted[b].knowledge_id)!
        const dx = pa.x - pb.x || .01
        const dy = pa.y - pb.y || .01
        const distance = Math.max(12, Math.hypot(dx, dy))
        const repulsion = 1800 / (distance * distance)
        const fx = dx / distance * repulsion
        const fy = dy / distance * repulsion
        forces[a].x += fx; forces[a].y += fy
        forces[b].x -= fx; forces[b].y -= fy
      }
    }
    for (const edge of links) {
      const a = indexes.get(edge.source)!
      const b = indexes.get(edge.target)!
      const pa = positions.get(edge.source)!
      const pb = positions.get(edge.target)!
      const dx = pb.x - pa.x
      const dy = pb.y - pa.y
      const distance = Math.max(1, Math.hypot(dx, dy))
      const spring = (distance - 110) * .014
      forces[a].x += dx / distance * spring; forces[a].y += dy / distance * spring
      forces[b].x -= dx / distance * spring; forces[b].y -= dy / distance * spring
    }
    sorted.forEach((node, index) => {
      if (previous.has(node.knowledge_id) && !rearrangeExisting) return
      const point = positions.get(node.knowledge_id)!
      const cooling = (1 - step / 100) * 2.2
      point.x = Math.max(42, Math.min(878, point.x + Math.max(-9, Math.min(9, forces[index].x)) * cooling))
      point.y = Math.max(40, Math.min(580, point.y + Math.max(-9, Math.min(9, forces[index].y)) * cooling))
    })
  }
  return positions
}
