import { betterAuth } from "better-auth";
import { admin } from "better-auth/plugins/admin";
import { twoFactor } from "better-auth/plugins/two-factor";
import { nextCookies } from "better-auth/next-js";
import { authDb } from "@/lib/auth-db";
import { AUTH_ROLES } from "@/lib/auth-policy";
import { optionalEnvList, requiredEnv } from "@/lib/server-env";

export const auth = betterAuth({
  database: authDb,
  secret: requiredEnv("BETTER_AUTH_SECRET"),
  baseURL: requiredEnv("BETTER_AUTH_URL"),
  trustedOrigins: optionalEnvList("BETTER_AUTH_TRUSTED_ORIGINS"),
  emailAndPassword: {
    enabled: true,
    requireEmailVerification: false,
  },
  session: {
    expiresIn: 60 * 60 * 24 * 7,
    updateAge: 60 * 60 * 24,
    freshAge: 60 * 15,
  },
  plugins: [
    admin({
      defaultRole: AUTH_ROLES.trader,
      adminRoles: [AUTH_ROLES.admin],
    }),
    twoFactor({
      issuer: process.env.BETTER_AUTH_2FA_ISSUER,
      skipVerificationOnEnable: false,
    }),
    nextCookies(),
  ],
});

export type AuthSession = typeof auth.$Infer.Session;
