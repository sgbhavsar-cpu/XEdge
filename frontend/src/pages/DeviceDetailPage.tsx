import { useState, type FormEvent } from 'react'
import { useParams, Link as RouterLink } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Typography from '@mui/material/Typography'
import Paper from '@mui/material/Paper'
import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import Chip from '@mui/material/Chip'
import Button from '@mui/material/Button'
import TextField from '@mui/material/TextField'
import Alert from '@mui/material/Alert'
import Link from '@mui/material/Link'
import CircularProgress from '@mui/material/CircularProgress'
import Table from '@mui/material/Table'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableContainer from '@mui/material/TableContainer'
import TableHead from '@mui/material/TableHead'
import TableRow from '@mui/material/TableRow'
import { api, ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { hasPermission } from '../auth/permissions'
import type {
  CertificateHistoryEntry,
  ConfigHistoryEntry,
  DeviceMetadataUpdate,
  DeviceSummary,
} from '../api/types'
import { connectionStateColor, formatCertExpiry, formatLastSeen } from '../utils/deviceFormatting'

export default function DeviceDetailPage() {
  // Present whenever this page renders -- only reachable via the
  // /devices/:deviceId route.
  const { deviceId } = useParams<{ deviceId: string }>()
  const id = deviceId as string

  const deviceQuery = useQuery({
    queryKey: ['devices', id],
    queryFn: () => api.get<DeviceSummary>(`/api/v1/fleet/devices/${id}`),
  })
  const configHistoryQuery = useQuery({
    queryKey: ['devices', id, 'config-history'],
    queryFn: () => api.get<ConfigHistoryEntry[]>(`/api/v1/fleet/devices/${id}/config-history`),
  })
  const certHistoryQuery = useQuery({
    queryKey: ['devices', id, 'certificate-history'],
    queryFn: () => api.get<CertificateHistoryEntry[]>(`/api/v1/fleet/devices/${id}/certificate-history`),
  })

  return (
    <Stack spacing={3}>
      <Link component={RouterLink} to="/devices">
        &larr; Back to devices
      </Link>

      {deviceQuery.isLoading && <CircularProgress />}
      {deviceQuery.error && (
        <Alert severity="error">
          {deviceQuery.error instanceof ApiError ? deviceQuery.error.message : 'Failed to load device.'}
        </Alert>
      )}
      {deviceQuery.data && <DeviceHeader device={deviceQuery.data} />}
      {deviceQuery.data && <MetadataSection deviceId={id} device={deviceQuery.data} />}
      {deviceQuery.data && <PendingConfigCard device={deviceQuery.data} />}

      <ConfigHistorySection query={configHistoryQuery} />
      <CertificateHistorySection query={certHistoryQuery} />
    </Stack>
  )
}

function DeviceHeader({ device }: { device: DeviceSummary }) {
  return (
    <Paper sx={{ p: 3 }}>
      <Typography variant="h5">{device.display_name ?? device.device_id}</Typography>
      <Typography variant="body2" color="text.secondary" gutterBottom>
        {device.device_id}
      </Typography>
      <Stack direction="row" spacing={3} sx={{ mt: 1, alignItems: 'center' }}>
        <Chip
          label={device.connection_state}
          color={connectionStateColor(device.connection_state)}
          size="small"
          variant="outlined"
        />
        <Typography variant="body2">Last seen: {formatLastSeen(device.last_seen_at)}</Typography>
        <Typography variant="body2">Cert expiry: {formatCertExpiry(device.cert_not_after)}</Typography>
        <Typography variant="body2">Agent: {device.agent_version ?? '—'}</Typography>
      </Stack>
    </Paper>
  )
}

const METADATA_FIELDS = [
  { key: 'display_name', label: 'Display Name' },
  { key: 'serial_number', label: 'Serial Number' },
  { key: 'make', label: 'Make' },
  { key: 'protocol', label: 'Protocol' },
  { key: 'hardware_firmware_version', label: 'Firmware Version' },
] as const

function MetadataSection({ deviceId, device }: { deviceId: string; device: DeviceSummary }) {
  const { session } = useAuth()
  const canWrite = session !== null && hasPermission(session.role, 'device:write')
  const queryClient = useQueryClient()

  // Seeded once from `device` -- this component only mounts after the
  // parent's query has already resolved, so there's no async gap where a
  // default of '' would flash before the real value arrives.
  const [fields, setFields] = useState<Record<string, string>>(() =>
    Object.fromEntries(METADATA_FIELDS.map(({ key }) => [key, device[key] ?? ''])),
  )

  const mutation = useMutation({
    mutationFn: (body: DeviceMetadataUpdate) =>
      api.patch<DeviceSummary>(`/api/v1/fleet/devices/${deviceId}/metadata`, body),
    onSuccess: (updated) => {
      queryClient.setQueryData(['devices', deviceId], updated)
      queryClient.invalidateQueries({ queryKey: ['devices'] })
    },
  })

  function handleSubmit(event: FormEvent) {
    event.preventDefault()
    mutation.mutate(
      Object.fromEntries(METADATA_FIELDS.map(({ key }) => [key, fields[key] === '' ? null : fields[key]])),
    )
  }

  return (
    <Paper sx={{ p: 3 }} component="form" onSubmit={handleSubmit}>
      <Typography variant="h6" gutterBottom>
        Metadata
      </Typography>
      {mutation.isError && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {mutation.error instanceof ApiError ? mutation.error.message : 'Failed to save metadata.'}
        </Alert>
      )}
      {mutation.isSuccess && (
        <Alert severity="success" sx={{ mb: 2 }}>
          Saved.
        </Alert>
      )}
      <Stack spacing={2} sx={{ maxWidth: 480 }}>
        {METADATA_FIELDS.map(({ key, label }) => (
          <TextField
            key={key}
            label={label}
            value={fields[key]}
            onChange={(e) => setFields((prev) => ({ ...prev, [key]: e.target.value }))}
            fullWidth
            disabled={!canWrite}
          />
        ))}
        {canWrite && (
          <Button type="submit" variant="contained" disabled={mutation.isPending} sx={{ alignSelf: 'flex-start' }}>
            {mutation.isPending ? 'Saving…' : 'Save'}
          </Button>
        )}
      </Stack>
    </Paper>
  )
}

function PendingConfigCard({ device }: { device: DeviceSummary }) {
  return (
    <Paper sx={{ p: 3 }}>
      <Typography variant="h6" gutterBottom>
        Pending Config
      </Typography>
      <Stack direction="row" spacing={3} sx={{ alignItems: 'center' }}>
        <Chip
          label={device.has_pending_config ? 'Pending' : 'None'}
          color={device.has_pending_config ? 'warning' : 'default'}
          size="small"
        />
        <Typography variant="body2">Version: {device.pending_config_version}</Typography>
      </Stack>
      {device.last_config_apply && (
        <Box sx={{ mt: 2 }}>
          <Typography variant="body2">
            Last apply (v{device.last_config_apply.version}):{' '}
            {device.last_config_apply.success ? 'succeeded' : `failed — ${device.last_config_apply.error}`}
          </Typography>
        </Box>
      )}
    </Paper>
  )
}

function ConfigHistorySection({
  query,
}: {
  query: ReturnType<typeof useQuery<ConfigHistoryEntry[]>>
}) {
  return (
    <Paper sx={{ p: 3 }}>
      <Typography variant="h6" gutterBottom>
        Config History
      </Typography>
      {query.isLoading && <CircularProgress size={24} />}
      {query.error && <Alert severity="error">Failed to load config history.</Alert>}
      {query.data && query.data.length === 0 && (
        <Typography variant="body2" color="text.secondary">
          No configs have been pushed to this device yet.
        </Typography>
      )}
      {query.data && query.data.length > 0 && (
        <TableContainer>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Version</TableCell>
                <TableCell>Pushed At</TableCell>
                <TableCell>Pushed By</TableCell>
                <TableCell>Applied At</TableCell>
                <TableCell>Result</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {query.data.map((entry) => (
                <TableRow key={entry.config_version}>
                  <TableCell>{entry.config_version}</TableCell>
                  <TableCell>{formatLastSeen(entry.pushed_at)}</TableCell>
                  <TableCell>{entry.pushed_by}</TableCell>
                  <TableCell>{formatLastSeen(entry.applied_at)}</TableCell>
                  <TableCell>
                    {entry.apply_success === null && 'pending'}
                    {entry.apply_success === true && 'success'}
                    {entry.apply_success === false && `failed — ${entry.apply_error}`}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
    </Paper>
  )
}

function CertificateHistorySection({
  query,
}: {
  query: ReturnType<typeof useQuery<CertificateHistoryEntry[]>>
}) {
  return (
    <Paper sx={{ p: 3 }}>
      <Typography variant="h6" gutterBottom>
        Certificate History
      </Typography>
      {query.isLoading && <CircularProgress size={24} />}
      {query.error && <Alert severity="error">Failed to load certificate history.</Alert>}
      {query.data && query.data.length === 0 && (
        <Typography variant="body2" color="text.secondary">
          No certificates recorded yet.
        </Typography>
      )}
      {query.data && query.data.length > 0 && (
        <TableContainer>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Serial Number</TableCell>
                <TableCell>Issued At</TableCell>
                <TableCell>Not Before</TableCell>
                <TableCell>Not After</TableCell>
                <TableCell>Reason</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {query.data.map((entry) => (
                <TableRow key={entry.serial_number}>
                  <TableCell>{entry.serial_number}</TableCell>
                  <TableCell>{formatLastSeen(entry.issued_at)}</TableCell>
                  <TableCell>{formatLastSeen(entry.not_before)}</TableCell>
                  <TableCell>{formatLastSeen(entry.not_after)}</TableCell>
                  <TableCell>{entry.reason}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
    </Paper>
  )
}
