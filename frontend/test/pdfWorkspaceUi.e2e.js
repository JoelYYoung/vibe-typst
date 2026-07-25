import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import puppeteer from 'puppeteer'

const baseUrl = process.env.VIBE_TYPST_URL || 'http://127.0.0.1:9003'
const browser = await puppeteer.launch({ headless: true })
const openedPages = []

after(async () => {
  await browser.close()
})

const delay = (milliseconds) => new Promise(resolve => setTimeout(resolve, milliseconds))

async function openPdfWorkspace({ countSockets = false } = {}) {
  const page = await browser.newPage()
  openedPages.push(page)
  await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 2 })
  if (countSockets) {
    await page.evaluateOnNewDocument(() => {
      const NativeWebSocket = window.WebSocket
      window.__openedWebSockets = 0
      window.WebSocket = new Proxy(NativeWebSocket, {
        construct(target, argumentsList) {
          window.__openedWebSockets += 1
          return Reflect.construct(target, argumentsList)
        },
      })
    })
  }
  await page.goto(baseUrl, { waitUntil: 'networkidle0' })
  await page.waitForSelector('.project-type-badge.pdf')
  await page.evaluate(() => {
    const badge = document.querySelector('.project-type-badge.pdf')
    badge.closest('.project-card').querySelector('.project-card-body').click()
  })
  await page.waitForSelector('.pdf-workspace')
  await page.waitForSelector('.pdf-page-stage img')
  await page.waitForFunction(() => {
    const image = document.querySelector('.pdf-page-stage img')
    return image?.complete && image.naturalWidth > 0
  })
  return page
}

async function clickButton(page, label, rootSelector = 'body') {
  await page.waitForFunction(
    (text, selector) => [...document.querySelector(selector).querySelectorAll('button')].some(
      button => button.textContent.trim().includes(text) && !button.disabled,
    ),
    {},
    label,
    rootSelector,
  )
  await page.evaluate((text, selector) => {
    const button = [...document.querySelector(selector).querySelectorAll('button')].find(
      candidate => candidate.textContent.trim().includes(text) && !candidate.disabled,
    )
    button.click()
  }, label, rootSelector)
}

async function previewPageNumber(page) {
  return page.$eval('.pdf-page-controls span', node => Number(node.textContent.match(/Page (\d+)/)[1]))
}

async function presenterPageNumber(page) {
  return page.$eval('.pr-page', node => Number(node.textContent.match(/(\d+)/)[1]))
}

test('PDF toolbar mirrors Typst placement while omitting unavailable actions', async () => {
  const page = await openPdfWorkspace()
  const observed = await page.evaluate(() => {
    const toolbar = document.querySelector('.pdf-workspace > .bar')
    const title = toolbar.querySelector('.bar-title')
    const titleRect = title?.getBoundingClientRect()
    return {
      hasSharedTitle: Boolean(title),
      title: title?.textContent.trim() || '',
      centerDelta: titleRect
        ? Math.abs(titleRect.left + titleRect.width / 2 - window.innerWidth / 2)
        : null,
      buttons: [...toolbar.querySelectorAll('button')].map(button => button.textContent.trim()),
      status: toolbar.querySelector('.status-chip.live')?.textContent.trim() || '',
      hasDrawer: Boolean(document.querySelector('.pdf-drawer')),
    }
  })

  assert.equal(observed.hasSharedTitle, true)
  assert.ok(observed.title)
  assert.ok(observed.centerDelta <= 1)
  assert.deepEqual(observed.buttons, ['← Projects', '▶ Present'])
  assert.equal(observed.status, 'no presentation')
  assert.equal(observed.hasDrawer, false)
})

test('PDF workspace renders a sharp page with Typst terminal chrome', async () => {
  const page = await openPdfWorkspace()
  const observed = await page.evaluate(() => {
    const image = document.querySelector('.pdf-page-stage img')
    return {
      layout: getComputedStyle(document.querySelector('.pdf-workspace-main')).display,
      divider: Boolean(document.querySelector('.pdf-workspace-divider')),
      terminalHeader: document.querySelector('.pdf-terminal-pane .term-head')?.textContent.trim(),
      longestImageEdge: Math.max(image.naturalWidth, image.naturalHeight),
      imageRendering: getComputedStyle(image).imageRendering,
    }
  })

  assert.equal(observed.layout, 'flex')
  assert.equal(observed.divider, true)
  assert.ok(observed.terminalHeader)
  assert.ok(observed.longestImageEdge >= 2500)
  assert.equal(observed.imageRendering, 'auto')
})

test('dragging the PDF divider refits without reconnecting the terminal', async () => {
  const page = await openPdfWorkspace({ countSockets: true })
  await delay(500)
  const terminalText = await page.$eval('.xterm-rows', node => node.textContent)
  assert.equal((terminalText.match(/\bcd '/g) || []).length, 0)

  const divider = await page.waitForSelector('.pdf-workspace-divider')
  const box = await divider.boundingBox()
  const before = await page.evaluate(() => ({
    width: document.querySelector('.pdf-terminal-pane').getBoundingClientRect().width,
    sockets: window.__openedWebSockets,
  }))

  await page.mouse.move(box.x + box.width / 2, box.y + 100)
  await page.mouse.down()
  await page.mouse.move(box.x + 140, box.y + 100, { steps: 8 })
  await page.mouse.up()
  await delay(150)

  const afterDrag = await page.evaluate(() => ({
    width: document.querySelector('.pdf-terminal-pane').getBoundingClientRect().width,
    sockets: window.__openedWebSockets,
  }))
  assert.ok(afterDrag.width > before.width + 100)
  assert.equal(afterDrag.sockets, before.sockets)
})

test('PDF Preview and Presentation pages synchronize only through explicit controls', async () => {
  const page = await openPdfWorkspace()
  await clickButton(page, 'Next', '.pdf-page-controls')
  await clickButton(page, 'Next', '.pdf-page-controls')
  assert.equal(await previewPageNumber(page), 3)

  await clickButton(page, 'Present', '.bar')
  assert.equal(await presenterPageNumber(page), 3)
  await clickButton(page, 'Next', '.pr-nav')
  assert.equal(await presenterPageNumber(page), 4)

  const projectionTarget = browser.waitForTarget(
    target => target.url().startsWith(`${baseUrl}/?project`),
  )
  await clickButton(page, 'Open projection', '.pr-top')
  const projection = await (await projectionTarget).page()
  openedPages.push(projection)
  await projection.waitForSelector('.projection')
  await clickButton(page, 'Exit', '.pr-top')
  await delay(1800)

  assert.equal(await previewPageNumber(page), 3)
  await clickButton(page, 'Follow presentation', '.pdf-preview-head')
  assert.equal(await previewPageNumber(page), 4)
  await clickButton(page, 'Previous', '.pdf-page-controls')
  assert.equal(await previewPageNumber(page), 3)
  await clickButton(page, 'Send preview', '.pdf-preview-head')
  await clickButton(page, 'Present', '.bar')
  assert.equal(await presenterPageNumber(page), 3)
})
