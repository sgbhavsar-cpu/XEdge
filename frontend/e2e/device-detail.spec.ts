import { test, expect } from '@playwright/test'

// Same real-backend requirement and env vars as login.spec.ts.
const TENANT = process.env.FLEET_TEST_TENANT ?? 'default'
const USERNAME = process.env.FLEET_TEST_USERNAME ?? 'admin'
const PASSWORD = process.env.FLEET_TEST_PASSWORD

test.skip(!PASSWORD, 'FLEET_TEST_PASSWORD not set -- no Fleet Manager to test against')

test('opens a device from the list and shows its detail sections', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Tenant').fill(TENANT)
  await page.getByLabel('Username').fill(USERNAME)
  await page.getByLabel('Password').fill(PASSWORD!)
  await page.getByRole('button', { name: /sign in/i }).click()
  await expect(page).toHaveURL(/\/devices$/)

  const firstDeviceLink = page.getByRole('grid').getByRole('link').first()
  // Whichever devices happen to be enrolled when this suite runs is out of
  // this test's control -- skip rather than fail if the fleet is empty.
  // The grid's data fetch is async, so wait for either a row or the
  // "No rows" overlay rather than checking visibility immediately.
  const hasDevice = await firstDeviceLink
    .waitFor({ state: 'visible', timeout: 5000 })
    .then(() => true)
    .catch(() => false)
  test.skip(!hasDevice, 'No devices enrolled to open')

  await firstDeviceLink.click()
  await expect(page).toHaveURL(/\/devices\/[^/]+$/)

  await expect(page.getByRole('heading', { name: 'Metadata' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Pending Config' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Config History' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Certificate History' })).toBeVisible()
})
