import { useMemo, useState, type ReactNode } from "react";

import { applyTheme, currentTheme, ThemeContext, type Theme } from "./theme";

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(currentTheme);
  const value = useMemo(
    () => ({
      theme,
      setTheme: (next: Theme) => {
        applyTheme(next);
        setThemeState(next);
      },
    }),
    [theme],
  );
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}
