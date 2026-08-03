import { test, expect } from '@playwright/test'

// Same real-backend requirement and env vars as login.spec.ts -- see that
// file's header comment for why credentials aren't hardcoded.
const TENANT = process.env.FLEET_TEST_TENANT ?? 'default'
const USERNAME = process.env.FLEET_TEST_USERNAME ?? 'admin'
const PASSWORD = process.env.FLEET_TEST_PASSWORD

test.skip(!PASSWORD, 'FLEET_TEST_PASSWORD not set -- no Fleet Manager to test against')

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Tenant').fill(TENANT)
  await page.getByLabel('Username').fill(USERNAME)
  await page.getByLabel('Password').fill(PASSWORD!)
  await page.getByRole('button', { name: /sign in/i }).click()
  await expect(page).toHaveURL(/\/devices$/)
})

test('shows the device list grid with its expected columns', async ({ page }) => {
  await expect(page.getByRole('columnheader', { name: 'Device ID' })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: 'Connection' })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: 'Cert Expiry' })).toBeVisible()
})

test('quick-filter search narrows the grid to zero rows for a nonsense query', async ({ page }) => {
  await page.getByRole('button', { name: 'Search' }).click()
  // No real device_id will ever match this -- deterministic regardless of
  // how many devices happen to be enrolled when this suite runs.
  await page.getByPlaceholder('Search…').fill('no-such-device-xyz-987654321')

  await expect(page.getByText('No results found.')).toBeVisible()
})
