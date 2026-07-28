import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import puppeteer from 'puppeteer'

const baseUrl = process.env.VIBE_TYPST_URL || 'http://127.0.0.1:9003'
const browser = await puppeteer.launch({ headless: true })

after(async () => {
  await browser.close()
})

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

test('token settings stack without horizontal page overflow on narrow screens', async () => {
  const page = await browser.newPage()
  await page.setViewport({ width: 390, height: 760, deviceScaleFactor: 1 })
  await page.setRequestInterception(true)
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname
    if (path === '/whoami') {
      return request.respond({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ username: 'alice', role: 'user' }),
      })
    }
    if (path === '/api/app/state') {
      return request.respond({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ configured: true, mode: 'server' }),
      })
    }
    if (path === '/api/projects') {
      return request.respond({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ projects: [] }),
      })
    }
    if (path === '/account/tokens') {
      return request.respond({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ tokens: [] }),
      })
    }
    return request.continue()
  })
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
