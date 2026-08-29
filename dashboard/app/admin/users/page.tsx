"use client";

import { FormEvent, useEffect, useState, useCallback } from "react";
import Sidebar from "@/components/Sidebar";
import { AUTH_ROLES, type AuthRole, isAdminRole } from "@/lib/auth-policy";
import { useSession } from "@/lib/auth-client";
import { RefreshCw, UserPlus } from "lucide-react";

interface AdminUser {
  id: string;
  name: string;
  email: string;
  role?: string;
  banned?: boolean | null;
  twoFactorEnabled?: boolean;
}

export default function AdminUsersPage() {
  const session = useSession();
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<AuthRole>(AUTH_ROLES.trader);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const isAdmin = isAdminRole(session.data?.user.role);

  const loadUsers = useCallback(async () => {
    if (!isAdmin) return;
    const response = await fetch("/api/auth/admin/list-users", {
      credentials: "include",
      cache: "no-store",
    });

    if (!response.ok) {
      setError("Unable to load users");
      return;
    }

    const payload = await response.json();
    setUsers(payload.users ?? []);
  }, [isAdmin]);

  useEffect(() => {
    const init = async () => { await loadUsers(); };
    init();
  }, [loadUsers]);

  const createUser = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    setMessage(null);
    setLoading(true);

    const response = await fetch("/api/auth/admin/create-user", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ name, email, password, role }),
    });

    setLoading(false);

    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      setError(payload?.message ?? payload?.error ?? "Unable to create user");
      return;
    }

    setName("");
    setEmail("");
    setPassword("");
    setRole(AUTH_ROLES.trader);
    setMessage("User created");
    await loadUsers();
  };

  if (session.isPending) {
    return <main className="min-h-screen grid place-items-center">Loading</main>;
  }

  if (!isAdmin) {
    return (
      <div className="flex min-h-screen">
        <Sidebar />
        <main className="flex-1 grid place-items-center">
          <p className="text-[11px] uppercase tracking-[0.25em]" style={{ color: "#ff3e3e" }}>Admin access required</p>
        </main>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-5 space-y-5 overflow-y-auto">
        <div>
          <p className="text-[10px] uppercase tracking-[0.25em]" style={{ color: "#3d4450" }}>Admin</p>
          <h1 className="text-xl font-bold" style={{ color: "#00e87b" }}>User Management</h1>
        </div>

        <form onSubmit={createUser} className="grid md:grid-cols-5 gap-3 p-4" style={{ border: "1px solid #252a33", background: "#0e1117" }}>
          <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Name" className="px-3 py-2 bg-transparent outline-none text-sm" style={{ border: "1px solid #252a33" }} required />
          <input value={email} onChange={(event) => setEmail(event.target.value)} placeholder="Email" type="email" className="px-3 py-2 bg-transparent outline-none text-sm" style={{ border: "1px solid #252a33" }} required />
          <input value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Temporary password" type="password" className="px-3 py-2 bg-transparent outline-none text-sm" style={{ border: "1px solid #252a33" }} required />
          <select value={role} onChange={(event) => setRole(event.target.value as AuthRole)} className="px-3 py-2 bg-transparent outline-none text-sm" style={{ border: "1px solid #252a33" }}>
            <option value={AUTH_ROLES.trader}>Trader</option>
            <option value={AUTH_ROLES.admin}>Admin</option>
          </select>
          <button disabled={loading} className="t-btn-green flex items-center justify-center gap-2 disabled:opacity-50">
            <UserPlus className="w-4 h-4" />
            {loading ? "CREATING" : "CREATE"}
          </button>
        </form>

        {message && <p className="text-[11px]" style={{ color: "#00e87b" }}>{message}</p>}
        {error && <p className="text-[11px]" style={{ color: "#ff3e3e" }}>{error}</p>}

        <section className="space-y-2">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-bold uppercase tracking-wider">Users</h2>
            <button onClick={loadUsers} className="t-btn flex items-center gap-2">
              <RefreshCw className="w-3 h-3" />
              Refresh
            </button>
          </div>

          <div className="overflow-hidden" style={{ border: "1px solid #252a33" }}>
            <table className="w-full text-sm">
              <thead style={{ background: "#0e1117", color: "#5a6270" }}>
                <tr>
                  <th className="text-left p-3">Name</th>
                  <th className="text-left p-3">Email</th>
                  <th className="text-left p-3">Role</th>
                  <th className="text-left p-3">2FA</th>
                  <th className="text-left p-3">Status</th>
                </tr>
              </thead>
              <tbody>
                {users.map((user) => (
                  <tr key={user.id} style={{ borderTop: "1px solid #252a33" }}>
                    <td className="p-3">{user.name}</td>
                    <td className="p-3">{user.email}</td>
                    <td className="p-3">{user.role ?? AUTH_ROLES.trader}</td>
                    <td className="p-3">{user.twoFactorEnabled ? "Enabled" : "Required"}</td>
                    <td className="p-3">{user.banned ? "Disabled" : "Active"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </main>
    </div>
  );
}
