import { test, expect } from '@playwright/test'

// Same real-backend requirement and env vars as login.spec.ts.
const TENANT = process.env.FLEET_TEST_TENANT ?? 'default'
const USERNAME = process.env.FLEET_TEST_USERNAME ?? 'admin'
const PASSWORD = process.env.FLEET_TEST_PASSWORD

test.skip(!PASSWORD, 'FLEET_TEST_PASSWORD not set -- no Fleet Manager to test against')

test('issues a join token, shows it once, then revokes it', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Tenant').fill(TENANT)
  await page.getByLabel('Username').fill(USERNAME)
  await page.getByLabel('Password').fill(PASSWORD!)
  await page.getByRole('button', { name: /sign in/i }).click()
  await expect(page).toHaveURL(/\/devices$/)

  await page.getByRole('link', { name: 'Join Tokens' }).click()
  await expect(page).toHaveURL(/\/join-tokens$/)

  const deviceId = `e2e-test-device-${Date.now()}`
  await page.getByLabel(/^Device ID/).fill(deviceId)
  await page.getByRole('button', { name: /issue token/i }).click()

  await expect(page.getByText('Join token issued')).toBeVisible()
  const rawToken = await page.locator('div[role="dialog"] input').inputValue()
  expect(rawToken.length).toBeGreaterThan(0)
  await page.getByRole('button', { name: /^done$/i }).click()

  const row = page.getByRole('row').filter({ hasText: deviceId })
  await expect(row.getByText('active')).toBeVisible()

  await row.getByRole('button', { name: /revoke/i }).click()
  await expect(row.getByText('revoked')).toBeVisible()
  await expect(row.getByRole('button', { name: /revoke/i })).not.toBeVisible()
})
