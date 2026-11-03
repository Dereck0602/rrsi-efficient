import { defineConfig } from 'vitepress'
import { repositoryLinks } from './repository-links'

const base = process.env.DOCS_BASE || '/FlashRSI/'
const item = (text: string, link: string) => ({ text, link })

export default defineConfig({
  title: 'FlashRSI',
  description: 'Run, compare, and develop agent self-improvement methods with native harnesses and versioned resources.',
  lang: 'en-US',
  base,
  // Keep .html URLs: GitHub Pages can serve every page directly without rewrites.
  cleanUrls: false,
  rewrites: { 'README.md': 'overview.md' },
  // Match the local reports excluded from source control.
  srcExclude: [
    '**/node_modules/**', 'smoke*', 'validation-*', 'core-source-audit*',
    'main-sync-*', 'git-integration-*',
  ],
  head: [
    ['link', { rel: 'icon', type: 'image/svg+xml', href: `${base}favicon.svg` }],
    ['meta', { name: 'theme-color', content: '#f7f5f2' }],
  ],
  markdown: {
    theme: { light: 'github-dark', dark: 'github-dark' },
    config: repositoryLinks,
  },
  themeConfig: {
    logo: '/favicon.svg',
    siteTitle: 'FlashRSI',
    nav: [
      { text: 'Project page', link: '/project.html', activeMatch: '^/project\\.html$' },
      { text: 'Guide', link: '/guide/installation', activeMatch: '^/(guide/|$|index)' },
      { text: 'Integrations', link: '/integrations/algorithms', activeMatch: '^/integrations/' },
      { text: 'Build', link: '/development/framework', activeMatch: '^/development/' },
      { text: '简体中文', link: '/usage.zh-CN' },
    ],
    sidebar: [
      { text: 'Introduction', items: [
        item('Quickstart', '/'),
        item('Framework & Snapshot', '/development/framework'),
        item('How a run works', '/running-experiments'),
      ] },
      { text: 'Run experiments', items: [
        item('Installation', '/guide/installation'),
        item('Configuration & CLI', '/guide/configuration'),
        item('Evaluate a Snapshot', '/guide/evaluation'),
        item('Snapshot storage', '/guide/snapshot-storage'),
      ] },
      { text: 'Integrations', items: [
        item('Algorithms', '/integrations/algorithms'),
        item('OPRO', '/opro'),
        item('GEPA', '/gepa'),
        item('EvoPrompt', '/evoprompt'),
        item('ACE', '/ace'),
        item('SkillOpt', '/skillopt'),
        item('Evo-Harness', '/evo-harness'),
        item('AHE', '/ahe'),
        item('ADAS', '/adas'),
        item('Meta-Harness', '/meta-harness'),
        item('DarwinX', '/darwinx'),
        item('ModularRSI', '/modular-rsi'),
        item('Benchmarks', '/integrations/benchmarks'),
        item('Datasets & splits', '/integrations/datasets'),
        item('Harnesses', '/integrations/harnesses'),
      ] },
      { text: 'Build on FlashRSI', items: [
        item('Add a benchmark', '/development/benchmark'),
        item('Add a harness', '/development/harness'),
        item('Add an algorithm', '/development/algorithm'),
      ] },
      { text: 'Reference', items: [
        item('FAQ', '/faq'),
        item('All integration notes', '/reference'),
        item('Python API & usage', '/usage'),
        item('Build & host these docs', '/development/docs-site'),
      ] },
    ],
    outline: { label: 'On this page', level: [2, 3] },
    search: {
      provider: 'local',
      options: {
        // Preserve historical notes as pages without ranking them above current guides.
        _render(src, env, md) {
          const historical = new Set([
            'README.md', 'implementation-notes.zh-CN.md', 'migration-plan.md',
            'mvp-doc-drift.md',
          ])
          return historical.has(env.relativePath) ? '' : md.render(src, env)
        },
        detailedView: true,
        translations: { button: { buttonText: 'Search documentation', buttonAriaLabel: 'Search documentation' } },
      },
    },
    socialLinks: [{ icon: 'github', link: 'https://github.com/RUC-NLPIR/FlashRSI' }],
    editLink: {
      pattern: 'https://github.com/RUC-NLPIR/FlashRSI/edit/main/docs/:path',
      text: 'Edit this page on GitHub',
    },
    docFooter: { prev: 'Previous page', next: 'Next page' },
    footer: {
      message: 'Released under the Apache License 2.0.',
      copyright: 'Gaoling School of Artificial Intelligence · Renmin University of China',
    },
  },
})
