import { createContext, useContext } from "react";

export type Theme = "dark" | "light";

export const THEME_KEY = "baluarte.theme";

/** The theme on the page now; `public/theme-init.js` sets it before the app loads. */
export function currentTheme(): Theme {
  return document.documentElement.getAttribute("data-bs-theme") === "light" ? "light" : "dark";
}

/** Applies a theme and remembers it in this browser. Dark is the default, so only light is stored. */
export function applyTheme(theme: Theme): void {
  document.documentElement.setAttribute("data-bs-theme", theme);
  try {
    if (theme === "light") window.localStorage.setItem(THEME_KEY, "light");
    else window.localStorage.removeItem(THEME_KEY);
  } catch {
    // Storage can be blocked; the choice then lasts until the page reloads.
  }
}

export interface ThemeState {
  theme: Theme;
  setTheme: (theme: Theme) => void;
}

export const ThemeContext = createContext<ThemeState | null>(null);

export function useTheme(): ThemeState {
  const state = useContext(ThemeContext);
  if (!state) throw new Error("useTheme must be used inside ThemeProvider");
  return state;
}
