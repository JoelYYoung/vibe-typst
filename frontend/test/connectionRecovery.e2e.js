import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import puppeteer from 'puppeteer'

const baseUrl = process.env.VIBE_TYPST_URL || 'http://127.0.0.1:9003'
const browser = await puppeteer.launch({ headless: true })
after(() => browser.close())

async function mockPage({ editor = false, slowRender = false } = {}) {
  const state = { failing: true, activeRenderRequests: 0, maxRenderRequests: 0 }
  const page = await browser.newPage()
  await page.setRequestInterception(true)
  page.on('request', async (request) => {
    const path = new URL(request.url()).pathname
    const respond = (body, status = 200) => request.respond({
      status, contentType: 'application/json', body: JSON.stringify(body),
    }).catch(() => {})
    if (path === '/api/app/state') return respond({ configured: true, mode: 'server' })
    if (path === '/whoami') return respond({ username: 'test', role: 'user' })
    if (path === '/api/projects') return respond({
      projects: [{ id: 'test-deck', name: 'Recovered deck', type: 'typst', main_file: 'main.typ' }],
    }, !editor && state.failing ? 503 : 200)
    if (path === '/api/projects/test-deck/open') return respond({
      project: { id: 'test-deck', name: 'Recovered deck', type: 'typst', main_file: 'main.typ' },
    })
    if (path === '/api/state') return respond({
      project: '/workspace/test-deck', project_name: 'Recovered deck',
      file: '/workspace/test-deck/main.typ', main: 'main.typ', mode: 'server',
      room: null, pages: [], tokens: {}, workdir_ready: true,
    })
    if (path === '/api/comments') return respond([], !slowRender && state.failing ? 503 : 200)
    if (path === '/api/render-version') {
      state.activeRenderRequests += 1
      state.maxRenderRequests = Math.max(state.maxRenderRequests, state.activeRenderRequests)
      if (slowRender) await new Promise((resolve) => setTimeout(resolve, 1500))
      await respond({ pages: [], tokens: {}, version: 1, error: null })
      state.activeRenderRequests -= 1
      return
    }
    if (path === '/api/slide-map') return respond({ pages: [], orphans: [] })
    if (path.startsWith('/api/')) return respond({})
    request.continue().catch(() => {})
  })
  await page.goto(`${baseUrl}/${editor ? '?openProject=test-deck' : ''}`, { waitUntil: 'domcontentloaded' })
  return { page, state }
}

test('a failed project list stays disconnected through successful heartbeats and retries without refresh', async () => {
  const { page, state } = await mockPage()
  try {
    await page.waitForSelector('.connection-banner.lost')
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
    await page.waitForFunction(() => document.querySelector('.connection-banner.lost button')?.disabled === false)
    assert.equal(await page.$('.connection-banner.restored'), null)
    state.failing = false
    await page.click('.connection-banner.lost button')
    await page.waitForFunction(() => document.body.textContent.includes('Recovered deck'))
    await page.waitForSelector('.connection-banner.restored')
  } finally { await page.close() }
})

test('recovering comments preserves the open editor', async () => {
  const { page, state } = await mockPage({ editor: true })
  try {
    await page.waitForSelector('.app')
    await page.waitForSelector('.connection-banner.lost')
    state.failing = false
    await page.click('.connection-banner.lost button')
    await page.waitForSelector('.connection-banner.restored')
    assert.ok(await page.$('.app'))
    assert.equal(await page.$('.projects-bg'), null)
  } finally { await page.close() }
})

test('slow render polling has at most one outstanding request per tab', async () => {
  const { page, state } = await mockPage({ editor: true, slowRender: true })
  try {
    await page.waitForSelector('.app')
    await new Promise((resolve) => setTimeout(resolve, 2300))
    assert.equal(state.maxRenderRequests, 1)
  } finally { await page.close() }
})
