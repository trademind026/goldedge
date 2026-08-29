export const AUTH_ROLES = {
  admin: "admin",
  trader: "trader",
} as const;

export type AuthRole = (typeof AUTH_ROLES)[keyof typeof AUTH_ROLES];

export const TRADING_ROLES: AuthRole[] = [AUTH_ROLES.admin, AUTH_ROLES.trader];

export function isAdminRole(role: unknown): boolean {
  return role === AUTH_ROLES.admin;
}

export function canTrade(role: unknown): boolean {
  return TRADING_ROLES.includes(role as AuthRole);
}

export function hasTwoFactorEnabled(user: unknown): boolean {
  return Boolean((user as { twoFactorEnabled?: boolean } | null)?.twoFactorEnabled);
}
