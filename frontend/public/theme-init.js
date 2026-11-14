// Dark is the default whatever the device prefers; a light choice made in the app is remembered.
// Loaded before the app so the first paint already has the chosen theme.
try {
  if (window.localStorage.getItem("baluarte.theme") === "light") {
    document.documentElement.setAttribute("data-bs-theme", "light");
  }
} catch {
  // Storage can be blocked; the page stays dark.
}
