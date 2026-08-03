// Client-side mirror of xedge/fleet/permissions.py's ROLE_PERMISSIONS --
// used only to decide whether to *show* a nav link or action. The real
// enforcement is server-side on every request (require_permission in
// manager_app.py); a stale or tampered copy of this map can only ever
// hide a button too eagerly or too late, never grant access the backend
// wouldn't already have refused.
export type Role = 'admin' | 'operator' | 'auditor' | 'readonly'
export type Permission = 'device:read' | 'device:write' | 'user:manage' | 'audit:read'

const ROLE_PERMISSIONS: Record<Role, ReadonlySet<Permission>> = {
  admin: new Set(['device:read', 'device:write', 'user:manage', 'audit:read']),
  operator: new Set(['device:read', 'device:write']),
  auditor: new Set(['device:read', 'audit:read']),
  readonly: new Set(['device:read']),
}

export function hasPermission(role: string, permission: Permission): boolean {
  return ROLE_PERMISSIONS[role as Role]?.has(permission) ?? false
}
