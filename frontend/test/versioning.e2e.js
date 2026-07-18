import assert from 'node:assert/strict'
import puppeteer from 'puppeteer'

const baseUrl = process.env.VERSIONING_E2E_URL || 'http://127.0.0.1:9003'
const browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] })
const pageErrors = []

try {
  const page = await browser.newPage()
  await page.setViewport({ width: 1440, height: 900 })
  page.on('pageerror', (error) => pageErrors.push(error))
  if (process.env.VERSIONING_E2E_LIVE !== '1') {
    await page.setRequestInterception(true)
    page.on('request', (request) => {
      const path = new URL(request.url()).pathname
      if (path === '/api/git/status') {
        request.respond({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ initialized: false, dirty: false, current: null }),
        })
      } else if (path === '/api/git/versions') {
        request.respond({ status: 200, contentType: 'application/json', body: '[]' })
      } else {
        request.continue()
      }
    })
  }

  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.project-card-body', { timeout: 10000 })
  await page.evaluate(async () => {
    const state = await fetch('/api/app/state').then((response) => response.json())
    const cards = [...document.querySelectorAll('.project-card')]
    const active = cards.find((card) => (
      card.querySelector('.project-name')?.textContent === state.active_project?.name
    ))
    const target = active || cards[0]
    if (!target) throw new Error('no project available for versioning E2E')
    target.querySelector('.project-card-body').click()
  })
  await page.waitForSelector('button[title="Manage project files"]', { timeout: 10000 })
  await page.click('button[title="Manage project files"]')

  const saveButton = await page.waitForSelector('.git-commit-btn', { timeout: 5000 })
  assert.equal(await saveButton.evaluate((button) => button.disabled), false)
  await saveButton.click()
  const submit = await page.waitForSelector('.git-commit-form button[type="submit"]', { timeout: 5000 })
  assert.equal(await submit.evaluate((button) => button.disabled), false)
  assert.deepEqual(pageErrors, [])
  console.log('versioning E2E: first-version controls are enabled with no repository or tags')
} finally {
  await browser.close()
}
