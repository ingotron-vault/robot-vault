import { createWriteStream, existsSync } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import net from 'node:net'
import path from 'node:path'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const packageDir = path.dirname(fileURLToPath(import.meta.url))
const repositoryRoot = path.resolve(packageDir, '../..')
const sourceRoot = path.resolve(process.env.BROWSER_SOURCE_ROOT || repositoryRoot)
const artifactsDir = path.resolve(process.env.BROWSER_ARTIFACT_DIR || path.join(repositoryRoot, 'tmp/browser-e2e'))

const APP_SOURCES = {
  query: path.join(sourceRoot, 'phases/05-query-service/src/ui-spa'),
  robot: path.join(sourceRoot, 'phases/02-robot-upload-agent/src/ui-spa'),
}

// Each app's scenario modules, imported only when the app is selected.
const APP_SCENARIOS = {
  query: [['./query-time.mjs', 'runQueryTimeScenarios'], ['./panels.mjs', 'runPanelScenarios']],
  robot: [['./robot-root.mjs', 'runRobotRootScenarios']],
}

// Optional local apps: a sibling extra-apps.mjs may export `sources(sourceRoot)`
// and `scenarios` in the shapes above. This repository ships no such file.
const extraAppsFile = path.join(packageDir, 'extra-apps.mjs')
if (existsSync(extraAppsFile)) {
  const extra = await import('./extra-apps.mjs')
  Object.assign(APP_SOURCES, extra.sources(sourceRoot))
  Object.assign(APP_SCENARIOS, extra.scenarios)
}

// BROWSER_APPS: comma-separated app names; the default and the core gate pass
// query,robot. Unknown or empty names fail the run.
function selectedApps() {
  const names = (process.env.BROWSER_APPS ?? 'query,robot').split(',').map((name) => name.trim()).filter(Boolean)
  const unknown = names.filter((name) => !(name in APP_SOURCES))
  if (names.length === 0 || unknown.length > 0) {
    throw new Error(`BROWSER_APPS must name apps from ${Object.keys(APP_SOURCES).join(', ')}; got ${JSON.stringify(process.env.BROWSER_APPS)}`)
  }
  return [...new Set(names)]
}

const report = {
  startedAt: new Date().toISOString(),
  repositoryRoot,
  sourceRoot,
  artifactsDir,
  browser: null,
  servers: [],
  checks: [],
  apiRequests: [],
  pageErrors: [],
  consoleErrors: [],
  failedRequests: [],
  errors: [],
  screenshots: [],
  traces: [],
  apps: [],
  passed: false,
}

const ownedServers = []

async function freePort() {
  const server = net.createServer()
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  const { port } = server.address()
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()))
  return port
}

function waitForExit(child) {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve()
  return new Promise((resolve) => child.once('exit', resolve))
}

async function stopOwnedServer(server) {
  const { child, output } = server
  if (child.exitCode === null && child.signalCode === null) {
    child.kill('SIGTERM')
    let timer
    await Promise.race([
      waitForExit(child),
      new Promise((resolve) => { timer = setTimeout(resolve, 5_000) }),
    ])
    clearTimeout(timer)
    if (child.exitCode === null && child.signalCode === null) {
      child.kill('SIGKILL')
      await waitForExit(child)
    }
  }
  output.end()
}

async function startAppServer(name, root) {
  const requireFromApp = createRequire(path.join(root, 'package.json'))
  const vitePackagePath = requireFromApp.resolve('vite/package.json')
  const vitePackage = JSON.parse(await readFile(vitePackagePath, 'utf8'))
  const viteBin = typeof vitePackage.bin === 'string' ? vitePackage.bin : vitePackage.bin?.vite
  if (!viteBin) throw new Error(`Could not find the Vite CLI entry in ${vitePackagePath}`)
  const viteEntry = path.resolve(path.dirname(vitePackagePath), viteBin)
  const port = await freePort()
  const logPath = path.join(artifactsDir, `vite-${name}.log`)
  const output = createWriteStream(logPath, { flags: 'w' })

  const child = spawn(process.execPath, [viteEntry, '--configLoader', 'runner', '--host', '127.0.0.1', '--port', String(port), '--strictPort'], {
    cwd: root,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: process.env,
  })
  child.stdout.pipe(output, { end: false })
  child.stderr.pipe(output, { end: false })
  let spawnError = null
  child.on('error', (error) => { spawnError = error })
  const owned = { name, root, port, url: `http://127.0.0.1:${port}`, logPath, child, output }
  ownedServers.push(owned)
  report.servers.push({ app: name, root, port, url: owned.url, log: logPath })

  const exited = new Promise((resolve) => child.once('exit', (code, signal) => resolve({ code, signal })))
  const deadline = Date.now() + 45_000
  let lastFailure = 'server has not responded yet'
  while (Date.now() < deadline) {
    if (spawnError) throw new Error(`${name} Vite could not start: ${spawnError.message}`)
    if (child.exitCode !== null || child.signalCode !== null) {
      const exit = await exited
      throw new Error(`${name} Vite exited before readiness (${JSON.stringify(exit)}); see ${logPath}`)
    }
    try {
      const response = await fetch(owned.url, { signal: AbortSignal.timeout(1_500) })
      if (response.ok) return owned
      lastFailure = `HTTP ${response.status}`
    } catch (error) {
      lastFailure = error instanceof Error ? error.message : String(error)
    }
    await new Promise((resolve) => setTimeout(resolve, 200))
  }
  throw new Error(`${name} Vite did not become ready: ${lastFailure}; see ${logPath}`)
}

async function captureScenarioError(label, run) {
  try {
    await run()
  } catch (error) {
    report.errors.push({ app: label, message: error instanceof Error ? `${error.name}: ${error.message}` : String(error) })
  }
}

async function main() {
  await mkdir(artifactsDir, { recursive: true })
  report.apps = selectedApps()
  const appServers = {}
  for (const name of report.apps) {
    appServers[name] = await startAppServer(name, APP_SOURCES[name])
  }

  const launchOptions = { headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] }
  if (process.env.BROWSER_EXECUTABLE) launchOptions.executablePath = process.env.BROWSER_EXECUTABLE
  const browser = await chromium.launch(launchOptions)
  report.browser = await browser.version()

  try {
    for (const app of report.apps) {
      for (const [modulePath, exportName] of APP_SCENARIOS[app]) {
        await captureScenarioError(app, async () => {
          const scenarios = await import(modulePath)
          await scenarios[exportName]({
            browser,
            baseUrl: appServers[app].url,
            artifactsDir,
            report,
          })
        })
      }
    }
  } finally {
    await browser.close()
  }
}

try {
  await main()
} catch (error) {
  report.errors.push({ app: 'runner', message: error instanceof Error ? `${error.name}: ${error.message}` : String(error) })
} finally {
  for (const server of [...ownedServers].reverse()) {
    try {
      await stopOwnedServer(server)
    } catch (error) {
      report.errors.push({ app: server.name, message: `could not stop Vite: ${error instanceof Error ? error.message : String(error)}` })
    }
  }

  report.finishedAt = new Date().toISOString()
  report.failedChecks = report.checks.filter(({ pass }) => !pass).length
  // Every selected app must have run checks: a scenario that never ran is not a pass.
  report.checksPerApp = Object.fromEntries(report.apps.map((app) => [app, report.checks.filter((entry) => entry.app === app).length]))
  const everyAppChecked = report.apps.length > 0 && report.apps.every((app) => report.checksPerApp[app] > 0)
  report.passed = everyAppChecked && report.failedChecks === 0 && report.errors.length === 0 && report.pageErrors.length === 0
  const reportPath = path.join(artifactsDir, 'report.json')
  try {
    await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`)
  } catch (error) {
    report.passed = false
    report.errors.push({ app: 'runner', message: `could not write report: ${error instanceof Error ? error.message : String(error)}` })
  }

  const summary = {
    passed: report.passed,
    apps: report.apps,
    checks: report.checks.length,
    checksPerApp: report.checksPerApp,
    failedChecks: report.failedChecks,
    errors: report.errors,
    pageErrors: report.pageErrors,
    report: reportPath,
  }
  process.stdout.write(`${JSON.stringify(summary, null, 2)}\n`)
  if (!report.passed) process.exitCode = 1
}
