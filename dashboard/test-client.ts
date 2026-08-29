import { createAuthClient } from "better-auth/react"
const authClient = createAuthClient({
    baseURL: "http://localhost:3000"
})

async function test() {
  const { data, error } = await authClient.signIn.email({
      email: "admin@gmail.com",
      password: "Test@123"
  });
  console.log("Data:", data);
  console.log("Error:", error);
}
test();
