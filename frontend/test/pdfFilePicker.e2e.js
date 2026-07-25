import assert from 'node:assert/strict'
import puppeteer from 'puppeteer'

const baseUrl = process.env.VIBE_TYPST_URL || 'http://127.0.0.1:9003'
const browser = await puppeteer.launch({ headless: true })
const page = await browser.newPage()
await page.setViewport({ width: 1100, height: 760, deviceScaleFactor: 2 })

try {
  await page.goto(baseUrl, { waitUntil: 'networkidle0' })
  await page.evaluate(() => {
    const button = [...document.querySelectorAll('button')]
      .find(candidate => candidate.textContent.includes('New project'))
    button.click()
  })
  await page.waitForSelector('.new-project-form')
  await page.evaluate(() => {
    const button = [...document.querySelectorAll('.new-project-type button')]
      .find(candidate => candidate.textContent.trim() === 'PDF')
    button.click()
  })

  const picker = await page.waitForSelector('.pdf-file-picker')
  const inputVisibility = await page.$eval('.pdf-file-picker-input', node => {
    const style = getComputedStyle(node)
    return {
      opacity: style.opacity,
      position: style.position,
      width: node.getBoundingClientRect().width,
    }
  })
  assert.equal(inputVisibility.opacity, '0')
  assert.equal(inputVisibility.position, 'absolute')
  assert.ok(inputVisibility.width <= 1)

  await page.evaluate(() => {
    const input = document.querySelector('.pdf-file-picker-input')
    const transfer = new DataTransfer()
    transfer.items.add(new File(
      [new Uint8Array(2048)],
      'deck.pdf',
      { type: 'application/pdf' },
    ))
    input.files = transfer.files
    input.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await page.waitForSelector('.pdf-file-picker-selection')
  assert.equal(
    await page.$eval('.pdf-file-picker-name', node => node.textContent),
    'deck.pdf',
  )
  assert.equal(
    await page.$eval('.pdf-file-picker-size', node => node.textContent),
    '2 KB',
  )
  assert.deepEqual(
    await page.$$eval('.pdf-file-picker button', buttons => buttons.map(
      button => button.textContent.trim(),
    )),
    ['Change', 'Remove'],
  )

  await picker.evaluate(node => {
    const transfer = new DataTransfer()
    transfer.items.add(new File(
      [new Uint8Array(10)],
      'notes.txt',
      { type: 'text/plain' },
    ))
    node.dispatchEvent(new DragEvent('drop', {
      bubbles: true,
      cancelable: true,
      dataTransfer: transfer,
    }))
  })
  assert.equal(
    await page.$eval('.pdf-file-picker-name', node => node.textContent),
    'deck.pdf',
  )

  await page.evaluate(() => {
    const button = [...document.querySelectorAll('.pdf-file-picker button')]
      .find(candidate => candidate.textContent.trim() === 'Remove')
    button.click()
  })
  await page.waitForSelector('.pdf-file-picker-selection', { hidden: true })
  assert.match(
    await page.$eval('.pdf-file-picker', node => node.textContent),
    /Select PDF/,
  )
} finally {
  await browser.close()
}
