const { Pool } = require('pg');
const pool = new Pool({ connectionString: 'postgresql://postgres:postgres@localhost:6543/trading_db' });
async function reset() {
  await pool.query('UPDATE "user" SET "twoFactorEnabled" = false WHERE email = \'admin@gmail.com\'');
  await pool.query('DELETE FROM "twoFactor"');
  console.log('2FA Reset!');
  pool.end();
}
reset();
