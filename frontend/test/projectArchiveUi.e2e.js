import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import puppeteer from 'puppeteer'

const baseUrl = process.env.VIBE_TYPST_URL || 'http://127.0.0.1:9003'
const browser = await puppeteer.launch({ headless: true })

after(async () => {
  await browser.close()
})

function json(request, body, status = 200) {
  return request.respond({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  })
}

test('a project can be archived from Projects and restored from Archived', async () => {
  const page = await browser.newPage()
  let project = {
    id: 'deck-1',
    name: 'Important deck',
    type: 'typst',
    main_file: 'main.typ',
    created: '2026-08-05T00:00:00Z',
    archived: false,
    archived_at: null,
  }
  await page.setRequestInterception(true)
  page.on('request', request => {
    const url = new URL(request.url())
    if (url.pathname === '/api/app/state') {
      return json(request, { configured: true, mode: 'server' })
    }
    if (url.pathname === '/whoami') return json(request, {}, 404)
    if (url.pathname === '/api/projects' && request.method() === 'GET') {
      const wantsArchived = url.searchParams.get('archived') === 'true'
      return json(request, { projects: project.archived === wantsArchived ? [project] : [] })
    }
    if (url.pathname === '/api/projects/deck-1/archive' && request.method() === 'POST') {
      project = { ...project, archived: true, archived_at: '2026-08-05T01:00:00Z' }
      return json(request, project)
    }
    if (url.pathname === '/api/projects/deck-1/restore' && request.method() === 'POST') {
      project = { ...project, archived: false, archived_at: null }
      return json(request, project)
    }
    return request.continue()
  })

  await page.goto(baseUrl, { waitUntil: 'networkidle0' })
  await page.waitForSelector('.project-card')
  await page.click('.project-menu-btn')
  await page.evaluate(() => {
    [...document.querySelectorAll('.project-dropdown button')]
      .find(button => button.textContent.trim() === 'Archive')
      .click()
  })
  await page.waitForFunction(() => document.querySelector('.projects-empty')?.textContent.includes('No projects yet'))

  await page.evaluate(() => {
    [...document.querySelectorAll('.projects-view-toggle button')]
      .find(button => button.textContent.trim() === 'Archived')
      .click()
  })
  await page.waitForSelector('.project-card.archived')
  assert.equal(
    await page.$eval('.project-card.archived .project-name', element => element.textContent),
    'Important deck',
  )
  await page.click('.project-menu-btn')
  await page.evaluate(() => {
    [...document.querySelectorAll('.project-dropdown button')]
      .find(button => button.textContent.trim() === 'Restore')
      .click()
  })
  await page.waitForFunction(() => document.querySelector('.projects-empty')?.textContent.includes('No archived projects'))

  await page.evaluate(() => {
    [...document.querySelectorAll('.projects-view-toggle button')]
      .find(button => button.textContent.trim() === 'Projects')
      .click()
  })
  await page.waitForSelector('.project-card:not(.archived)')
})
