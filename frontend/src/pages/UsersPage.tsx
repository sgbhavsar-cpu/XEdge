import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Typography from '@mui/material/Typography'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import Button from '@mui/material/Button'
import TextField from '@mui/material/TextField'
import Select from '@mui/material/Select'
import MenuItem from '@mui/material/MenuItem'
import InputLabel from '@mui/material/InputLabel'
import FormControl from '@mui/material/FormControl'
import Alert from '@mui/material/Alert'
import CircularProgress from '@mui/material/CircularProgress'
import Dialog from '@mui/material/Dialog'
import DialogTitle from '@mui/material/DialogTitle'
import DialogContent from '@mui/material/DialogContent'
import DialogActions from '@mui/material/DialogActions'
import Table from '@mui/material/Table'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableContainer from '@mui/material/TableContainer'
import TableHead from '@mui/material/TableHead'
import TableRow from '@mui/material/TableRow'
import { api, ApiError } from '../api/client'
import type { Role } from '../auth/permissions'

interface FleetUser {
  username: string
  role: Role
}

const ROLES: Role[] = ['admin', 'operator', 'auditor', 'readonly']

export default function UsersPage() {
  const queryClient = useQueryClient()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<Role>('readonly')
  const [passwordDialogUser, setPasswordDialogUser] = useState<string | null>(null)
  const [newPassword, setNewPassword] = useState('')
  const [deleteError, setDeleteError] = useState<string | null>(null)

  const usersQuery = useQuery({
    queryKey: ['users'],
    queryFn: () => api.get<FleetUser[]>('/api/v1/fleet/users'),
  })

  const createMutation = useMutation({
    mutationFn: (body: { username: string; password: string; role: Role }) =>
      api.post<FleetUser>('/api/v1/fleet/users', body),
    onSuccess: () => {
      setUsername('')
      setPassword('')
      setRole('readonly')
      queryClient.invalidateQueries({ queryKey: ['users'] })
    },
  })

  const roleMutation = useMutation({
    mutationFn: ({ user, role: newRole }: { user: string; role: Role }) =>
      api.patch(`/api/v1/fleet/users/${user}`, { role: newRole }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['users'] }),
  })

  const passwordMutation = useMutation({
    mutationFn: ({ user, password: newPw }: { user: string; password: string }) =>
      api.patch(`/api/v1/fleet/users/${user}`, { password: newPw }),
    onSuccess: () => {
      setPasswordDialogUser(null)
      setNewPassword('')
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (user: string) => api.delete(`/api/v1/fleet/users/${user}`),
    onSuccess: () => {
      setDeleteError(null)
      queryClient.invalidateQueries({ queryKey: ['users'] })
    },
    onError: (err) => setDeleteError(err instanceof ApiError ? err.message : 'Failed to delete user.'),
  })

  function handleCreate(event: FormEvent) {
    event.preventDefault()
    createMutation.mutate({ username, password, role })
  }

  function handlePasswordSubmit(event: FormEvent) {
    event.preventDefault()
    if (passwordDialogUser) {
      passwordMutation.mutate({ user: passwordDialogUser, password: newPassword })
    }
  }

  return (
    <Stack spacing={3}>
      <Typography variant="h5">Users</Typography>

      <Paper sx={{ p: 3 }} component="form" onSubmit={handleCreate}>
        <Typography variant="h6" gutterBottom>
          Create a user
        </Typography>
        {createMutation.isError && (
          <Alert severity="error" sx={{ mb: 2 }}>
            {createMutation.error instanceof ApiError ? createMutation.error.message : 'Failed to create user.'}
          </Alert>
        )}
        <Stack direction="row" spacing={2} sx={{ alignItems: 'flex-start' }}>
          <TextField label="Username" value={username} onChange={(e) => setUsername(e.target.value)} required />
          <TextField
            label="Password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
          <FormControl sx={{ minWidth: 140 }}>
            <InputLabel id="create-role-label">Role</InputLabel>
            <Select
              labelId="create-role-label"
              label="Role"
              value={role}
              onChange={(e) => setRole(e.target.value as Role)}
            >
              {ROLES.map((r) => (
                <MenuItem key={r} value={r}>
                  {r}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <Button type="submit" variant="contained" disabled={createMutation.isPending} sx={{ mt: 1 }}>
            {createMutation.isPending ? 'Creating…' : 'Create'}
          </Button>
        </Stack>
      </Paper>

      <Paper sx={{ p: 3 }}>
        <Typography variant="h6" gutterBottom>
          Accounts
        </Typography>
        {deleteError && (
          <Alert severity="error" sx={{ mb: 2 }} onClose={() => setDeleteError(null)}>
            {deleteError}
          </Alert>
        )}
        {usersQuery.isLoading && <CircularProgress size={24} />}
        {usersQuery.error && (
          <Alert severity="error">
            {usersQuery.error instanceof ApiError ? usersQuery.error.message : 'Failed to load users.'}
          </Alert>
        )}
        {usersQuery.data && (
          <TableContainer>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Username</TableCell>
                  <TableCell>Role</TableCell>
                  <TableCell />
                </TableRow>
              </TableHead>
              <TableBody>
                {usersQuery.data.map((user) => (
                  <TableRow key={user.username}>
                    <TableCell>{user.username}</TableCell>
                    <TableCell>
                      <Select
                        size="small"
                        value={user.role}
                        onChange={(e) =>
                          roleMutation.mutate({ user: user.username, role: e.target.value as Role })
                        }
                      >
                        {ROLES.map((r) => (
                          <MenuItem key={r} value={r}>
                            {r}
                          </MenuItem>
                        ))}
                      </Select>
                    </TableCell>
                    <TableCell>
                      <Stack direction="row" spacing={1}>
                        <Button size="small" onClick={() => setPasswordDialogUser(user.username)}>
                          Reset Password
                        </Button>
                        <Button
                          size="small"
                          color="error"
                          onClick={() => deleteMutation.mutate(user.username)}
                        >
                          Delete
                        </Button>
                      </Stack>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        )}
      </Paper>

      <Dialog open={passwordDialogUser !== null} onClose={() => setPasswordDialogUser(null)}>
        <form onSubmit={handlePasswordSubmit}>
          <DialogTitle>Reset password for {passwordDialogUser}</DialogTitle>
          <DialogContent>
            {passwordMutation.isError && (
              <Alert severity="error" sx={{ mb: 2 }}>
                {passwordMutation.error instanceof ApiError
                  ? passwordMutation.error.message
                  : 'Failed to reset password.'}
              </Alert>
            )}
            <TextField
              autoFocus
              label="New Password"
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              fullWidth
              required
              sx={{ mt: 1 }}
            />
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setPasswordDialogUser(null)}>Cancel</Button>
            <Button type="submit" variant="contained" disabled={passwordMutation.isPending}>
              {passwordMutation.isPending ? 'Saving…' : 'Save'}
            </Button>
          </DialogActions>
        </form>
      </Dialog>
    </Stack>
  )
}
