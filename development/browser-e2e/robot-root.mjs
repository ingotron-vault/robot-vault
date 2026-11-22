import { beginTrace, check, fulfillJson, logApiRequest, observePage, saveScreenshot } from './scenario-helpers.mjs'

const ROOT_A = '/tmp/robot_data/robot-a'
const ROOT_B = '/tmp/robot_data/robot-b'
const STALE_WARNING = 'Could not reach the upload agent for the latest status. Showing the last known data.'

function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

function withTimeout(promise, label, milliseconds = 20_000) {
  let timer
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`Timed out waiting for ${label} after ${milliseconds} ms`)), milliseconds)
    }),
  ]).finally(() => clearTimeout(timer))
}

function heldResponse(route, body, held) {
  let release
  const gate = new Promise((resolve) => { release = resolve })
  const entry = {
    release,
    done: (async () => {
      await gate
      await fulfillJson(route, body)
    })(),
  }
  held.push(entry)
  return entry
}

function robotRecords() {
  return [
    { name: 'robot-a', path: ROOT_A, mcap_count: 1, uploaded_count: 0, pending_count: 1, failed_count: 0, total_size_bytes: 1024, last_modified: 1_780_000_000 },
    { name: 'robot-b', path: ROOT_B, mcap_count: 1, uploaded_count: 0, pending_count: 1, failed_count: 0, total_size_bytes: 2048, last_modified: 1_780_000_100 },
  ]
}

function statusFor(root) {
  const isA = root === ROOT_A
  return {
    root,
    pending_count: 1,
    connected: true,
    server_reachable: true,
    server_host: isA ? 'ingest-a' : 'ingest-b',
    server_port: 50051,
    network_type: 'ethernet',
    network_interface: 'eth0',
    network_ssid: null,
    is_wifi: false,
    is_cellular: false,
    is_ethernet: true,
    connection_quality: 'good',
    batch_strategy: { parallel_streams: 2, retry_multiplier: 1.5 },
    pending: [],
  }
}

function filesFor(root) {
  const isA = root === ROOT_A
  const name = isA ? 'a.mcap' : 'b.mcap'
  return [{
    path: `${root}/${name}`,
    name,
    size: 1024,
    mtime: 1_780_000_000,
    status: 'pending',
    data_file_present: true,
    safe_to_remove_local: false,
  }]
}

async function dismissFirstRunTour(page, report, scenario) {
  const skipTour = page.getByText('Skip tour', { exact: true })
  if (await skipTour.isVisible().catch(() => false)) {
    await skipTour.click()
    await skipTour.waitFor({ state: 'hidden' })
    check(report, 'robot', scenario, 'first-run tour is dismissed through its visible control', true, 'dismissed', 'dismissed')
  }
}

async function installRobotApi(page, report, scenario, behavior) {
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const pathname = url.pathname
    logApiRequest(report, 'robot', scenario, request)

    if (pathname === '/api/robots/discover') {
      return fulfillJson(route, { robots: robotRecords() })
    }
    if (pathname === '/api/upload/jobs') return fulfillJson(route, [])
    if (pathname === '/api/files') {
      return fulfillJson(route, filesFor(url.searchParams.get('root') ?? ''))
    }
    if (pathname === '/api/status') {
      const root = url.searchParams.get('root') ?? ''
      behavior.statusCounts.set(root, (behavior.statusCounts.get(root) ?? 0) + 1)
      const count = behavior.statusCounts.get(root)

      if (root === ROOT_A && count === 2 && behavior.holdARefresh) {
        const entry = heldResponse(route, statusFor(ROOT_A), behavior.held)
        behavior.holdARefresh.resolve(entry)
        return entry.done
      }
      if (root === ROOT_B && count === 1 && behavior.holdBFirst) {
        const entry = heldResponse(route, statusFor(ROOT_B), behavior.held)
        behavior.holdBFirst.resolve(entry)
        return entry.done
      }
      if (root === behavior.failRoot && count > 1) {
        behavior.failedRefresh.resolve({ root, count })
        return fulfillJson(route, { detail: 'synthetic refresh failure' }, 503)
      }
      return fulfillJson(route, statusFor(root))
    }
    return fulfillJson(route, {})
  })
}

async function runRootSwitch({ browser, baseUrl, artifactsDir, report }) {
  const scenario = 'held-status-root-switch'
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  const stopTrace = await beginTrace(context, artifactsDir, 'robot-held-status-root-switch')
  const page = await context.newPage()
  page.setDefaultTimeout(18_000)
  observePage(page, report, 'robot', scenario)
  let screenshotSaved = false

  const held = []
  const behavior = {
    held,
    statusCounts: new Map(),
    holdARefresh: deferred(),
    holdBFirst: deferred(),
    failedRefresh: deferred(),
    failRoot: null,
  }

  try {
    await installRobotApi(page, report, scenario, behavior)
    await page.goto(baseUrl, { waitUntil: 'domcontentloaded' })
    await dismissFirstRunTour(page, report, scenario)
    const robotPicker = page.locator('#robot-status select')
    await robotPicker.selectOption('robot-a')
    await page.getByText('Server reachable (ingest-a:50051)', { exact: true }).waitFor()

    await page.getByRole('link', { name: 'Files', exact: true }).click()
    await page.getByText('a.mcap', { exact: true }).waitFor()
    await page.getByRole('checkbox', { name: 'Select a.mcap' }).check()
    const uploadA = page.getByRole('button', { name: 'Upload selected (1)', exact: true })
    await uploadA.waitFor({ state: 'visible' })
    check(report, 'robot', scenario, 'Robot A upload is available with Robot A status',
      await uploadA.isEnabled(), await uploadA.isEnabled(), true)

    const heldA = await withTimeout(behavior.holdARefresh.promise, 'Robot A refresh request')
    check(report, 'robot', scenario, 'a later Robot A status request is held in flight',
      Boolean(heldA), behavior.statusCounts.get(ROOT_A), 'first status plus held refresh')

    await robotPicker.selectOption('robot-b')
    const heldB = await withTimeout(behavior.holdBFirst.promise, 'Robot B initial status request')
    await page.getByText('b.mcap', { exact: true }).waitFor()
    const oldStatusCount = await page.getByText('Server reachable (ingest-a:50051)', { exact: true }).count()
    check(report, 'robot', scenario, 'Robot A status is removed while Robot B status is pending',
      oldStatusCount === 0, oldStatusCount, 0)

    await page.getByRole('checkbox', { name: 'Select b.mcap' }).check()
    const uploadB = page.getByRole('button', { name: 'Upload selected (1)', exact: true })
    const disabledPending = await uploadB.isDisabled()
    check(report, 'robot', scenario, 'upload remains disabled until Robot B status arrives',
      disabledPending, disabledPending, true)

    const pendingScreenshot = await saveScreenshot(page, artifactsDir, 'robot-held-status-root-switch-pending-b')
    report.screenshots.push({ app: 'robot', scenario, state: 'robot-b-pending', path: pendingScreenshot })

    heldB.release()
    await withTimeout(heldB.done, 'Robot B status response')
    await page.getByText('Server reachable (ingest-b:50051)', { exact: true }).waitFor()
    const enabledForB = await uploadB.isEnabled()
    check(report, 'robot', scenario, 'Robot B upload becomes available after Robot B status succeeds',
      enabledForB, enabledForB, true)

    // Let the request that began under A finish after the switch. It must only
    // update A's old query key and cannot replace the currently selected B.
    heldA.release()
    await heldA.done
    await page.waitForTimeout(100)
    const stillB = await page.getByText('Server reachable (ingest-b:50051)', { exact: true }).count()
    const aAfterLateReply = await page.getByText('Server reachable (ingest-a:50051)', { exact: true }).count()
    check(report, 'robot', scenario, 'late Robot A reply cannot overwrite Robot B status',
      stillB === 1 && aAfterLateReply === 0,
      { robotB: stillB, robotA: aAfterLateReply },
      { robotB: 1, robotA: 0 })

    const screenshot = await saveScreenshot(page, artifactsDir, 'robot-held-status-root-switch')
    screenshotSaved = true
    report.screenshots.push({ app: 'robot', scenario, path: screenshot })
  } catch (error) {
    report.errors.push({ app: 'robot', scenario, message: error instanceof Error ? `${error.name}: ${error.message}` : String(error) })
    if (!screenshotSaved) {
      try {
        const screenshot = await saveScreenshot(page, artifactsDir, 'robot-held-status-root-switch-failure')
        report.screenshots.push({ app: 'robot', scenario, failure: true, path: screenshot })
      } catch {}
    }
  } finally {
    for (const entry of held) entry.release()
    await Promise.allSettled(held.map((entry) => entry.done))
    try {
      report.traces.push({ app: 'robot', scenario, path: await stopTrace() })
    } catch (error) {
      report.errors.push({ app: 'robot', scenario, message: `trace: ${error instanceof Error ? error.message : String(error)}` })
    }
    await context.close()
  }
}

async function runSameRootFailure({ browser, baseUrl, artifactsDir, report }) {
  const scenario = 'same-root-failed-refresh-retains-stale-status'
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  const stopTrace = await beginTrace(context, artifactsDir, 'robot-same-root-stale-status')
  const page = await context.newPage()
  page.setDefaultTimeout(22_000)
  observePage(page, report, 'robot', scenario)
  let screenshotSaved = false

  const behavior = {
    held: [],
    statusCounts: new Map(),
    holdARefresh: null,
    holdBFirst: null,
    failedRefresh: deferred(),
    failRoot: ROOT_A,
  }

  try {
    await installRobotApi(page, report, scenario, behavior)
    await page.goto(baseUrl, { waitUntil: 'domcontentloaded' })
    await dismissFirstRunTour(page, report, scenario)
    await page.locator('#robot-status select').selectOption('robot-a')
    const lastKnownStatus = page.getByText('Server reachable (ingest-a:50051)', { exact: true })
    await lastKnownStatus.waitFor()

    const failure = await withTimeout(behavior.failedRefresh.promise, 'same-root failed refresh')
    check(report, 'robot', scenario, 'same-root refresh request was deliberately rejected',
      failure.root === ROOT_A && failure.count > 1, failure, 'Robot A refresh after initial status')
    const staleBanner = page.getByText(STALE_WARNING, { exact: true })
    await staleBanner.waitFor()
    const retained = await lastKnownStatus.count()
    check(report, 'robot', scenario, 'last successful Robot A status remains visible with stale warning',
      retained === 1, { retained, warning: await staleBanner.textContent() },
      { retained: 1, warning: STALE_WARNING })

    const screenshot = await saveScreenshot(page, artifactsDir, 'robot-same-root-stale-status')
    screenshotSaved = true
    report.screenshots.push({ app: 'robot', scenario, path: screenshot })
  } catch (error) {
    report.errors.push({ app: 'robot', scenario, message: error instanceof Error ? `${error.name}: ${error.message}` : String(error) })
    if (!screenshotSaved) {
      try {
        const screenshot = await saveScreenshot(page, artifactsDir, 'robot-same-root-stale-status-failure')
        report.screenshots.push({ app: 'robot', scenario, failure: true, path: screenshot })
      } catch {}
    }
  } finally {
    try {
      report.traces.push({ app: 'robot', scenario, path: await stopTrace() })
    } catch (error) {
      report.errors.push({ app: 'robot', scenario, message: `trace: ${error instanceof Error ? error.message : String(error)}` })
    }
    await context.close()
  }
}

export async function runRobotRootScenarios(options) {
  await runRootSwitch(options)
  await runSameRootFailure(options)
}
