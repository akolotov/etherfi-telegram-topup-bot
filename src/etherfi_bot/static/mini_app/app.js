(() => {
  const tg = window.Telegram?.WebApp;
  const base = location.pathname.replace(/\/$/, "");
  const el = id => document.getElementById(id);
  let maximum = 0;
  const auth = { Authorization: `tma ${tg?.initData || ""}` };

  function format(value) {
    return new Intl.NumberFormat("en-US", { maximumFractionDigits: 6 }).format(Number(value));
  }
  function selectedAmount() { return Number(el("amount").value.replace(",", ".")); }
  function validate() {
    const amount = selectedAmount();
    const valid = Number.isFinite(amount) && amount > 0 && amount <= maximum && /^\d+(?:[.,]\d{1,6})?$/.test(el("amount").value.trim());
    el("continue").disabled = !valid;
    el("error").textContent = amount > maximum ? "Amount exceeds the available maximum." : "";
    return valid;
  }
  function choose(value, button) {
    el("amount").value = value;
    document.querySelectorAll(".presets button").forEach(item => item.classList.toggle("selected", item === button));
    validate();
  }
  async function request(path, options = {}) {
    const response = await fetch(`${base}/api/${path}`, { ...options, headers: { ...auth, ...(options.headers || {}) } });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || "Request failed");
    return body;
  }
  async function load() {
    tg?.ready(); tg?.expand();
    try {
      const data = await request("context");
      maximum = Number(data.maximum_amount);
      el("target-balance").textContent = format(data.target_balance);
      el("safe-balance").textContent = format(data.safe_balance);
      el("limit").textContent = `Maximum top up: ${format(data.maximum_amount)} USDC`;
      data.preset_amounts.forEach(value => {
        const button = document.createElement("button");
        button.type = "button"; button.textContent = `${format(value)} USDC`;
        button.disabled = Number(value) > maximum;
        button.addEventListener("click", () => choose(value, button));
        el("presets").appendChild(button);
      });
      el("loading").hidden = true; el("form").hidden = false;
    } catch (error) { el("loading").textContent = error.message; }
  }
  el("amount").addEventListener("input", () => { document.querySelectorAll(".presets button").forEach(item => item.classList.remove("selected")); validate(); });
  el("continue").addEventListener("click", async () => {
    if (!validate()) return;
    el("continue").disabled = true; el("error").textContent = "";
    try {
      await request("prepare", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ amount: el("amount").value.replace(",", ".") }) });
      tg?.HapticFeedback?.notificationOccurred("success"); tg?.close();
    } catch (error) { validate(); el("error").textContent = error.message; }
  });
  load();
})();
