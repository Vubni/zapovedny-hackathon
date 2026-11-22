const form = document.getElementById("operator-login");
const password = document.getElementById("operator-password");
const loginButton = form.querySelector("button");
const logoutButton = document.getElementById("operator-logout");
const sessionControls = document.getElementById("operator-session");
const status = document.getElementById("operator-status");
const errorLabel = document.getElementById("operator-error");

function showError(message = "") {
  errorLabel.textContent = message;
  errorLabel.hidden = !message;
}

function showSession(active) {
  status.textContent = active ? "Вы вошли как оператор." : "";
  status.hidden = !active;
  form.hidden = active;
  sessionControls.hidden = !active;
}

async function request(path, options) {
  const response = await fetch("/api/operator/" + path, { cache: "no-store", ...options });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "Запрос не выполнен.");
  return body;
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  loginButton.disabled = true;
  showError();
  try {
    await request("login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: password.value }),
    });
    password.value = "";
    location.replace("/files");
  } catch (error) {
    showError(error.message || "Вход не выполнен.");
    loginButton.disabled = false;
    password.select();
  }
});

logoutButton.addEventListener("click", async () => {
  logoutButton.disabled = true;
  showError();
  try {
    await request("logout", { method: "POST" });
    showSession(false);
    password.focus();
  } catch (error) {
    showError(error.message || "Выход не выполнен.");
  } finally {
    logoutButton.disabled = false;
  }
});

try {
  showSession((await request("session")).authenticated);
} catch (error) {
  showSession(false);
  showError(error.message || "Не удалось проверить сессию.");
}
