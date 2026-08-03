import type { ReactElement } from 'react'
import { render } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AuthProvider } from '../auth/AuthContext'

// Every real page below needs the same provider stack main.tsx sets up
// (router + query client + auth context) -- centralized here so a test
// doesn't have to repeat it, and a new provider added later only needs
// updating in this one place.
export function renderWithProviders(ui: ReactElement, { route = '/' } = {}) {
  // retry: false -- react-query's default retries would otherwise make
  // any error-path test wait through several backoff cycles before the
  // query settles into its error state.
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <AuthProvider>{ui}</AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
