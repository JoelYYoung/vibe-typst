import assert from 'node:assert/strict'
import puppeteer from 'puppeteer'

const baseUrl = process.env.FILE_MANAGER_E2E_URL || 'http://127.0.0.1:9003'
const runId = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`
const folder = `drop-e2e-${runId}`
const filename = `collision-${runId}.txt`
const initialContent = `folder copy ${runId}`
const movedContent = `root copy ${runId}`

async function jsonRequest(page, url, options = {}) {
  return page.evaluate(async ({ requestUrl, requestOptions }) => {
    const response = await fetch(requestUrl, requestOptions)
    const payload = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(payload.detail || `${response.status} ${response.statusText}`)
    return payload
  }, { requestUrl: url, requestOptions: options })
}

async function fileList(page) {
  return jsonRequest(page, '/api/project/files')
}

async function fileContent(page, path) {
  return page.evaluate(async (targetPath) => {
    const response = await fetch(`/api/project/files/view?path=${encodeURIComponent(targetPath)}`)
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
    return response.text()
  }, path)
}

async function folderRow(page, name) {
  const handle = await page.evaluateHandle((folderName) => {
    const label = [...document.querySelectorAll('.fm-name')]
      .find((node) => node.textContent.trim() === folderName)
    return label?.closest('.fm-row') || null
  }, name)
  const element = handle.asElement()
  if (!element) {
    await handle.dispose()
    throw new Error(`folder row ${name} was not rendered`)
  }
  return element
}

async function dropExternalFile(page, folderName, name, content) {
  const row = await folderRow(page, folderName)
  await page.evaluate((target, payload) => {
    const transfer = new DataTransfer()
    transfer.items.add(new File([payload.content], payload.name, { type: 'text/plain' }))
    for (const type of ['dragenter', 'dragover', 'drop']) {
      target.dispatchEvent(new DragEvent(type, {
        bubbles: true,
        cancelable: true,
        dataTransfer: transfer,
      }))
    }
  }, row, { name, content })
  await row.dispose()
}

async function dropInternalFile(page, folderName, sourcePath) {
  const row = await folderRow(page, folderName)
  await page.evaluate((target, path) => {
    const transfer = new DataTransfer()
    transfer.setData('application/x-fm-move', JSON.stringify([path]))
    for (const type of ['dragenter', 'dragover', 'drop']) {
      target.dispatchEvent(new DragEvent(type, {
        bubbles: true,
        cancelable: true,
        dataTransfer: transfer,
      }))
    }
  }, row, sourcePath)
  await row.dispose()
}

async function waitForDirectUpload(page, expected) {
  await page.waitForFunction(async (value) => {
    const response = await fetch('/api/project/files')
    if (!response.ok) return false
    const { items = [] } = await response.json()
    const paths = items.map((item) => item.path)
    return paths.includes(`${value.folder}/${value.filename}`) && !paths.includes(value.filename)
  }, { timeout: 5000, polling: 100 }, expected)
}

async function waitForCollisionMove(page, expected) {
  await page.waitForFunction(async (value) => {
    const response = await fetch('/api/project/files')
    if (!response.ok) return false
    const { items = [] } = await response.json()
    const paths = items.map((item) => item.path)
    return !paths.includes(value.filename)
      && paths.includes(`${value.folder}/${value.filename}`)
      && paths.includes(`${value.folder}/${value.suffixed}`)
  }, { timeout: 5000, polling: 100 }, expected)
}

const browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] })
const pageErrors = []
const failedFileRequests = []
let page

try {
  const context = await browser.createBrowserContext()
  page = await context.newPage()
  await page.setViewport({ width: 1440, height: 900 })
  page.on('pageerror', (error) => pageErrors.push(error))
  page.on('response', (response) => {
    if (response.url().includes('/api/project/files') && response.status() >= 400) {
      failedFileRequests.push(`${response.status()} ${response.request().method()} ${response.url()}`)
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
    if (!target) throw new Error('no project available for file-manager E2E')
    target.querySelector('.project-card-body').click()
  })
  await page.waitForSelector('button[title="Manage project files"]', { timeout: 10000 })

  await jsonRequest(page, '/api/project/files/mkdir', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: folder }),
  })
  await page.click('button[title="Manage project files"]')
  await page.waitForSelector('.filemgr-list', { timeout: 5000 })
  await page.waitForFunction((folderName) => (
    [...document.querySelectorAll('.fm-name')].some((node) => node.textContent.trim() === folderName)
  ), { timeout: 5000 }, folder)

  // Regression: an operating-system file dropped on a folder must upload into that folder,
  // and the bubbling root handler must not upload a second copy at the project root.
  await dropExternalFile(page, folder, filename, initialContent)
  await waitForDirectUpload(page, { folder, filename })
  assert.equal(await fileContent(page, `${folder}/${filename}`), initialContent)

  // Reproduce the reported 409: a root file has the same name as one already in the folder.
  // The move must preserve both files and choose a deterministic numbered name.
  await page.evaluate(async ({ name, content }) => {
    const data = new FormData()
    data.append('file', new File([content], name, { type: 'text/plain' }))
    const response = await fetch('/api/project/files/upload', { method: 'POST', body: data })
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
  }, { name: filename, content: movedContent })
  await page.waitForFunction((name) => (
    [...document.querySelectorAll('.fm-name')].some((node) => node.textContent.trim() === name)
  ), { timeout: 5000 }, filename)
  await dropInternalFile(page, folder, filename)

  const suffixed = filename.replace(/(\.[^.]*)$/, '_1$1')
  await waitForCollisionMove(page, { folder, filename, suffixed })
  assert.equal(await fileContent(page, `${folder}/${filename}`), initialContent)
  assert.equal(await fileContent(page, `${folder}/${suffixed}`), movedContent)

  const { items } = await fileList(page)
  assert.equal(items.some((item) => item.path === filename), false)
  assert.deepEqual(pageErrors, [])
  assert.deepEqual(failedFileRequests, [])
  assert.equal(await page.$('.filemgr-error'), null)
  console.log('file manager E2E: folder upload and collision-preserving move passed')
} finally {
  if (page) {
    await page.evaluate(async ({ testFolder, rootFile }) => {
      await fetch('/api/project/dirs', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: testFolder }),
      }).catch(() => {})
      await fetch('/api/project/files', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: rootFile }),
      }).catch(() => {})
    }, { testFolder: folder, rootFile: filename }).catch(() => {})
  }
  await browser.close()
}
