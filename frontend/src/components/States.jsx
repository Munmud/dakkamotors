import { useTranslation } from "react-i18next";

export function LoadingState() {
  const { t } = useTranslation();
  return (
    <p className="state state__body" role="status">
      {t("loading")}…
    </p>
  );
}

export function EmptyState() {
  const { t } = useTranslation();
  return (
    <div className="state">
      <h2 className="state__title">{t("empty.title")}</h2>
      <p className="state__body">{t("empty.body")}</p>
    </div>
  );
}

export function ErrorState({ title, body, onRetry }) {
  const { t } = useTranslation();
  return (
    <div className="state" role="alert">
      <h2 className="state__title">{title ?? t("error.title")}</h2>
      <p className="state__body">{body ?? t("error.body")}</p>
      {onRetry && (
        <button type="button" className="btn" onClick={onRetry}>
          {t("error.retry")}
        </button>
      )}
    </div>
  );
}
