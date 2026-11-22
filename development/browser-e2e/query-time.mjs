import { check, beginTrace, fulfillJson, logApiRequest, observePage, safeSlug, saveScreenshot } from './scenario-helpers.mjs'

const SOURCE_INSTANT = '2026-09-29T04:30:00.000Z'
const EDIT_INSTANT = '2026-09-29T05:30:00.000Z'
const UNTIL_INSTANT = '2026-09-29T06:30:00.000Z'
const UPLOAD_ID = 'upload-time-42'

// Literal values are intentional: expected local display must not be computed
// by the same browser Date logic used by the implementation under test.
const ZONES = [
  {
    id: 'UTC',
    initialFrom: '2026-09-29T04:30',
    initialUntil: '2026-09-29T06:30',
    editedFrom: '2026-09-29T05:30',
    received: '2026-09-29 04:30:00.000',
  },
  {
    // A zone with a +05:30 half-hour offset: whole-hour arithmetic would be wrong here.
    id: 'Asia/Kolkata',
    initialFrom: '2026-09-29T10:00',
    initialUntil: '2026-09-29T12:00',
    editedFrom: '2026-09-29T11:00',
    received: '2026-09-29 10:00:00.000',
  },
  {
    id: 'America/New_York',
    initialFrom: '2026-09-29T00:30',
    initialUntil: '2026-09-29T02:30',
    editedFrom: '2026-09-29T01:30',
    received: '2026-09-29 00:30:00.000',
  },
]

const pageInfo = {
  limit: 50,
  offset: 0,
  returned: 1,
  total_count: 1,
  has_prev: false,
  has_next: false,
  next_offset: null,
  prev_offset: null,
  order_by: 'completed_at',
  order_dir: 'desc',
}

function apiFixtures(pathname) {
  if (pathname === '/api/v1/auth/config') return { mode: 'none' }
  if (pathname === '/api/v1/edition') return { edition: 'core' }
  if (pathname === '/api/robots') return ['robot-a']
  if (pathname === '/api/obs/health') {
    return { status: { prometheus: { ok: true }, grafana: { ok: true } } }
  }
  if (pathname === '/api/tenant') return { scope: 'browser-e2e-fixture' }
  if (pathname === '/api/v1/uploads/list') {
    return {
      items: [{
        upload_id: UPLOAD_ID,
        robot_id: 'robot-a',
        original_filename: 'time.mcap',
        storage_path: 'raw/robot-a/time.mcap',
        file_size_bytes: 200,
        status: 'completed',
        completed_at: Date.parse(SOURCE_INSTANT) / 1000,
      }],
      page: pageInfo,
    }
  }
  return {}
}

async function runPageScenario({ browser, baseUrl, artifactsDir, report, zone, routeName }) {
  const scenario = `${routeName}-${zone.id}`
  const context = await browser.newContext({
    timezoneId: zone.id,
    viewport: { width: 1440, height: 1000 },
  })
  const stopTrace = await beginTrace(context, artifactsDir, `query-${safeSlug(scenario)}`)
  const page = await context.newPage()
  page.setDefaultTimeout(12_000)
  observePage(page, report, 'query', scenario)
  let screenshotSaved = false

  try {
    await page.route('**/api/**', async (route) => {
      const request = route.request()
      const url = new URL(request.url())
      logApiRequest(report, 'query', scenario, request)
      await fulfillJson(route, apiFixtures(url.pathname))
    })

    const endpoint = '/api/v1/uploads/list'
    const initialUrl = new URL(`/ui/${routeName}`, baseUrl)
    initialUrl.searchParams.set('since', SOURCE_INSTANT)
    initialUrl.searchParams.set('until', UNTIL_INSTANT)
    await page.goto(initialUrl.toString(), { waitUntil: 'domcontentloaded' })

    const from = page.getByLabel('From', { exact: true })
    await from.waitFor({ state: 'attached' })
    if (!(await from.isVisible())) {
      await page.getByText('Advanced filters & table view', { exact: true }).click()
    }
    await from.waitFor({ state: 'visible' })
    const initialDisplayed = await from.inputValue()
    check(
      report, 'query', scenario, 'initial UTC instant displays as the literal local wall time',
      initialDisplayed === zone.initialFrom,
      initialDisplayed,
      zone.initialFrom,
    )

    const until = await page.getByLabel('Until', { exact: true }).inputValue()
    check(report, 'query', scenario, 'until control also uses the literal local wall time',
      until === zone.initialUntil, until, zone.initialUntil)

    const row = page.getByRole('row').filter({ hasText: UPLOAD_ID })
    await row.waitFor({ state: 'visible' })
    const receivedText = (await row.locator('td').nth(3).textContent())?.trim() ?? ''
    check(report, 'query', scenario, 'received timestamp displays in the browser local zone',
      receivedText === zone.received, receivedText, zone.received)

    const editedRequest = page.waitForRequest((request) => {
      const url = new URL(request.url())
      return url.pathname === endpoint && url.searchParams.get('since') === EDIT_INSTANT
    })
    await from.fill(zone.editedFrom)
    await page.waitForFunction((instant) => new URL(window.location.href).searchParams.get('since') === instant, EDIT_INSTANT)
    const editedUrlSince = new URL(page.url()).searchParams.get('since')
    const requestAfterEdit = await editedRequest
    const requestSince = new URL(requestAfterEdit.url()).searchParams.get('since')
    check(report, 'query', scenario, 'edited local time is serialized as the expected UTC instant in the URL',
      editedUrlSince === EDIT_INSTANT, editedUrlSince, EDIT_INSTANT)
    check(report, 'query', scenario, 'API query uses that same UTC instant',
      requestSince === EDIT_INSTANT, requestSince, EDIT_INSTANT)

    const clearRequest = page.waitForRequest((request) => {
      const url = new URL(request.url())
      return url.pathname === endpoint && !url.searchParams.has('since')
    })
    await from.fill('')
    await page.waitForFunction(() => !new URL(window.location.href).searchParams.has('since'))
    const clearedUrlHasSince = new URL(page.url()).searchParams.has('since')
    const requestAfterClear = await clearRequest
    const clearedRequestHasSince = new URL(requestAfterClear.url()).searchParams.has('since')
    check(report, 'query', scenario, 'clearing the input removes since from the URL',
      !clearedUrlHasSince, clearedUrlHasSince, false)
    check(report, 'query', scenario, 'clearing the input removes since from the API query',
      !clearedRequestHasSince, clearedRequestHasSince, false)

    const screenshot = await saveScreenshot(page, artifactsDir, `query-${safeSlug(scenario)}`)
    screenshotSaved = true
    report.screenshots.push({ app: 'query', scenario, path: screenshot })
  } catch (error) {
    report.errors.push({ app: 'query', scenario, message: error instanceof Error ? `${error.name}: ${error.message}` : String(error) })
    if (!screenshotSaved) {
      try {
        const screenshot = await saveScreenshot(page, artifactsDir, `query-${safeSlug(scenario)}-failure`)
        report.screenshots.push({ app: 'query', scenario, failure: true, path: screenshot })
      } catch {}
    }
  } finally {
    try {
      report.traces.push({ app: 'query', scenario, path: await stopTrace() })
    } catch (error) {
      report.errors.push({ app: 'query', scenario, message: `trace: ${error instanceof Error ? error.message : String(error)}` })
    }
    await context.close()
  }
}

export async function runQueryTimeScenarios(options) {
  for (const zone of ZONES) {
    await runPageScenario({ ...options, zone, routeName: 'uploads' })
  }
}
