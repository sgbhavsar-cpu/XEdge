import { Link as RouterLink, Outlet } from 'react-router-dom'
import AppBar from '@mui/material/AppBar'
import Toolbar from '@mui/material/Toolbar'
import Typography from '@mui/material/Typography'
import Button from '@mui/material/Button'
import Box from '@mui/material/Box'
import Container from '@mui/material/Container'
import { useAuth } from '../auth/AuthContext'
import { hasPermission } from '../auth/permissions'

// More nav links are added here as later PRs introduce the pages they
// point to (user management XEDGE-515). Hiding a link a role lacks
// permission for is a UX nicety only; every route it would point to
// enforces its own permission server-side regardless (same
// defense-in-depth split the device-local Web UI's nav_permissions()
// already uses).
export default function Layout() {
  const { session, logout } = useAuth()

  return (
    <>
      <AppBar position="static">
        <Toolbar>
          <Typography variant="h6" component="div" sx={{ flexGrow: 1 }}>
            xEdge Fleet Manager
          </Typography>
          {session !== null && (
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
              {hasPermission(session.role, 'device:read') && (
                <Button color="inherit" component={RouterLink} to="/devices">
                  Devices
                </Button>
              )}
              {hasPermission(session.role, 'device:write') && (
                <Button color="inherit" component={RouterLink} to="/join-tokens">
                  Join Tokens
                </Button>
              )}
              <Typography variant="body2">
                {session.username} ({session.role})
              </Typography>
              <Button color="inherit" onClick={() => void logout()}>
                Sign out
              </Button>
            </Box>
          )}
        </Toolbar>
      </AppBar>
      <Container sx={{ py: 4 }}>
        <Outlet />
      </Container>
    </>
  )
}
