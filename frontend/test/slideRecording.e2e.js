import assert from 'node:assert/strict'
import { spawn, execFileSync } from 'node:child_process'
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import net from 'node:net'
import puppeteer from 'puppeteer'

const repo = fileURLToPath(new URL('../../', import.meta.url))
const temp = await mkdtemp(`${tmpdir()}/slide-recording-test-`)
const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms))
const waitFor = (page, predicate, timeout = 10000) => page.waitForFunction(predicate, { polling: 100, timeout })
let server
let baseUrl = process.env.RECORDING_E2E_URL
if (!baseUrl) {
  const socket = net.createServer()
  await new Promise(resolve => socket.listen(0, '127.0.0.1', resolve))
  const port = socket.address().port
  await new Promise(resolve => socket.close(resolve))
  baseUrl = `http://127.0.0.1:${port}`
  server = spawn(process.env.RECORDING_TEST_PYTHON || `${repo}backend/.venv/bin/python`, [`${repo}tests/recording_e2e_server.py`], {
    cwd: repo, env: { ...process.env, RECORDING_E2E_PORT: String(port) }, stdio: ['ignore', 'ignore', 'pipe'],
  })
  server.stderr.on('data', chunk => process.stderr.write(chunk))
}
const browser = await puppeteer.launch({ headless: true, args: [
  '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', '--autoplay-policy=no-user-gesture-required',
] })

async function clickText(page, selector, text) {
  await page.evaluate((selector, text) => {
    const button = [...document.querySelectorAll(selector)].find(el => (el.getAttribute('aria-label') || el.textContent.trim()) === text)
    if (!button || button.disabled) throw new Error(`missing or disabled button: ${text}`)
    button.click()
  }, selector, text)
}
async function takes(projectId) {
  return fetch(`${baseUrl}/test/takes?project_id=${projectId}`).then(response => response.json())
}
async function enterPresenter(page, projectId) {
  console.log(`${projectId}: opening presenter`)
  await page.goto(`${baseUrl}/?openProject=${projectId}`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.openbtn.present')
  await waitFor(page, () => document.querySelector('.openbtn.present')?.disabled === false)
  await page.evaluate(() => document.querySelector('.openbtn.present').click())
  await waitFor(page, () => document.querySelector('.pr-slide')?.naturalWidth > 0)
  await page.evaluate(() => document.querySelector('.pr-record-mode').click())
  await waitFor(page, () => document.querySelector('.pr-record-start')?.disabled === false)
}
async function recordPage(page, expectSave = true) {
  await page.evaluate(() => document.querySelector('.pr-record-start').click())
  await page.waitForSelector('.pr-record-stop', { timeout: 10000 })
  await waitFor(page, () => {
    const value = document.querySelector('.pr-mic-meter')?.getAttribute('aria-valuenow')
    return value != null && Number(value) > -40
  })
  const initialLevel = await page.$eval('.pr-mic-meter', node => Number(node.getAttribute('aria-valuenow')))
  await page.evaluate(() => { window.__recordingTestGain.gain.value = .35 })
  await waitFor(page, () => {
    const value = document.querySelector('.pr-mic-meter').getAttribute('aria-valuenow')
    return value != null && Number(value) > -18
  })
  const louderLevel = await page.$eval('.pr-mic-meter', node => Number(node.getAttribute('aria-valuenow')))
  assert.ok(louderLevel > initialLevel + 7, 'meter must respond to the actual input volume')
  assert.ok(await page.$eval('.pr-mic-meter', node => Math.abs(node.getBoundingClientRect().right - node.parentElement.getBoundingClientRect().right) < 4), 'meter must sit at the right of the same toolbar row')
  await page.evaluate(() => { window.__recordingTestGain.gain.value = 0 })
  await waitFor(page, () => Number(document.querySelector('.pr-mic-meter').getAttribute('aria-valuenow')) <= -59)
  await page.evaluate(() => { window.__recordingTestGain.gain.value = .1 })
  assert.equal(await page.$eval('.pr-nav-btn', el => el.disabled), true)
  await page.keyboard.press('ArrowRight')
  await page.keyboard.press('Escape')
  assert.ok(await page.$('.pr-record-stop'), 'recording must prevent navigation and exit')
  const rect = await page.$eval('.pr-current img', image => {
    const box = image.parentElement.getBoundingClientRect()
    const scale = Math.min(box.width / image.naturalWidth, box.height / image.naturalHeight)
    const width = image.naturalWidth * scale
    const height = image.naturalHeight * scale
    return { left: box.left + (box.width - width) / 2, top: box.top + (box.height - height) / 2, width, height }
  })
  await page.mouse.move(rect.left + rect.width * .2, rect.top + rect.height * .3)
  assert.equal(await page.$('.pr-pointer'), null, 'hovering must not show a laser while recording')
  await sleep(150)
  await page.mouse.down({ button: 'left' })
  await page.waitForSelector('.pr-pointer')
  await page.mouse.move(rect.left + rect.width * .65, rect.top + rect.height * .55, { steps: 3 })
  await sleep(1100)
  await page.mouse.up({ button: 'left' })
  await waitFor(page, () => !document.querySelector('.pr-pointer'))
  await page.mouse.move(rect.left + rect.width * .8, rect.top + rect.height * .7)
  await sleep(400)
  assert.equal(await page.$('.pr-pointer'), null, 'released laser must stay hidden when the mouse moves')
  await page.evaluate(() => document.querySelector('.pr-record-stop').click())
  await waitFor(page, () => !document.querySelector('.pr-mic-meter'))
  await waitFor(page, () => document.querySelector('.pr-record-start')?.getAttribute('aria-label') !== 'Saving page…')
  if (expectSave) await waitFor(page, () => document.querySelector('.pr-record-start')?.disabled === false)
}

async function checkTranscriptControls(page) {
  const draft = await page.$eval('.pr-note-edit', node => node.value)
  const initialHeight = await page.$eval('.pr-note-edit', node => node.clientHeight)
  await page.evaluate(() => document.querySelector('.pr-hide-preview').click())
  assert.equal(await page.$('.pr-next'), null)
  const expanded = await page.evaluate(() => ({
    textarea: document.querySelector('.pr-note-edit').clientHeight,
    notes: document.querySelector('.pr-notes').getBoundingClientRect().height,
    side: document.querySelector('.pr-side').getBoundingClientRect().height,
  }))
  assert.ok(expanded.textarea > initialHeight + 100, 'hiding preview must give its space to the transcript')
  assert.ok(Math.abs(expanded.notes - expanded.side) < 2, 'transcript must fill the right column')
  for (let count = 0; count < 3; count++) {
    await page.evaluate(() => document.querySelector('[aria-label="Increase transcript font size"]').click())
  }
  await page.evaluate(() => document.querySelector('[aria-label="Decrease transcript font size"]').click())
  assert.equal(await page.$eval('.pr-note-edit', node => getComputedStyle(node).fontSize), '19px')
  assert.equal(await page.$eval('.pr-note-edit', node => node.value), draft, 'display settings must preserve the transcript draft')
  await page.evaluate(() => document.querySelector('.pr-show-preview').click())
  assert.ok(await page.$('.pr-next'), 'preview can be restored')
  await page.evaluate(() => document.querySelector('.pr-hide-preview').click())
}

try {
  const deadline = Date.now() + 10000
  while (true) {
    try { if ((await fetch(`${baseUrl}/api/app/state`)).ok) break } catch {}
    if (Date.now() > deadline) throw new Error('recording fixture server did not start')
    await sleep(100)
  }
  for (const projectId of ['recording-typst', 'recording-pdf']) {
    const page = await browser.newPage()
    await page.goto(baseUrl, { waitUntil: 'domcontentloaded' })
    await page.evaluate(() => localStorage.removeItem('vibe-typst.presenter-preferences'))
    await page.evaluateOnNewDocument(() => {
      const capture = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices)
      navigator.mediaDevices.getUserMedia = async (constraints) => {
        const microphone = await capture(constraints)
        // macOS headless capture can supply silent fake input even with a WAV flag.
        // Add a deterministic tone to a real MediaStream, retaining the native permission
        // and device acquisition path; no physical microphone is used by this test.
        const context = new AudioContext()
        await context.resume()
        const destination = context.createMediaStreamDestination()
        context.createMediaStreamSource(microphone).connect(destination)
        const oscillator = context.createOscillator()
        const gain = context.createGain()
        oscillator.frequency.value = 440
        gain.gain.value = .1
        window.__recordingTestGain = gain
        oscillator.connect(gain).connect(destination)
        oscillator.start()
        const track = destination.stream.getAudioTracks()[0]
        const stop = track.stop.bind(track)
        track.stop = () => {
          stop()
          microphone.getTracks().forEach(track => track.stop())
          oscillator.stop()
          context.close()
        }
        return destination.stream
      }
    })
    await page.setViewport({ width: 1440, height: 900 })
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    await enterPresenter(page, projectId)
    assert.ok(await page.$eval('.pr-recording-row', row => {
      const buttons = [...row.querySelectorAll('button')]
      const top = buttons[0].getBoundingClientRect().top
      return row.scrollWidth <= row.clientWidth && buttons.every(button =>
        Math.abs(button.getBoundingClientRect().top - top) < 1
        && !button.textContent.trim() && button.title && button.getAttribute('aria-label'))
    }), 'recording actions must fit one row with icon-only accessible buttons and hover details')
    await checkTranscriptControls(page)
    await page.$eval('[aria-label="Target presentation minutes"]', input => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set
      setter.call(input, '6')
      input.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await waitFor(page, () => document.querySelector('.pr-page-budget')?.textContent.includes('03:00'))
    await recordPage(page)
    const first = (await takes(projectId))[0]
    assert.ok(first.pointer.length >= 2)
    assert.ok(first.pointer.some(point => point.x > .6))
    await clickText(page, '.pr-recording button', 'Preview page')
    await waitFor(page, () => document.querySelector('.pr-recording-preview video')?.readyState >= 2)
    const previewDuration = await page.$eval('.pr-recording-preview video', video => video.duration)
    assert.ok(Number.isFinite(previewDuration) && Math.abs(previewDuration - first.duration) < .2, 'preview must know the full duration before playing through')
    await page.$eval('.pr-recording-preview video', video => { video.pause(); video.currentTime = video.duration * .7 })
    await waitFor(page, () => !document.querySelector('.pr-recording-preview video').seeking)
    assert.equal(await page.$eval('.pr-recording-preview video', video => video.duration), previewDuration, 'seeking must not change the timeline length')
    await clickText(page, '.pr-recording button', 'Close preview')
    await clickText(page, '.pr-nav button', 'Next page')
    await recordPage(page)
    const before = await takes(projectId)
    assert.equal(before.length, 2)
    await clickText(page, '.pr-nav button', 'Previous page')

    // A failed save must preserve both older takes and expose a recoverable local recording.
    let failOnce = true
    await page.setRequestInterception(true)
    page.on('request', request => {
      if (failOnce && request.method() === 'PUT' && request.url().includes('/api/recording/pages/1')) {
        failOnce = false
        request.respond({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Temporary save failure' }) }).catch(() => {})
      } else request.continue().catch(() => {})
    })
    await recordPage(page, false)
    await page.waitForSelector('.pr-recording-recovery a')
    const failed = await takes(projectId)
    assert.deepEqual(failed.map(take => take.sha256), before.map(take => take.sha256))
    await clickText(page, '.pr-recording-recovery button', 'Retry saving page 1')
    await waitFor(page, () => !document.querySelector('.pr-recording-recovery'))
    const after = await takes(projectId)
    assert.notEqual(after[0].take, before[0].take)
    assert.equal(after[1].take, before[1].take)
    assert.equal(after[1].sha256, before[1].sha256)
    await clickText(page, '.pr-top button', 'Exit presenter')
    await page.evaluate(() => document.querySelector('.openbtn.present').click())
    await page.evaluate(() => document.querySelector('.pr-record-mode').click())
    await waitFor(page, () => document.querySelectorAll('.pr-thumb-recorded.recorded').length === 2)
    // Reload the entire app, then reopen the same project to verify server persistence.
    await enterPresenter(page, projectId)
    await waitFor(page, () => document.querySelectorAll('.pr-thumb-recorded.recorded').length === 2)
    assert.equal(await page.$('.pr-next'), null, 'hidden preview preference must survive reload')
    assert.equal(await page.$eval('[aria-label="Target presentation minutes"]', input => input.value), '6')
    assert.equal(await page.$eval('.pr-note-edit', node => getComputedStyle(node).fontSize), '19px', 'font size must survive reload')
    await clickText(page, '.pr-recording button', 'Export full MP4')
    await waitFor(page, () => !!document.querySelector('.pr-recording a[download="presentation.mp4"]'), 60000)
    const href = await page.$eval('.pr-recording a[download="presentation.mp4"]', link => link.href)
    const response = await fetch(href)
    assert.equal(response.status, 200)
    const mp4 = `${temp}/${projectId}.mp4`
    await writeFile(mp4, Buffer.from(await response.arrayBuffer()))
    const info = JSON.parse(execFileSync('ffprobe', ['-v', 'error', '-show_streams', '-show_format', '-of', 'json', mp4]))
    assert.ok(info.streams.some(stream => stream.codec_name === 'h264' && stream.width === 1920 && stream.height === 1080))
    assert.ok(info.streams.some(stream => stream.codec_name === 'aac'))
    assert.ok(Math.abs(Number(info.format.duration) - after.reduce((total, take) => total + take.duration, 0)) < .2)
    // Inspect the actual exported cursor pixel while it rests at the captured slide position.
    const pointerTime = after[0].pointer.filter(point => point.x > .64 && point.x < .66).at(-1).t + .3
    const pixel = execFileSync('ffmpeg', ['-v', 'error', '-ss', String(pointerTime), '-i', mp4, '-frames:v', '1',
      '-vf', 'crop=2:2:1248:594,scale=1:1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'])
    assert.ok(pixel[0] > 170 && pixel[1] < 140 && pixel[2] < 140, `exported cursor pixel: ${[...pixel]}`)
    const sampleLaser = (offset, time = pointerTime) => execFileSync('ffmpeg', ['-v', 'error', '-ss', String(time), '-i', mp4, '-frames:v', '1',
      '-vf', `crop=2:2:${1248 + offset}:594,scale=1:1`, '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'])
    const largerCore = sampleLaser(10)
    assert.ok(largerCore[0] > 170 && largerCore[1] < 150, '1080p recording must have the enlarged red core')
    const whiteRing = sampleLaser(14)
    assert.ok([...whiteRing].every(value => value > 180), `white projection-style border: ${[...whiteRing]}`)
    const released = after[0].pointer.at(-1)
    assert.equal(released.x, null)
    const halo = sampleLaser(22)
    const background = sampleLaser(22, released.t + .2)
    assert.ok(halo[1] < background[1] - 15, `projection-style halo must be visible: ${[...halo]} vs ${[...background]}`)
    const releasedPixel = execFileSync('ffmpeg', ['-v', 'error', '-ss', String(released.t + .2), '-i', mp4, '-frames:v', '1',
      '-vf', 'crop=2:2:1248:594,scale=1:1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'])
    assert.ok(!(releasedPixel[0] > 170 && releasedPixel[1] < 140 && releasedPixel[2] < 140), 'exported laser must disappear after release')
    const pcm = execFileSync('ffmpeg', ['-v', 'error', '-ss', '0.3', '-i', mp4, '-t', '0.5', '-vn', '-f', 's16le', 'pipe:1'])
    assert.ok(pcm.some(value => value !== 0), 'simulated microphone tone must survive MP4 export')
    const clock = seconds => `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(Math.floor(seconds) % 60).padStart(2, '0')}`
    assert.ok((await page.$eval('.pr-recording-total', node => node.textContent)).includes(clock(after.reduce((sum, take) => sum + take.duration, 0))))
    await clickText(page, '.pr-recording button', 'Clear page recording')
    await waitFor(page, () => document.querySelectorAll('.pr-thumb-recorded.recorded').length === 1)
    const cleared = await takes(projectId)
    assert.equal(cleared.length, 1)
    assert.equal(cleared[0].take, after[1].take)
    assert.equal(cleared[0].sha256, after[1].sha256)
    assert.equal(await page.$('.pr-recording a[download="presentation.mp4"]'), null)
    assert.ok((await page.$eval('.pr-recording-total', node => node.textContent)).includes(clock(after[1].duration)))
    assert.equal(await page.$eval('[aria-label="Current page recording time"]', node => node.textContent.trim()), '00:00')
    await enterPresenter(page, projectId)
    await waitFor(page, () => document.querySelectorAll('.pr-thumb-recorded.recorded').length === 1)
    if (projectId === 'recording-typst') {
      await recordPage(page)
      const original = await takes(projectId)
      assert.ok(original.every(take => take.recording_id))
      let response = await fetch(`${baseUrl}/test/typst/order`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '[2,1]' })
      assert.equal(response.status, 200)
      const reordered = await takes(projectId)
      assert.deepEqual(reordered.map(take => take.take), original.map(take => take.take).reverse())
      assert.deepEqual(reordered.map(take => take.page), [1, 2])
      response = await fetch(`${baseUrl}/test/typst/order`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '[2]' })
      assert.equal(response.status, 200)
      const remaining = await takes(projectId)
      assert.equal(remaining.length, 1)
      assert.equal(remaining[0].take, original[0].take)
      assert.equal(remaining[0].sha256, original[0].sha256)
      assert.equal(remaining[0].page, 1)
    }
    assert.deepEqual(errors, [])
    console.log(`${projectId}: laser in video, transcript layout/font persistence, clear page, total/page/target time, source bindings, recovery, retake, H.264/AAC MP4 passed`)
    await page.close()
  }
} catch (error) {
  console.error(error)
  for (const page of await browser.pages()) {
    console.log(await page.evaluate(() => ({ text: document.querySelector('.pr-recording')?.innerText,
      recorder: window.testRecorder?.state, tracks: window.testRecorder?.stream.getTracks().map(track => ({ kind: track.kind, state: track.readyState })) })))
  }
  throw error
} finally {
  await browser.close()
  server?.kill('SIGTERM')
  await rm(temp, { recursive: true, force: true })
}
