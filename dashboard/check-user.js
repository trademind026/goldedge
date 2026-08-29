const { Pool } = require('pg');
const pool = new Pool({ connectionString: 'postgresql://postgres:postgres@localhost:6543/trading_db' });
pool.query('SELECT "twoFactorEnabled" FROM "user" WHERE email = \'admin@gmail.com\'', (err, res) => {
  if (err) console.error(err);
  else console.log(res.rows[0]);
  pool.end();
});
