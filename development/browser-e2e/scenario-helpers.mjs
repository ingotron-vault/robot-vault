import { mkdir } from 'node:fs/promises'
import path from 'node:path'

export function check(report, app, scenario, label, pass, actual, expected) {
  report.checks.push({ app, scenario, label, pass: Boolean(pass), actual, expected })
}

export function observePage(page, report, app, scenario) {
  page.on('pageerror', (error) => {
    report.pageErrors.push({ app, scenario, message: error.message })
  })
  page.on('console', (message) => {
    if (message.type() === 'error') {
      report.consoleErrors.push({ app, scenario, message: message.text() })
    }
  })
  page.on('requestfailed', (request) => {
    report.failedRequests.push({
      app,
      scenario,
      method: request.method(),
      url: request.url(),
      error: request.failure()?.errorText ?? 'request failed',
    })
  })
}

export async function beginTrace(context, artifactsDir, name) {
  await mkdir(artifactsDir, { recursive: true })
  await context.tracing.start({ screenshots: true, snapshots: true, sources: true })
  return async () => {
    const trace = path.join(artifactsDir, `${name}.trace.zip`)
    await context.tracing.stop({ path: trace })
    return trace
  }
}

export async function saveScreenshot(page, artifactsDir, name) {
  await mkdir(artifactsDir, { recursive: true })
  const screenshot = path.join(artifactsDir, `${name}.png`)
  await page.screenshot({ path: screenshot, fullPage: true, animations: 'disabled' })
  return screenshot
}

export async function fulfillJson(route, body, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  })
}

export function logApiRequest(report, app, scenario, request) {
  report.apiRequests.push({
    app,
    scenario,
    method: request.method(),
    url: request.url(),
    body: request.postData(),
  })
}

export function safeSlug(value) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, '-')
}
