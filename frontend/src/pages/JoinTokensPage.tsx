import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Typography from '@mui/material/Typography'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import Chip from '@mui/material/Chip'
import Button from '@mui/material/Button'
import TextField from '@mui/material/TextField'
import Alert from '@mui/material/Alert'
import CircularProgress from '@mui/material/CircularProgress'
import Dialog from '@mui/material/Dialog'
import DialogTitle from '@mui/material/DialogTitle'
import DialogContent from '@mui/material/DialogContent'
import DialogContentText from '@mui/material/DialogContentText'
import DialogActions from '@mui/material/DialogActions'
import Table from '@mui/material/Table'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableContainer from '@mui/material/TableContainer'
import TableHead from '@mui/material/TableHead'
import TableRow from '@mui/material/TableRow'
import { api, ApiError } from '../api/client'
import type { CreateJoinTokenResponse, JoinTokenRecord, JoinTokenStatus } from '../api/types'
import { formatLastSeen } from '../utils/deviceFormatting'

const STATUS_COLOR: Record<JoinTokenStatus, 'success' | 'default' | 'error' | 'warning'> = {
  active: 'success',
  consumed: 'default',
  revoked: 'error',
  expired: 'warning',
}

export default function JoinTokensPage() {
  const queryClient = useQueryClient()
  const [deviceId, setDeviceId] = useState('')
  const [issuedToken, setIssuedToken] = useState<CreateJoinTokenResponse | null>(null)

  const tokensQuery = useQuery({
    queryKey: ['join-tokens'],
    queryFn: () => api.get<JoinTokenRecord[]>('/api/v1/fleet/join-tokens'),
  })

  const createMutation = useMutation({
    mutationFn: (body: { device_id: string }) =>
      api.post<CreateJoinTokenResponse>('/api/v1/fleet/join-tokens', body),
    onSuccess: (response) => {
      setIssuedToken(response)
      setDeviceId('')
      queryClient.invalidateQueries({ queryKey: ['join-tokens'] })
    },
  })

  const revokeMutation = useMutation({
    mutationFn: (tokenId: string) => api.delete(`/api/v1/fleet/join-tokens/${tokenId}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['join-tokens'] }),
  })

  function handleIssue(event: FormEvent) {
    event.preventDefault()
    createMutation.mutate({ device_id: deviceId })
  }

  return (
    <Stack spacing={3}>
      <Typography variant="h5">Join Tokens</Typography>

      <Paper sx={{ p: 3 }} component="form" onSubmit={handleIssue}>
        <Typography variant="h6" gutterBottom>
          Issue a join token
        </Typography>
        {createMutation.isError && (
          <Alert severity="error" sx={{ mb: 2 }}>
            {createMutation.error instanceof ApiError ? createMutation.error.message : 'Failed to issue token.'}
          </Alert>
        )}
        <Stack direction="row" spacing={2} sx={{ alignItems: 'center' }}>
          <TextField
            label="Device ID"
            value={deviceId}
            onChange={(e) => setDeviceId(e.target.value)}
            required
            helperText="The device_id the enrolling device will present -- doesn't need to exist yet."
          />
          <Button type="submit" variant="contained" disabled={createMutation.isPending}>
            {createMutation.isPending ? 'Issuing…' : 'Issue token'}
          </Button>
        </Stack>
      </Paper>

      <Paper sx={{ p: 3 }}>
        <Typography variant="h6" gutterBottom>
          Issued tokens
        </Typography>
        {tokensQuery.isLoading && <CircularProgress size={24} />}
        {tokensQuery.error && (
          <Alert severity="error">
            {tokensQuery.error instanceof ApiError ? tokensQuery.error.message : 'Failed to load join tokens.'}
          </Alert>
        )}
        {tokensQuery.data && tokensQuery.data.length === 0 && (
          <Typography variant="body2" color="text.secondary">
            No join tokens issued yet.
          </Typography>
        )}
        {tokensQuery.data && tokensQuery.data.length > 0 && (
          <TableContainer>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Device ID</TableCell>
                  <TableCell>Created</TableCell>
                  <TableCell>Expires</TableCell>
                  <TableCell>Status</TableCell>
                  <TableCell />
                </TableRow>
              </TableHead>
              <TableBody>
                {tokensQuery.data.map((token) => (
                  <TableRow key={token.id}>
                    <TableCell>{token.device_id}</TableCell>
                    <TableCell>{formatLastSeen(token.created_at)}</TableCell>
                    <TableCell>{formatLastSeen(token.expires_at)}</TableCell>
                    <TableCell>
                      <Chip label={token.status} color={STATUS_COLOR[token.status]} size="small" />
                    </TableCell>
                    <TableCell>
                      {token.status === 'active' && (
                        <Button
                          size="small"
                          color="error"
                          disabled={revokeMutation.isPending}
                          onClick={() => revokeMutation.mutate(token.id)}
                        >
                          Revoke
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        )}
      </Paper>

      <Dialog open={issuedToken !== null} onClose={() => setIssuedToken(null)} maxWidth="sm" fullWidth>
        <DialogTitle>Join token issued</DialogTitle>
        <DialogContent>
          <DialogContentText sx={{ mb: 2 }}>
            This token is shown only once and cannot be retrieved again -- copy it now and give it to the
            device being enrolled ({issuedToken?.device_id}). It expires in{' '}
            {issuedToken ? Math.round(issuedToken.ttl_seconds / 60) : 0} minutes.
          </DialogContentText>
          <TextField
            value={issuedToken?.join_token ?? ''}
            fullWidth
            slotProps={{ input: { readOnly: true, sx: { fontFamily: 'monospace' } } }}
            onFocus={(e) => e.target.select()}
          />
        </DialogContent>
        <DialogActions>
          <Button
            onClick={() => issuedToken && navigator.clipboard.writeText(issuedToken.join_token)}
          >
            Copy
          </Button>
          <Button onClick={() => setIssuedToken(null)} variant="contained">
            Done
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  )
}
