import { test, expect } from '@playwright/test'

// Same real-backend requirement and env vars as login.spec.ts.
const TENANT = process.env.FLEET_TEST_TENANT ?? 'default'
const USERNAME = process.env.FLEET_TEST_USERNAME ?? 'admin'
const PASSWORD = process.env.FLEET_TEST_PASSWORD

test.skip(!PASSWORD, 'FLEET_TEST_PASSWORD not set -- no Fleet Manager to test against')

async function login(page: import('@playwright/test').Page, username: string, password: string) {
  await page.goto('/login')
  await page.getByLabel('Tenant').fill(TENANT)
  await page.getByLabel('Username').fill(username)
  await page.getByLabel('Password').fill(password)
  await page.getByRole('button', { name: /sign in/i }).click()
}

test('creates a readonly user who cannot see management nav links, then cleans it up', async ({ page }) => {
  const testUsername = `e2e-readonly-${Date.now()}`
  const testPassword = 'e2e-test-password-123'

  await login(page, USERNAME, PASSWORD!)
  await expect(page).toHaveURL(/\/devices$/)

  await page.getByRole('link', { name: 'Users' }).click()
  await expect(page).toHaveURL(/\/users$/)

  await page.getByLabel(/^Username/).fill(testUsername)
  await page.getByLabel(/^Password/).fill(testPassword)
  await page.getByLabel('Role').click()
  await page.getByRole('option', { name: 'readonly' }).click()
  await page.getByRole('button', { name: /^create$/i }).click()

  const newRow = page.getByRole('row').filter({ hasText: testUsername })
  await expect(newRow).toBeVisible()

  await page.getByRole('button', { name: /sign out/i }).click()
  await expect(page).toHaveURL(/\/login$/)

  await login(page, testUsername, testPassword)
  await expect(page).toHaveURL(/\/devices$/)
  await expect(page.getByRole('link', { name: 'Join Tokens' })).not.toBeVisible()
  await expect(page.getByRole('link', { name: 'Users' })).not.toBeVisible()

  await page.getByRole('button', { name: /sign out/i }).click()
  await expect(page).toHaveURL(/\/login$/)

  // Cleanup -- delete the test account so repeated runs don't accumulate.
  await login(page, USERNAME, PASSWORD!)
  await page.getByRole('link', { name: 'Users' }).click()
  const row = page.getByRole('row').filter({ hasText: testUsername })
  await row.getByRole('button', { name: /^delete$/i }).click()
  await expect(row).not.toBeVisible()
})
