import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import puppeteer from 'puppeteer'

const baseUrl = process.env.VIBE_TYPST_URL || 'http://127.0.0.1:9003'
const browser = await puppeteer.launch({ headless: true })

after(async () => {
  await browser.close()
})

async function installEmptyAccountMocks(page) {
  await page.setRequestInterception(true)
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname
    const responses = {
      '/whoami': { username: 'alice', role: 'user' },
      '/api/app/state': { configured: true, mode: 'server' },
      '/api/projects': { projects: [] },
      '/account/tokens': { tokens: [] },
    }
    if (responses[path]) {
      return request.respond({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(responses[path]),
      })
    }
    return request.continue()
  })
}

test('account tokens are created once, hidden after dismissal, and explicitly revoked', async () => {
  const page = await browser.newPage()
  await page.setViewport({ width: 1100, height: 820, deviceScaleFactor: 1 })
  await page.setRequestInterception(true)

  let token = null
  let deleteCount = 0
  let createPayload = null
  page.on('request', async (request) => {
    const url = new URL(request.url())
    const respond = (body, status = 200) => request.respond({
      status,
      contentType: 'application/json',
      body: JSON.stringify(body),
    })
    if (url.pathname === '/whoami') {
      return respond({ username: 'alice', role: 'user' })
    }
    if (url.pathname === '/api/app/state') {
      return respond({ configured: true, mode: 'server' })
    }
    if (url.pathname === '/api/projects' && request.method() === 'GET') {
      return respond({ projects: [] })
    }
    if (url.pathname === '/account/tokens' && request.method() === 'GET') {
      return respond({ tokens: token ? [token] : [] })
    }
    if (url.pathname === '/account/tokens' && request.method() === 'POST') {
      createPayload = JSON.parse(request.postData() || '{}')
      token = {
        id: 'token-1',
        name: createPayload.name,
        token_prefix: 'vbt_tok1_se',
        preset: createPayload.preset,
        created_at: 1785196800,
        expires_at: createPayload.expires_at,
        last_used_at: null,
        revoked_at: null,
      }
      return respond({ token, secret: 'vbt_tok1_secret' })
    }
    if (
      url.pathname === '/account/tokens/token-1'
      && request.method() === 'DELETE'
    ) {
      deleteCount += 1
      token = { ...token, revoked_at: 1785196900 }
      return respond({ ok: true })
    }
    return request.continue()
  })

  await page.goto(baseUrl, { waitUntil: 'networkidle0' })
  await page.click('button[title="Account"]')
  await page.click('button[data-action="manage-tokens"]')
  await page.type('input[name="token-name"]', 'remote-codex')
  await page.select('select[name="token-preset"]', 'editor')
  await page.select('select[name="token-expiry"]', '90d')
  await page.click('.token-create-form button[type="submit"]')
  await page.waitForSelector('[data-testid="token-secret"]')

  assert.deepEqual(
    await page.$eval(
      '[data-testid="token-secret"]',
      (element) => element.textContent,
    ),
    'vbt_tok1_secret',
  )
  assert.equal(createPayload.name, 'remote-codex')
  assert.equal(createPayload.preset, 'editor')
  assert.equal(typeof createPayload.expires_at, 'number')
  assert.deepEqual(
    await page.evaluate(() => ({
      local: Object.values(localStorage),
      session: Object.values(sessionStorage),
      url: location.href,
    })),
    // Deep comparison below also proves the secret never entered browser storage or URL.
    { local: [], session: [], url: baseUrl.endsWith('/') ? baseUrl : `${baseUrl}/` },
  )
  await page.$eval('.token-dialog-backdrop', (element) => {
    element.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
  })
  assert.notEqual(await page.$('[data-testid="token-secret"]'), null)

  await page.click('button[data-action="close-token-secret"]')
  assert.equal(await page.$('[data-testid="token-secret"]'), null)
  await page.click('button[aria-label="Close token settings"]')
  await page.click('button[title="Account"]')
  await page.click('button[data-action="manage-tokens"]')
  await page.waitForSelector('.token-row')
  assert.equal(await page.$('[data-testid="token-secret"]'), null)

  await page.click('button[data-action="revoke-token"]')
  await page.click('button[data-action="confirm-revoke-token"]')
  await page.waitForFunction(() => (
    document.querySelector('.token-status')?.textContent === 'Revoked'
  ))
  assert.equal(deleteCount, 1)
})

test('desktop token creation layout keeps its title and controls aligned', async () => {
  const page = await browser.newPage()
  await page.setViewport({ width: 900, height: 800, deviceScaleFactor: 1 })
  await installEmptyAccountMocks(page)
  await page.goto(baseUrl, { waitUntil: 'networkidle0' })
  await page.click('button[title="Account"]')
  await page.click('button[data-action="manage-tokens"]')

  const geometry = await page.evaluate(() => {
    const form = document.querySelector('.token-create-form')
    const title = form.querySelector('.token-section-title').getBoundingClientRect()
    const labels = [...form.querySelectorAll('label')]
      .map((node) => node.getBoundingClientRect())
    const controls = [
      form.querySelector('input[name="token-name"]'),
      form.querySelector('select[name="token-preset"]'),
      form.querySelector('select[name="token-expiry"]'),
      form.querySelector('button[type="submit"]'),
    ].map((node) => node.getBoundingClientRect())
    return {
      pageWidth: document.documentElement.scrollWidth,
      viewportWidth: document.documentElement.clientWidth,
      titleBottom: title.bottom,
      firstLabelTop: Math.min(...labels.map((rect) => rect.top)),
      controlTops: controls.map((rect) => rect.top),
      controlLefts: controls.map((rect) => rect.left),
    }
  })

  assert.ok(geometry.titleBottom <= geometry.firstLabelTop)
  assert.ok(
    Math.max(...geometry.controlTops) - Math.min(...geometry.controlTops) <= 1,
  )
  assert.deepEqual(
    [...geometry.controlLefts].sort((left, right) => left - right),
    geometry.controlLefts,
  )
  assert.equal(geometry.pageWidth, geometry.viewportWidth)
})

test('token settings stack without horizontal page overflow on narrow screens', async () => {
  const page = await browser.newPage()
  await page.setViewport({ width: 390, height: 760, deviceScaleFactor: 1 })
  await installEmptyAccountMocks(page)
  await page.goto(baseUrl, { waitUntil: 'networkidle0' })
  await page.click('button[title="Account"]')
  await page.click('button[data-action="manage-tokens"]')
  const dimensions = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    page: document.documentElement.scrollWidth,
    columns: getComputedStyle(document.querySelector('.token-create-form')).gridTemplateColumns,
  }))
  assert.equal(dimensions.page, dimensions.viewport)
  assert.equal(dimensions.columns.split(' ').length, 1)
})
