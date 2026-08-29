import { createAuthClient } from "better-auth/react"
const authClient = createAuthClient({
    baseURL: "http://localhost:3000"
})

async function test() {
  const { data, error } = await authClient.signUp.email({
      email: "test_new@gmail.com",
      name: "Test New",
      password: "Test@123"
  });
  console.log("SignUp Data:", data);
  console.log("SignUp Error:", error);
}
test();
