import { check, beginTrace, fulfillJson, logApiRequest, observePage, safeSlug, saveScreenshot } from './scenario-helpers.mjs'

// The core UI build has no Pro pages. Episodes and Datasets routes must show
// the "Available in Pro" panel, and the page must never ask the server for
// Pro data. The footer must carry the AGPL-3.0 notice and the source link.

const PRO_ROUTES = [
  { path: 'episodes', heading: 'Episodes is available in Pro', apiPrefix: '/api/v1/episodes' },
  { path: 'datasets', heading: 'Datasets is available in Pro', apiPrefix: '/api/v1/datasets' },
]

function apiFixtures(pathname) {
  if (pathname === '/api/v1/auth/config') return { mode: 'none' }
  if (pathname === '/api/v1/edition') return { edition: 'core', core_version: '0.0.0-e2e' }
  if (pathname === '/api/tenant') return { scope: 'browser-e2e-fixture' }
  if (pathname === '/api/obs/health') return { status: { prometheus: null } }
  if (pathname === '/api/v1/robots/list') return { items: [] }
  if (pathname === '/api/v1/metrics/overview') {
    return { kpis: { uploads_last_hour: 0, storage_bytes_total: 0, episodes_awaiting_qa: 0, exports_completed_24h: 0 } }
  }
  return {}
}

async function visit({ browser, baseUrl, artifactsDir, report, name, run }) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  const stopTrace = await beginTrace(context, artifactsDir, `core-panels-${safeSlug(name)}`)
  const page = await context.newPage()
  page.setDefaultTimeout(12_000)
  observePage(page, report, 'query', name)
  const apiPaths = []
  try {
    await page.route('**/api/**', async (route) => {
      const request = route.request()
      apiPaths.push(new URL(request.url()).pathname)
      logApiRequest(report, 'query', name, request)
      await fulfillJson(route, apiFixtures(new URL(request.url()).pathname))
    })
    await run(page, apiPaths)
    // No page may ask for a bootstrap state: the first admin comes from the IdP.
    if (apiPaths.includes('/api/v1/bootstrap/state')) throw new Error('the app requested /api/v1/bootstrap/state, which the vault does not serve')
    const screenshot = await saveScreenshot(page, artifactsDir, `core-panels-${safeSlug(name)}`)
    report.screenshots.push({ app: 'query', scenario: name, path: screenshot })
  } catch (error) {
    report.errors.push({ app: 'query', scenario: name, message: error instanceof Error ? `${error.name}: ${error.message}` : String(error) })
  } finally {
    try {
      report.traces.push({ app: 'query', scenario: name, path: await stopTrace() })
    } catch {}
    await context.close()
  }
}

export async function runPanelScenarios({ browser, baseUrl, artifactsDir, report }) {
  const common = { browser, baseUrl, artifactsDir, report }
  for (const route of PRO_ROUTES) {
    const name = `panel-${route.path}`
    await visit({
      ...common,
      name,
      run: async (page, apiPaths) => {
        await page.goto(new URL(`/ui/${route.path}`, baseUrl).toString(), { waitUntil: 'domcontentloaded' })
        const heading = page.getByRole('heading', { name: route.heading })
        await heading.waitFor({ state: 'visible' })
        check(report, 'query', name, 'route shows the Available in Pro panel', await heading.isVisible(), true, true)
        const href = await page.getByRole('link', { name: 'Learn about Ingotron Pro' }).getAttribute('href')
        check(report, 'query', name, 'panel links to the upgrade page', href === 'https://ingotron.com/pro', href, 'https://ingotron.com/pro')
        const asked = apiPaths.filter((p) => p.startsWith(route.apiPrefix))
        check(report, 'query', name, 'no request for Pro data', asked.length === 0, asked, [])
        const footer = page.locator('.rv-shell__source')
        await footer.waitFor({ state: 'visible' })
        const text = (await footer.textContent()) ?? ''
        check(report, 'query', name, 'footer shows version and AGPL-3.0', text.includes('v0.0.0-e2e') && text.includes('AGPL-3.0'), text, 'v0.0.0-e2e … AGPL-3.0')
        const source = await footer.getByRole('link', { name: 'Source' }).getAttribute('href')
        check(report, 'query', name, 'footer source link points at the release tag', source?.endsWith('/tree/v0.0.0-e2e') ?? false, source, '…/tree/v0.0.0-e2e')
      },
    })
  }
  await visit({
    ...common,
    name: 'home',
    run: async (page, apiPaths) => {
      await page.goto(new URL('/ui/', baseUrl).toString(), { waitUntil: 'domcontentloaded' })
      await page.getByText('Available in Pro').first().waitFor({ state: 'visible' })
      const tiles = await page.getByText('Available in Pro').count()
      check(report, 'query', 'home', 'Review and Verification tiles say Available in Pro', tiles === 2, tiles, 2)
      check(report, 'query', 'home', 'Home asks for no dataset list', !apiPaths.some((p) => p.startsWith('/api/v1/datasets')), apiPaths, 'no /api/v1/datasets')
    },
  })
  await visit({
    ...common,
    name: 'help',
    run: async (page) => {
      await page.goto(new URL('/ui/help', baseUrl).toString(), { waitUntil: 'domcontentloaded' })
      await page.getByTestId('available-in-pro').first().waitFor({ state: 'visible' })
      const panels = await page.getByTestId('available-in-pro').count()
      check(report, 'query', 'help', 'Help workflow cards use the panel', panels === 2, panels, 2)
    },
  })
}
