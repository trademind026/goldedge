import { auth } from './lib/auth';

async function test() {
   // Let's create a new user directly using auth.api
   try {
     const user = await auth.api.signUpEmail({
       body: {
         email: "test2@gmail.com",
         password: "Test@123",
         name: "Test2"
       }
     });
     console.log("Signup successful", user);
   } catch(e) {
     console.error("Signup failed", e);
   }
}
test();
