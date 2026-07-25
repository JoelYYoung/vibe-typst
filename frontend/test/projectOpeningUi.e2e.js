import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import puppeteer from 'puppeteer'

const baseUrl = process.env.VIBE_TYPST_URL || 'http://127.0.0.1:9003'
const browser = await puppeteer.launch({ headless: true })

after(async () => {
  await browser.close()
})

test('the selected project card shows opening feedback while navigation is pending', async () => {
  const page = await browser.newPage()
  await page.setViewport({ width: 1200, height: 800, deviceScaleFactor: 2 })
  await page.setRequestInterception(true)

  let pendingOpenRequest
  let captureOpenRequest
  const openRequestCaptured = new Promise(resolve => { captureOpenRequest = resolve })
  page.on('request', request => {
    if (request.method() === 'POST' && /\/api\/projects\/[^/]+\/open$/.test(request.url())) {
      pendingOpenRequest = request
      captureOpenRequest()
      return
    }
    request.continue()
  })

  await page.goto(baseUrl, { waitUntil: 'networkidle0' })
  await page.waitForSelector('.project-type-badge.pdf')
  await page.evaluate(() => {
    const badge = document.querySelector('.project-type-badge.pdf')
    badge.closest('.project-card').querySelector('.project-card-body').click()
  })
  await openRequestCaptured
  await page.waitForSelector('.project-card[aria-busy="true"] .project-opening-overlay', {
    timeout: 2000,
  })

  const observed = await page.evaluate(() => {
    const selected = document.querySelector('.project-card[aria-busy="true"]')
    const other = [...document.querySelectorAll('.project-card')].find(card => card !== selected)
    return {
      overlay: selected.querySelector('.project-opening-overlay')?.textContent.trim(),
      menuDisabled: selected.querySelector('.project-menu-btn')?.disabled,
      otherOverlay: other?.querySelector('.project-opening-overlay')?.textContent.trim() || null,
      otherOpenDisabled: other?.querySelector('.project-card-body')?.getAttribute('aria-disabled'),
    }
  })

  assert.equal(observed.overlay, 'Opening…')
  assert.equal(observed.menuDisabled, true)
  assert.equal(observed.otherOverlay, null)
  assert.equal(observed.otherOpenDisabled, 'true')

  await pendingOpenRequest.continue()
  await page.waitForSelector('.pdf-workspace')
})
