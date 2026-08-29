import { auth } from './lib/auth';

async function test() {
   try {
     const user = await auth.api.signInEmail({
       body: {
         email: "admin@gmail.com",
         password: "Test@123"
       }
     });
     console.log("SignIn successful", user);
   } catch(e) {
     console.error("SignIn failed", e);
   }
}
test();
