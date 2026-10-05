// Projection and recorded frames share a 720p reference appearance.
const laser = { size: 22, border: 3, halo: 6, color: '#e32636',
  white: 'rgba(255,255,255,.96)', glow: 'rgba(227,38,54,.3)', shadow: 'rgba(0,0,0,.8)' }

export const projectionLaserStyle = {
  width: laser.size, height: laser.size, background: laser.color,
  borderWidth: laser.border, borderColor: laser.white,
  boxShadow: `0 0 0 ${laser.halo}px ${laser.glow}, 0 2px 12px ${laser.shadow}`,
}

export function paintPresentationLaser(context, x, y, scale = 1) {
  context.save()
  context.translate(x, y)
  context.scale(scale, scale)
  const circle = (radius, color) => {
    context.beginPath()
    context.arc(0, 0, radius, 0, Math.PI * 2)
    context.fillStyle = color
    context.fill()
  }
  context.shadowColor = laser.shadow
  context.shadowOffsetY = 2 * scale
  context.shadowBlur = 12 * scale
  circle(laser.size / 2, laser.white)
  context.shadowColor = 'transparent'
  circle(laser.size / 2 + laser.halo, laser.glow)
  circle(laser.size / 2, laser.white)
  circle(laser.size / 2 - laser.border, laser.color)
  context.restore()
}
