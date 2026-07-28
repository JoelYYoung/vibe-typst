import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import puppeteer from 'puppeteer'

const baseUrl = process.env.VIBE_TYPST_URL || 'http://127.0.0.1:9003'
const browser = await puppeteer.launch({ headless: true })

after(async () => {
  await browser.close()
})

async function directPage(project) {
  const page = await browser.newPage()
  await page.setRequestInterception(true)
  page.on('request', (request) => {
    const url = new URL(request.url())
    const respond = (body, status = 200) => request.respond({
      status,
      contentType: 'application/json',
      body: JSON.stringify(body),
    })
    if (url.pathname === '/api/app/state') {
      return respond({ configured: true, mode: 'server' })
    }
    if (
      url.pathname === `/api/projects/${project.id}/open`
      && request.method() === 'POST'
    ) {
      return respond({
        ok: true,
        project,
        project_id: project.id,
        context_version: `ctx-${project.id}`,
      })
    }
    if (url.pathname === '/api/state') {
      return respond(project.type === 'pdf' ? {
        project_type: 'pdf',
        project_name: project.name,
        project: `/workspace/${project.id}`,
        pages: [],
        tokens: {},
        version: 1,
        generation: 'pdf-1',
      } : {
        project_type: 'typst',
        project_name: project.name,
        project: `/workspace/${project.id}`,
        source: '= Deck',
        pages: [],
        tokens: {},
        room: 'room-1',
      })
    }
    if (url.pathname === '/api/render-version') {
      return respond({
        project_type: project.type,
        pages: [],
        tokens: {},
        version: 1,
        generation: project.type === 'pdf' ? 'pdf-1' : undefined,
        room: project.type === 'typst' ? 'room-1' : undefined,
        error: null,
      })
    }
    if (url.pathname === '/api/slide-map') {
      return respond({
        project_type: project.type,
        pages: [],
        total: 0,
        orphans: project.type === 'pdf' ? {} : [],
        generation: project.type === 'pdf' ? 'pdf-1' : undefined,
      })
    }
    if (url.pathname === '/api/comments') return respond({ comments: [] })
    if (url.pathname === '/api/git/status') {
      return respond({ ok: true, is_repo: false, dirty: false })
    }
    if (url.pathname === '/whoami') {
      return respond({ username: 'alice', role: 'user' })
    }
    return request.continue()
  })
  await page.goto(
    `${baseUrl}/?openProject=${encodeURIComponent(project.id)}`,
    { waitUntil: 'domcontentloaded' },
  )
  return page
}

test('authenticated Typst and PDF direct links open their canonical workspace and consume the query', async () => {
  const typst = await directPage({
    id: 'typst-1',
    name: 'Remote deck',
    type: 'typst',
    main_file: 'main.typ',
  })
  await typst.waitForSelector('.app')
  assert.equal(new URL(typst.url()).searchParams.has('openProject'), false)
  await typst.close()

  const pdf = await directPage({
    id: 'pdf-1',
    name: 'Remote paper',
    type: 'pdf',
    main_file: 'document.pdf',
  })
  await pdf.waitForSelector('.pdf-workspace')
  assert.equal(new URL(pdf.url()).searchParams.has('openProject'), false)
  await pdf.close()
})

test('the legacy project query still selects Projection rather than direct-open', async () => {
  const page = await browser.newPage()
  await page.setRequestInterception(true)
  let openCalls = 0
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (/\/api\/projects\/[^/]+\/open$/.test(url.pathname)) openCalls += 1
    if (url.pathname === '/api/state') {
      return request.respond({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ pages: [], tokens: {} }),
      })
    }
    return request.continue()
  })
  await page.goto(`${baseUrl}/?project`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.projection')
  assert.equal(openCalls, 0)
})
