"use client";

import { createAuthClient } from "better-auth/react";
import { adminClient } from "better-auth/client/plugins";
import { twoFactorClient } from "better-auth/client/plugins";

export const authClient = createAuthClient({
  plugins: [
    adminClient(),
    twoFactorClient({
      twoFactorPage: "/two-factor",
    }),
  ],
});

export const { signIn, signOut, useSession } = authClient;
