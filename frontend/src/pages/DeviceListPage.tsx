import { useQuery } from '@tanstack/react-query'
import { Link as RouterLink } from 'react-router-dom'
import { DataGrid, type GridColDef } from '@mui/x-data-grid'
import Chip from '@mui/material/Chip'
import Link from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import Alert from '@mui/material/Alert'
import { api, ApiError } from '../api/client'
import type { DeviceSummary } from '../api/types'
import { connectionStateColor, formatCertExpiry, formatLastSeen } from '../utils/deviceFormatting'

const columns: GridColDef<DeviceSummary>[] = [
  {
    field: 'display_name',
    headerName: 'Name',
    flex: 1,
    valueGetter: (_value, row) => row.display_name ?? row.device_id,
    renderCell: (params) => (
      <Link component={RouterLink} to={`/devices/${params.row.device_id}`}>
        {params.value}
      </Link>
    ),
  },
  { field: 'device_id', headerName: 'Device ID', flex: 1 },
  {
    field: 'connection_state',
    headerName: 'Connection',
    width: 140,
    renderCell: (params) => (
      <Chip
        label={params.value}
        color={connectionStateColor(params.value)}
        size="small"
        variant="outlined"
      />
    ),
  },
  {
    field: 'cert_not_after',
    headerName: 'Cert Expiry',
    width: 200,
    valueGetter: (_value, row) => formatCertExpiry(row.cert_not_after),
  },
  {
    field: 'last_seen_at',
    headerName: 'Last Seen',
    width: 200,
    valueGetter: (_value, row) => formatLastSeen(row.last_seen_at),
  },
  {
    field: 'agent_version',
    headerName: 'Agent Version',
    width: 140,
    valueGetter: (_value, row) => row.agent_version ?? '—',
  },
]

export default function DeviceListPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ['devices'],
    queryFn: () => api.get<DeviceSummary[]>('/api/v1/fleet/devices'),
    // Mirrors the device-local Web UI's fleet-status polling cadence
    // (xedge-ui.js's LOG_POLL_INTERVAL_MS precedent) so an operator sees a
    // device's connection state and cert expiry update without a manual
    // refresh.
    refetchInterval: 30_000,
  })

  if (error) {
    const message = error instanceof ApiError ? error.message : 'Failed to load devices.'
    return <Alert severity="error">{message}</Alert>
  }

  return (
    <>
      <Typography variant="h5" sx={{ mb: 2 }}>
        Devices
      </Typography>
      <DataGrid
        rows={data ?? []}
        columns={columns}
        getRowId={(row) => row.device_id}
        loading={isLoading}
        showToolbar
        autoHeight
        initialState={{ pagination: { paginationModel: { pageSize: 25 } } }}
        pageSizeOptions={[25, 50, 100]}
      />
    </>
  )
}
