import { Route, Routes } from "react-router-dom";
import { useTranslation } from "react-i18next";

import CallButton from "./components/CallButton";
import Header from "./components/Header";
import Account from "./pages/Account";
import ResetPassword from "./pages/ResetPassword";
import VerifyEmail from "./pages/VerifyEmail";
import BookTestDrive from "./pages/BookTestDrive";
import CarDetail from "./pages/CarDetail";
import Home from "./pages/Home";
import { AuthProvider } from "./lib/AuthContext";
import { NotificationProvider } from "./lib/NotificationContext";

function Footer() {
  const { t } = useTranslation();
  const year = new Date().getFullYear();

  return (
    <footer className="footer">
      <div className="footer__inner">
        <p className="footer__prompt">{t("footer.callPrompt")}</p>
        <CallButton onInk />
        <p className="footer__legal u-nums">
          © {year} {t("footer.rights")}
        </p>
        {/*
          No staff login link here any more. It sat on every page of a public site
          advertising where the admin lives, and bought nothing: staff know the URL, and
          they now get an Admin button in the masthead once signed in.
        */}
      </div>
    </footer>
  );
}

export default function App() {
  return (
    // Inside AuthProvider, because the bell only ever fetches once it knows there is a
    // signed-in customer to fetch for.
    <AuthProvider>
      <NotificationProvider>
        <div className="l-shell">
          <Header />
          <main className="l-main">
            <Routes>
              <Route path="/" element={<Home />} />
              <Route path="/cars/:slug" element={<CarDetail />} />
              <Route path="/cars/:slug/test-drive" element={<BookTestDrive />} />
              <Route path="/account" element={<Account />} />
              <Route path="/account/login" element={<Account mode="login" />} />
              <Route path="/account/register" element={<Account mode="register" />} />
              <Route path="/account/verify" element={<VerifyEmail />} />
              <Route path="/account/reset" element={<ResetPassword />} />
            </Routes>
          </main>
          <Footer />
        </div>
      </NotificationProvider>
    </AuthProvider>
  );
}
