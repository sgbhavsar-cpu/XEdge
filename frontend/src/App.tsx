import { Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/Layout'
import RequireAuth from './components/RequireAuth'
import LoginPage from './pages/LoginPage'
import DeviceListPage from './pages/DeviceListPage'

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<RequireAuth />}>
        <Route element={<Layout />}>
          <Route path="/" element={<Navigate to="/devices" replace />} />
          <Route path="/devices" element={<DeviceListPage />} />
        </Route>
      </Route>
    </Routes>
  )
}
