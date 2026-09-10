import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { fetchNotifications, markNotificationsRead } from "./auth";
import { useAuth } from "./AuthContext";

/**
 * The customer's bell.
 *
 * **This deliberately does not poll, and deliberately never fetches for a visitor who is
 * not signed in.** Aurora is configured to scale to zero, and an anonymous car page is
 * served entirely from CloudFront without waking it. A `useEffect` that fetched on mount
 * for everyone would turn all of that cached traffic into origin requests and keep the
 * database warm around the clock — for a shop that takes a handful of bookings a week,
 * it would be the single most expensive line of code in the project.
 *
 * So: one fetch when a signed-in session is confirmed, and another after anything the
 * customer does that could have created a notification. A signed-in visitor already hits
 * the uncached `/api/auth/me/` on every load, so the database is awake for them anyway.
 *
 * The cost is that something arriving while a tab sits open is not seen until the next
 * navigation. For this site that is the right trade.
 */
const NotificationContext = createContext(null);

export function NotificationProvider({ children }) {
  const { state: authState } = useAuth();
  const [items, setItems] = useState([]);
  const [unread, setUnread] = useState(0);
  const [state, setState] = useState("unknown");

  const refresh = useCallback(async () => {
    try {
      const { items: rows, unread: count } = await fetchNotifications();
      setItems(rows);
      setUnread(count);
      setState("loaded");
    } catch {
      // A bell that cannot load must not take the masthead down with it.
      setState("loaded");
    }
  }, []);

  useEffect(() => {
    if (authState === "signed-in") {
      refresh();
    } else if (authState === "anonymous") {
      setItems([]);
      setUnread(0);
      setState("anonymous");
    }
  }, [authState, refresh]);

  const markAllRead = useCallback(async () => {
    // Optimistic: the count is the only thing anyone is watching, and a failed POST
    // costs a stale badge until the next navigation, not a lost notification.
    setUnread(0);
    try {
      await markNotificationsRead();
    } catch {
      /* the next refresh will put it back */
    }
  }, []);

  const value = useMemo(
    () => ({ items, unread, state, refresh, markAllRead }),
    [items, unread, state, refresh, markAllRead],
  );

  return (
    <NotificationContext.Provider value={value}>{children}</NotificationContext.Provider>
  );
}

export function useNotifications() {
  const value = useContext(NotificationContext);
  if (!value) throw new Error("useNotifications must be used inside NotificationProvider");
  return value;
}
