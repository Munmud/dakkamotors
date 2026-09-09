import { Route, Routes } from "react-router-dom";
import { useTranslation } from "react-i18next";

import CallButton from "./components/CallButton";
import Header from "./components/Header";
import CarDetail from "./pages/CarDetail";
import Home from "./pages/Home";

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
      </div>
    </footer>
  );
}

export default function App() {
  return (
    <div className="l-shell">
      <Header />
      <main className="l-main">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/cars/:id" element={<CarDetail />} />
        </Routes>
      </main>
      <Footer />
    </div>
  );
}
