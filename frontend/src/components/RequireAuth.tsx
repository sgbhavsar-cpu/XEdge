import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'

export default function RequireAuth() {
  const { session } = useAuth()
  const location = useLocation()

  if (session === null) {
    return <Navigate to="/login" state={{ from: location }} replace />
  }
  return <Outlet />
}
