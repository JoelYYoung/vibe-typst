import { containRect } from './presentationPointer.js'
import { paintPresentationLaser } from './presentationLaser.js'

export function supportedRecordingType(Recorder = globalThis.MediaRecorder) {
  if (!Recorder) return null
  return ['video/webm;codecs=vp8,opus', 'video/webm;codecs=vp9,opus', 'video/mp4', 'video/webm']
    .find((type) => Recorder.isTypeSupported(type)) || null
}

export function recordingPageState(take, name, token) {
  if (!take) return 'missing'
  return take.name === name && take.token === token ? 'recorded' : 'stale'
}

export function formatRecordingTime(seconds = 0) {
  const rounded = Math.max(0, Math.floor(seconds))
  return `${String(Math.floor(rounded / 60)).padStart(2, '0')}:${String(rounded % 60).padStart(2, '0')}`
}

// Capture only a frozen slide and its normalized pointer, never presenter controls or notes.
// The immutable slide bytes are hashed so recompilation during microphone permission cannot
// silently record an image different from the one the user started on.
export async function startSlideRecording({ url, name, token, recording_id, signal, onInterrupted }) {
  const mime = supportedRecordingType()
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    throw new Error('Microphone recording requires HTTPS or localhost.')
  }
  if (!mime || !HTMLCanvasElement.prototype.captureStream) {
    throw new Error('This browser cannot record slides. Use a current Chrome, Edge, Firefox, or Safari.')
  }
  let microphone, videoStream, imageUrl, timer, limitTimer, recorder, audioContext, audioSource, analyser, audioSamples
  const release = () => {
    clearInterval(timer)
    clearTimeout(limitTimer)
    microphone?.getTracks().forEach((track) => track.stop())
    videoStream?.getTracks().forEach((track) => track.stop())
    audioSource?.disconnect()
    analyser?.disconnect()
    if (audioContext && audioContext.state !== 'closed') audioContext.close().catch(() => {})
    if (imageUrl) URL.revokeObjectURL(imageUrl)
  }
  try {
    microphone = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true }, video: false })
    signal?.throwIfAborted()
    const AudioContext = window.AudioContext || window.webkitAudioContext
    if (AudioContext) {
      try {
        audioContext = new AudioContext()
        analyser = audioContext.createAnalyser()
        analyser.fftSize = 2048
        audioSamples = new Float32Array(analyser.fftSize)
        audioSource = audioContext.createMediaStreamSource(microphone)
        audioSource.connect(analyser)
        // Analyse the recorded input without sending it to the speakers.
        await audioContext.resume()
        signal?.throwIfAborted()
      } catch (error) {
        if (error.name === 'AbortError') throw error
        audioSource?.disconnect()
        analyser = null
        if (audioContext && audioContext.state !== 'closed') audioContext.close().catch(() => {})
      }
    }
    const response = await fetch(url, { signal })
    if (!response.ok) throw new Error('Could not load this slide for recording.')
    const imageBlob = await response.blob()
    const digest = await crypto.subtle.digest('SHA-1', await imageBlob.arrayBuffer())
    const actualToken = Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, '0')).join('').slice(0, 12)
    if (actualToken !== token) throw new Error('Slide content changed. Wait for the preview to update, then start again.')
    const image = new Image()
    imageUrl = URL.createObjectURL(imageBlob)
    image.src = imageUrl
    await image.decode()
    signal?.throwIfAborted()
    const canvas = document.createElement('canvas')
    canvas.width = 1920
    canvas.height = 1080
    const context = canvas.getContext('2d')
    const rect = containRect(canvas.width, canvas.height, image.naturalWidth, image.naturalHeight)
    let pointer = null
    const samples = []
    const paint = () => {
      context.fillStyle = '#000'
      context.fillRect(0, 0, canvas.width, canvas.height)
      context.drawImage(image, rect.left, rect.top, rect.width, rect.height)
      if (pointer) {
        const x = rect.left + pointer.x * rect.width
        const y = rect.top + pointer.y * rect.height
        paintPresentationLaser(context, x, y, canvas.height / 720)
      }
    }
    paint()
    videoStream = canvas.captureStream(30)
    const stream = new MediaStream([...videoStream.getVideoTracks(), ...microphone.getAudioTracks()])
    recorder = new MediaRecorder(stream, { mimeType: mime, videoBitsPerSecond: 6000000, audioBitsPerSecond: 192000 })
    const chunks = []
    let bytes = 0
    let failure = null
    let stoppedAt = null
    const startedAt = performance.now()
    const time = () => (performance.now() - startedAt) / 1000
    let resolveDone, rejectDone
    const done = new Promise((resolve, reject) => { resolveDone = resolve; rejectDone = reject })
    // Install a rejection handler immediately; callers await done when stopping.
    done.catch(() => {})
    recorder.ondataavailable = (event) => {
      if (event.data.size) { chunks.push(event.data); bytes += event.data.size }
      if (bytes > 500 * 1024 * 1024 && recorder.state === 'recording') {
        onInterrupted('This page reached the recording size limit and was stopped.')
      }
    }
    recorder.onerror = (event) => {
      failure = event.error || new Error('The browser could not finish recording. The previous take is preserved.')
      onInterrupted(failure.message)
    }
    recorder.onstop = () => {
      if (stoppedAt === null) onInterrupted('The browser stopped recording. Review this take before exporting.')
      const duration = stoppedAt ?? time()
      release()
      if (failure) rejectDone(failure)
      else if (!chunks.length || duration < 0.1) rejectDone(new Error('The recording was too short. Please record again.'))
      else resolveDone({ video: new Blob(chunks, { type: recorder.mimeType }),
        metadata: { name, token, recording_id, duration, mime: recorder.mimeType, pointer: samples } })
    }
    recorder.start(1000)
    timer = setInterval(paint, 1000 / 30)
    limitTimer = setTimeout(() => onInterrupted('This page reached the two-hour limit and was stopped.'), 7200 * 1000)
    microphone.getAudioTracks().forEach((track) => {
      track.onended = () => onInterrupted('The microphone disconnected. Recording was stopped.')
    })
    const stop = () => {
      if (recorder.state !== 'inactive') {
        stoppedAt = time()
        clearInterval(timer)
        clearTimeout(limitTimer)
        recorder.stop()
      }
      return done
    }
    return {
      stop,
      elapsed: time,
      audioLevel() {
        if (!analyser || audioContext.state !== 'running') return null
        analyser.getFloatTimeDomainData(audioSamples)
        let energy = 0
        for (const sample of audioSamples) energy += sample * sample
        return Math.max(-60, Math.min(0, 10 * Math.log10(energy / audioSamples.length || 1e-6)))
      },
      setPointer(point) {
        if (recorder.state !== 'recording') return
        pointer = point ? { x: point.x, y: point.y } : null
        const t = time()
        const sample = { t: Math.round(t * 1000) / 1000,
          x: pointer ? Math.round(pointer.x * 10000) / 10000 : null,
          y: pointer ? Math.round(pointer.y * 10000) / 10000 : null }
        const last = samples[samples.length - 1]
        if (last && last.x === sample.x && last.y === sample.y) return
        // Keep the final position of every frame bucket. Dropping fast events outright
        // loses the last mouse position when the user stops moving between frames.
        if (last && Math.floor(last.t * 30) === Math.floor(sample.t * 30)) samples[samples.length - 1] = sample
        else samples.push(sample)
      },
    }
  } catch (error) {
    if (recorder?.state === 'recording') recorder.stop()
    release()
    if (error.name === 'NotAllowedError') throw new Error('Microphone permission was denied. Allow microphone access in your browser, then retry.')
    if (error.name === 'NotFoundError') throw new Error('No microphone was found. Connect a microphone and retry.')
    throw error
  }
}
