/* eslint-disable @typescript-eslint/no-explicit-any, @typescript-eslint/no-unused-vars, react-hooks/exhaustive-deps, react-hooks/set-state-in-effect */
import { auth } from './lib/auth';
import { authDb } from './lib/auth-db';

async function test() {
  const email = 'admin@gmail.com';
  const password = 'Test@123';
  
  // Try to use the internal api to check password
  // Better auth provides a way to verify password if we can access it
  try {
     console.log('Testing auth');
  } catch(e) {
     console.error(e);
  }
}
test();
