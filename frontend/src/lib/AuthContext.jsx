import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { fetchMe } from "./auth";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [customer, setCustomer] = useState(null);
  // "unknown" until the first check completes, so the header does not flash "Sign in"
  // at someone who is already signed in.
  const [state, setState] = useState("unknown");

  const refresh = useCallback(async () => {
    const me = await fetchMe();
    setCustomer(me);
    setState(me ? "signed-in" : "anonymous");
    return me;
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const value = useMemo(
    () => ({ customer, state, refresh, setCustomer }),
    [customer, state, refresh],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}
