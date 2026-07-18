import assert from 'node:assert/strict'
import puppeteer from 'puppeteer'

const baseUrl = process.env.POINTER_E2E_URL || 'http://127.0.0.1:9003'
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

function watchPageErrors(page, errors) {
  page.on('pageerror', (error) => errors.push(error))
}

async function paintedSlide(page, hostSelector, imageSelector) {
  return page.$eval(hostSelector, (host, selector) => {
    const image = host.querySelector(selector)
    const hostRect = host.getBoundingClientRect()
    if (!image || !image.naturalWidth || !image.naturalHeight) throw new Error('slide image is not loaded')
    const scale = Math.min(hostRect.width / image.naturalWidth, hostRect.height / image.naturalHeight)
    const width = image.naturalWidth * scale
    const height = image.naturalHeight * scale
    return {
      hostLeft: hostRect.left,
      hostTop: hostRect.top,
      hostWidth: hostRect.width,
      hostHeight: hostRect.height,
      left: hostRect.left + (hostRect.width - width) / 2,
      top: hostRect.top + (hostRect.height - height) / 2,
      width,
      height,
    }
  }, imageSelector)
}

async function laserPosition(page) {
  return page.$eval('.proj-pointer', (pointer) => ({
    left: Number.parseFloat(pointer.style.left),
    top: Number.parseFloat(pointer.style.top),
  }))
}

async function waitForLaser(page) {
  await page.waitForSelector('.proj-pointer', { visible: true, timeout: 3000 })
}

async function waitForNoLaser(page) {
  await page.waitForSelector('.proj-pointer', { hidden: true, timeout: 3000 })
}

const browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] })
const errors = []

try {
  const context = await browser.createBrowserContext()
  const presenter = await context.newPage()
  await presenter.setViewport({ width: 1440, height: 900 })
  watchPageErrors(presenter, errors)
  await presenter.goto(baseUrl, { waitUntil: 'domcontentloaded' })
  await presenter.waitForSelector('.project-card-body', { timeout: 10000 })
  await presenter.evaluate(async () => {
    const state = await fetch('/api/app/state').then((response) => response.json())
    const cards = [...document.querySelectorAll('.project-card')]
    const active = cards.find((card) => (
      card.querySelector('.project-name')?.textContent === state.active_project?.name
    ))
    const target = active || cards[0]
    if (!target) throw new Error('no project available for pointer E2E')
    target.querySelector('.project-card-body').click()
  })
  await presenter.waitForSelector('.openbtn.present', { timeout: 10000 })
  await presenter.click('.openbtn.present')
  await presenter.waitForFunction(() => {
    const image = document.querySelector('.pr-current img')
    return image && image.naturalWidth > 0 && image.naturalHeight > 0
  }, { timeout: 10000 })

  const projection = await context.newPage()
  await projection.setViewport({ width: 1000, height: 1000 })
  watchPageErrors(projection, errors)
  await projection.goto(`${baseUrl}?project`, { waitUntil: 'domcontentloaded' })
  await projection.waitForFunction(() => {
    const image = document.querySelector('.proj-slide')
    return image && image.naturalWidth > 0 && image.naturalHeight > 0
  }, { timeout: 10000 })

  const slide = await paintedSlide(presenter, '.pr-current', 'img')
  await presenter.mouse.move(slide.left + slide.width / 2, slide.top + slide.height / 2)
  await presenter.mouse.down({ button: 'left' })
  await waitForLaser(projection)
  let laser = await laserPosition(projection)
  assert.ok(Math.abs(laser.left - 500) < 2, `center x was ${laser.left}`)
  assert.ok(Math.abs(laser.top - 500) < 2, `center y was ${laser.top}`)

  await presenter.mouse.move(slide.left + slide.width * 0.25, slide.top + slide.height * 0.8, { steps: 4 })
  await projection.waitForFunction(() => {
    const pointer = document.querySelector('.proj-pointer')
    return pointer && Number.parseFloat(pointer.style.left) < 300 && Number.parseFloat(pointer.style.top) > 600
  }, { timeout: 3000 })
  laser = await laserPosition(projection)
  const projectedSlide = await paintedSlide(projection, '.projection', 'img')
  assert.ok(Math.abs(laser.left - (projectedSlide.left + projectedSlide.width * 0.25)) < 2)
  assert.ok(Math.abs(laser.top - (projectedSlide.top + projectedSlide.height * 0.8)) < 2)

  await presenter.mouse.up({ button: 'left' })
  await waitForNoLaser(projection)

  // A press in the black letterbox must not create a false point on the slide.
  const topLetterbox = slide.top - slide.hostTop
  const leftLetterbox = slide.left - slide.hostLeft
  if (topLetterbox > 3 || leftLetterbox > 3) {
    const x = topLetterbox > 3 ? slide.left + slide.width / 2 : slide.hostLeft + 1
    const y = topLetterbox > 3 ? slide.hostTop + 1 : slide.top + slide.height / 2
    await presenter.mouse.move(x, y)
    await presenter.mouse.down({ button: 'left' })
    await pause(150)
    assert.equal(await projection.$('.proj-pointer'), null)
    await presenter.mouse.up({ button: 'left' })
  }

  // Navigation while pressed must clear the dot instead of stranding it on the projector.
  await presenter.mouse.move(slide.left + slide.width / 2, slide.top + slide.height / 2)
  await presenter.mouse.down({ button: 'left' })
  await waitForLaser(projection)
  await presenter.keyboard.press('ArrowRight')
  await waitForNoLaser(projection)
  await presenter.mouse.up({ button: 'left' })

  assert.deepEqual(errors, [])
  console.log('presentation pointer E2E: press, drag, release, letterbox, and navigation passed')
} finally {
  await browser.close()
}
