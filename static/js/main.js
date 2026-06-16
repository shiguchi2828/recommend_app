document.addEventListener("click", async (event) => {
  const weatherButton = event.target.closest("[data-weather-refresh]");
  if (weatherButton) {
    updateHomeWeatherFromCurrentLocation(weatherButton);
    return;
  }

  const button = event.target.closest(".save-button");
  if (!button) return;

  const planId = button.dataset.planId;
  if (!planId) return;

  const currentlySaved = button.dataset.saved === "true";
  setSavingState(button, true);

  try {
    const response = await fetch(`/api/favorites/${planId}`, {
      method: currentlySaved ? "DELETE" : "POST",
      headers: { "Accept": "application/json" },
    });
    const data = await response.json();

    if (!response.ok || !data.ok) {
      throw new Error(data.message || "保存に失敗しました");
    }

    updateSaveButton(button, data.saved);
    showToast(data.saved ? "候補を保存しました" : "保存を解除しました", "success");

    if (!data.saved && button.dataset.removeCard === "true") {
      button.closest("[data-favorite-card]")?.remove();
      showEmptySavedStateIfNeeded();
    }
  } catch (error) {
    showToast(error.message);
  } finally {
    setSavingState(button, false);
  }
});

document.addEventListener("DOMContentLoaded", () => {
  updateHomeWeatherFromCurrentLocation();
});

document.addEventListener("submit", (event) => {
  const form = event.target.closest("[data-loading-form]");
  if (!form) return;

  // ── 連打防止: 送信中フラグで二重送信をブロック ──────────────────────────────
  if (form.dataset.submitting === "true") {
    event.preventDefault();
    return;
  }
  form.dataset.submitting = "true";

  const button = form.querySelector("[data-loading-text]");
  if (button) {
    button.dataset.defaultText = button.textContent;
    button.textContent = button.dataset.loadingText;
    button.disabled = true;
  }

  showLoadingOverlay(button?.dataset.loadingText || "候補を生成中...");

  // ── 安全タイムアウト: 60秒後に送信ロック解除（通信エラー対策） ──────────────
  setTimeout(() => {
    form.dataset.submitting = "false";
    if (button) {
      button.disabled = false;
      if (button.dataset.defaultText) button.textContent = button.dataset.defaultText;
    }
  }, 60_000);
});

function updateHomeWeatherFromCurrentLocation(triggerButton = null) {
  const panel = document.querySelector("[data-current-weather]");
  if (!panel) return;

  const note = panel.querySelector("[data-weather-note]");
  const button = triggerButton || panel.querySelector("[data-weather-refresh]");
  const locationEl = panel.querySelector("[data-weather-location]");

  if (!navigator.geolocation) {
    setWeatherNote(note, "このブラウザでは現在地取得に対応していません。");
    return;
  }

  const savedLocation = locationEl?.textContent || "";

  if (locationEl) {
    locationEl.textContent = "現在地を確認中...";
    locationEl.classList.add("is-fetching");
  }
  setButtonLoading(button, true);
  setWeatherNote(note, "現在地の天気を取得中...");

  navigator.geolocation.getCurrentPosition(
    async (position) => {
      const { latitude, longitude } = position.coords;
      try {
        const params = new URLSearchParams({
          lat: latitude.toString(),
          lon: longitude.toString(),
        });
        const response = await fetch(`/api/weather/current?${params}`);
        const data = await response.json();

        if (!response.ok || !data.ok) {
          throw new Error(data.message || "現在地の天気取得に失敗しました。");
        }

        renderCurrentWeather(panel, data.weather);
        setWeatherNote(note, "現在地の天気を表示しています。");
      } catch (error) {
        restoreLocation(locationEl, savedLocation);
        setWeatherNote(note, error.message);
      } finally {
        setButtonLoading(button, false);
      }
    },
    () => {
      restoreLocation(locationEl, savedLocation);
      setWeatherNote(note, "位置情報へのアクセスを許可すると、現在地の天気が表示されます。");
      setButtonLoading(button, false);
    },
    {
      enableHighAccuracy: false,
      timeout: 8000,
      maximumAge: 0,
    },
  );
}

function restoreLocation(el, text) {
  if (!el) return;
  el.textContent = text;
  el.classList.remove("is-fetching");
}

function setButtonLoading(button, loading) {
  if (!button) return;

  if (loading) {
    button.dataset.defaultText = button.textContent;
    button.textContent = button.dataset.loadingText || "取得中...";
  } else if (button.dataset.defaultText) {
    button.textContent = button.dataset.defaultText;
  }

  button.disabled = loading;
}

function renderCurrentWeather(panel, weather) {
  const locEl = panel.querySelector("[data-weather-location]");
  if (locEl) { locEl.textContent = weather.location_name || "現在地周辺"; locEl.classList.remove("is-fetching"); }
  setText(panel, "[data-weather-temperature]", `${weather.temperature}℃`);
  setText(panel, "[data-weather-condition]", weather.condition);
  setText(panel, "[data-weather-summary]", weather.summary);
  setText(panel, "[data-weather-rain]", `${weather.rain_chance}%`);
  setText(panel, "[data-weather-recommendation]", weather.recommendation);
  updateWeatherVisual(weather.condition);
}

function setText(root, selector, text) {
  const element = root.querySelector(selector);
  if (element) element.textContent = text;
}

function setWeatherNote(note, text) {
  if (note) note.textContent = text;
}

function updateSaveButton(button, saved) {
  button.dataset.saved = String(saved);
  button.textContent = saved ? "★" : "☆";
  button.classList.toggle("is-saved", saved);
  button.setAttribute("aria-label", saved ? "保存を解除する" : "保存する");
}

function setSavingState(button, saving) {
  button.disabled = saving;
  button.classList.toggle("is-loading", saving);
}

function updateWeatherVisual(condition) {
  const wtype = condition?.includes("雨") || condition?.includes("雷") ? "rainy"
    : condition?.includes("くもり") || condition?.includes("霧") ? "cloudy"
    : "sunny";
  document.body.dataset.weather = wtype;
  document.querySelector(".weather-visual")?.setAttribute("data-weather", wtype);
}

function showToast(message, type = "error") {
  let container = document.querySelector(".toast-container");
  if (!container) {
    container = document.createElement("div");
    container.className = "toast-container";
    document.body.appendChild(container);
  }
  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  requestAnimationFrame(() => {
    requestAnimationFrame(() => toast.classList.add("toast-visible"));
  });
  setTimeout(() => {
    toast.classList.remove("toast-visible");
    setTimeout(() => toast.remove(), 280);
  }, 3000);
}

function showLoadingOverlay(text) {
  const overlay = document.querySelector("#loadingOverlay");
  if (!overlay) return;
  const label = overlay.querySelector("p");
  if (label && text) label.textContent = text;
  overlay.classList.add("is-active");
  overlay.removeAttribute("aria-hidden");
}

function showEmptySavedStateIfNeeded() {
  const list = document.querySelector(".saved-list");
  if (!list || list.children.length > 0) return;

  list.insertAdjacentHTML(
    "afterend",
    `<div class="empty-state">
      <p>まだ保存した候補はありません。</p>
      <a class="button button-primary" href="/result">候補を見る</a>
    </div>`,
  );
  list.remove();
}
