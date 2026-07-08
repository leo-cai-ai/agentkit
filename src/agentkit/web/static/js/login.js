(() => {
  const form = document.querySelector("[data-login-form]");
  const token = document.getElementById("access-token");
  const visibility = document.querySelector("[data-token-visibility-toggle]");
  const submit = form?.querySelector('[type="submit"]');

  visibility?.addEventListener("click", () => {
    const visible = token?.type === "text";
    if (token) token.type = visible ? "password" : "text";
    visibility.textContent = visible ? "显示" : "隐藏";
    visibility.setAttribute("aria-pressed", String(!visible));
    token?.focus();
  });

  form?.addEventListener("submit", () => {
    if (!submit) return;
    submit.disabled = true;
    submit.textContent = submit.dataset.loadingLabel || "正在验证";
  });
})();
