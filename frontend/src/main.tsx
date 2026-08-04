import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import CssBaseline from '@mui/material/CssBaseline'
import { ThemeProvider, createTheme } from '@mui/material/styles'
import './index.css'
import App from './App.tsx'
import { AuthProvider } from './auth/AuthContext'
import { ApiError } from './api/client'

const theme = createTheme()
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // A 4xx (permission denied, not found, ...) won't succeed on
      // retry -- react-query's default of retrying everything 3x means
      // an operator without permission for a page waits through several
      // seconds of exponential backoff before seeing why it failed.
      retry: (failureCount, error) =>
        error instanceof ApiError && error.status >= 400 && error.status < 500 ? false : failureCount < 3,
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <AuthProvider>
            <App />
          </AuthProvider>
        </BrowserRouter>
      </QueryClientProvider>
    </ThemeProvider>
  </StrictMode>,
)
