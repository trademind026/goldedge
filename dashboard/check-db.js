const { Pool } = require('pg');
const pool = new Pool({ connectionString: 'postgresql://postgres:postgres@localhost:6543/trading_db' });
pool.query('SELECT * FROM "account"', (err, res) => {
  if (err) console.error(err);
  else console.log("Account", res.rows);
  pool.end();
});
