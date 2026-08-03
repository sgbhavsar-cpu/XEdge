import { test, expect } from '@playwright/test'

// Requires a real xedge-fleet-manager running against a real Postgres
// (see playwright.config.ts) -- no mocked fetch, per this sprint's
// testing decision. Credentials come from env vars rather than a
// hardcoded bootstrap password, since that password is randomly
// generated fresh on every first startup (xedge/fleet/manager_cli.py):
//   FLEET_TEST_TENANT (default: "default")
//   FLEET_TEST_USERNAME (default: "admin")
//   FLEET_TEST_PASSWORD (required)
const TENANT = process.env.FLEET_TEST_TENANT ?? 'default'
const USERNAME = process.env.FLEET_TEST_USERNAME ?? 'admin'
const PASSWORD = process.env.FLEET_TEST_PASSWORD

test.skip(!PASSWORD, 'FLEET_TEST_PASSWORD not set -- no Fleet Manager to test against')

test('signs in with valid credentials and shows the dashboard', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Tenant').fill(TENANT)
  await page.getByLabel('Username').fill(USERNAME)
  await page.getByLabel('Password').fill(PASSWORD!)
  await page.getByRole('button', { name: /sign in/i }).click()

  await expect(page.getByText(`${USERNAME} (`)).toBeVisible()
  await expect(page.getByRole('button', { name: /sign out/i })).toBeVisible()
})

test('shows an error for the wrong password without crashing', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Tenant').fill(TENANT)
  await page.getByLabel('Username').fill(USERNAME)
  await page.getByLabel('Password').fill('definitely-wrong')
  await page.getByRole('button', { name: /sign in/i }).click()

  await expect(page.getByText('Invalid credentials')).toBeVisible()
})

test('signing out returns to the login page and clears the session', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Tenant').fill(TENANT)
  await page.getByLabel('Username').fill(USERNAME)
  await page.getByLabel('Password').fill(PASSWORD!)
  await page.getByRole('button', { name: /sign in/i }).click()
  await expect(page.getByRole('button', { name: /sign out/i })).toBeVisible()

  await page.getByRole('button', { name: /sign out/i }).click()

  await expect(page).toHaveURL(/\/login$/)
})

test('reloading after login forces a fresh sign-in (in-memory token only)', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Tenant').fill(TENANT)
  await page.getByLabel('Username').fill(USERNAME)
  await page.getByLabel('Password').fill(PASSWORD!)
  await page.getByRole('button', { name: /sign in/i }).click()
  await expect(page.getByRole('button', { name: /sign out/i })).toBeVisible()

  await page.reload()

  await expect(page).toHaveURL(/\/login$/)
})
