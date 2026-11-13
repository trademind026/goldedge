CREATE TABLE "user" (
					id TEXT PRIMARY KEY,
					name TEXT NOT NULL,
					email TEXT NOT NULL UNIQUE,
					"emailVerified" BOOLEAN NOT NULL,
					image TEXT,
					"createdAt" TIMESTAMP NOT NULL,
					"updatedAt" TIMESTAMP NOT NULL,
					"twoFactorEnabled" BOOLEAN,
					role TEXT,
					banned BOOLEAN,
					"banReason" TEXT,
					"banExpires" TIMESTAMP
				);
				CREATE TABLE "session" (
					id TEXT PRIMARY KEY,
					"userId" TEXT NOT NULL REFERENCES "user"(id),
					token TEXT NOT NULL UNIQUE,
					"expiresAt" TIMESTAMP NOT NULL,
					"ipAddress" TEXT,
					"userAgent" TEXT,
					"createdAt" TIMESTAMP NOT NULL,
					"updatedAt" TIMESTAMP NOT NULL
				);
				CREATE TABLE "account" (
					id TEXT PRIMARY KEY,
					"userId" TEXT NOT NULL REFERENCES "user"(id),
					"accountId" TEXT NOT NULL,
					"providerId" TEXT NOT NULL,
					"accessToken" TEXT,
					"refreshToken" TEXT,
					"accessTokenExpiresAt" TIMESTAMP,
					"refreshTokenExpiresAt" TIMESTAMP,
					scope TEXT,
					idToken TEXT,
					password TEXT,
					"createdAt" TIMESTAMP NOT NULL,
					"updatedAt" TIMESTAMP NOT NULL
				);
				CREATE TABLE "verification" (
					id TEXT PRIMARY KEY,
					identifier TEXT NOT NULL,
					value TEXT NOT NULL,
					"expiresAt" TIMESTAMP NOT NULL,
					"createdAt" TIMESTAMP NOT NULL,
					"updatedAt" TIMESTAMP NOT NULL
				);
				CREATE TABLE "twoFactor" (
					id TEXT PRIMARY KEY,
					secret TEXT NOT NULL,
					"backupCodes" TEXT NOT NULL,
					"userId" TEXT NOT NULL REFERENCES "user"(id),
					verified BOOLEAN NOT NULL DEFAULT FALSE
				);
