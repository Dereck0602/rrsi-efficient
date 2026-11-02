import { readFileSync, readdirSync, statSync } from 'node:fs'
import { extname, join, resolve, sep } from 'node:path'

const root = resolve('docs/.vitepress/dist')
const base = process.env.DOCS_BASE || '/FlashRSI/'
const origin = 'https://docs.local'
const files = (dir) => readdirSync(dir).flatMap((name) => {
  const path = join(dir, name)
  return statSync(path).isDirectory() ? files(path) : [path]
})
const cache = new Map()
const anchors = (path) => {
  if (!cache.has(path)) {
    const html = readFileSync(path, 'utf8')
    cache.set(path, new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1])))
  }
  return cache.get(path)
}
const errors = new Set()
let links = 0
const htmlFiles = files(root).filter((path) => path.endsWith('.html'))
for (const path of htmlFiles) {
  const source = path.slice(root.length + 1)
  const url = new URL(`${base}${source}`, origin)
  const html = readFileSync(path, 'utf8')
  for (const [, raw] of html.matchAll(/\b(?:href|src)="([^"]+)"/g)) {
    if (/^(?:data:|mailto:|tel:|javascript:)/i.test(raw)) continue
    const target = new URL(raw.replaceAll('&amp;', '&'), url)
    if (target.origin !== origin) continue
    links++
    if (!target.pathname.startsWith(base)) {
      errors.add(`${source}: outside base path: ${raw}`)
      continue
    }
    let local = decodeURIComponent(target.pathname.slice(base.length))
    if (!local || local.endsWith('/')) local += 'index.html'
    else if (!extname(local)) local += '.html'
    const destination = resolve(root, local)
    if (!destination.startsWith(`${root}${sep}`)) {
      errors.add(`${source}: outside build directory: ${raw}`)
      continue
    }
    try {
      if (!statSync(destination).isFile()) throw new Error('not a file')
      if (target.hash && destination.endsWith('.html') && !anchors(destination).has(decodeURIComponent(target.hash.slice(1)))) {
        errors.add(`${source}: missing anchor: ${raw}`)
      }
    } catch {
      errors.add(`${source}: missing file: ${raw}`)
    }
  }
}
if (errors.size) {
  console.error([...errors].sort().join('\n'))
  process.exitCode = 1
} else {
  console.log(`Checked ${htmlFiles.length} pages and ${links} local links/assets; all destinations and anchors exist.`)
}
