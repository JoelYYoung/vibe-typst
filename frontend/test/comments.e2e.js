import assert from 'node:assert/strict'
import puppeteer from 'puppeteer'

const baseUrl = process.env.COMMENTS_E2E_URL || 'http://127.0.0.1:9003'
const browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] })
const pageErrors = []

try {
  const page = await browser.newPage()
  await page.setViewport({ width: 1440, height: 900 })
  page.on('pageerror', (error) => pageErrors.push(error))
  await page.setRequestInterception(true)
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname
    if (path === '/api/comments') {
      request.respond({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          { id: 'older', seq: 1, kind: 'page', status: 'done', body: 'finished earlier', done_at: '2026-07-18T11:00:00' },
          { id: 'newer', seq: 2, kind: 'page', status: 'done', body: 'finished later', done_at: '2026-07-18T12:00:00' },
        ]),
      })
    } else {
      request.continue()
    }
  })

  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.project-card-body', { timeout: 10000 })
  await page.evaluate(async () => {
    const state = await fetch('/api/app/state').then((response) => response.json())
    const cards = [...document.querySelectorAll('.project-card')]
    const active = cards.find((card) => (
      card.querySelector('.project-name')?.textContent === state.active_project?.name
    ))
    const target = active || cards[0]
    if (!target) throw new Error('no project available for comments E2E')
    target.querySelector('.project-card-body').click()
  })
  await page.waitForSelector('.tabs', { timeout: 10000 })
  await page.evaluate(() => {
    const done = [...document.querySelectorAll('.tab')]
      .find((button) => button.textContent.trim() === 'done')
    if (!done) throw new Error('Done tab not found')
    done.click()
  })
  await page.waitForSelector('.clist .card', { timeout: 5000 })

  const order = await page.$$eval('.clist .card .seq', (nodes) => nodes.map((node) => node.textContent.trim()))
  assert.deepEqual(order, ['#2', '#1'])
  assert.deepEqual(pageErrors, [])
  console.log('comments E2E: latest completed comment is rendered first')
} finally {
  await browser.close()
}
