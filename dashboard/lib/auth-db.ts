import pg from "pg";
import { requiredEnv } from "@/lib/server-env";

const { Pool } = pg;

export const authDb = new Pool({
  connectionString: requiredEnv("DATABASE_URL"),
});
