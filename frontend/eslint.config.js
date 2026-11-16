import js from "@eslint/js";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "coverage"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [
      js.configs.recommended,
      ...tseslint.configs.strict,
      jsxA11y.flatConfigs.strict,
      reactHooks.configs.flat.recommended,
    ],
    languageOptions: {
      globals: globals.browser,
    },
    rules: {
      // Tabler classes that fail contrast in the dark theme (UX spec 5.4): use the subtle recipe,
      // solid primary, or text-secondary instead.
      "no-restricted-syntax": [
        "error",
        {
          selector:
            "Literal[value=/(^|\\s)(bg-[a-z]+-lt|btn-outline-[a-z]+|btn-success|btn-warning|btn-info|text-muted)(\\s|$)/]",
          message:
            "Low-contrast Tabler class: see UX spec 5.4 (use subtle badges, btn-primary or text-secondary).",
        },
        {
          selector:
            "TemplateElement[value.raw=/(^|\\s)(bg-[a-z]+-lt|btn-outline-[a-z]+|btn-success|btn-warning|btn-info|text-muted)(\\s|$)/]",
          message:
            "Low-contrast Tabler class: see UX spec 5.4 (use subtle badges, btn-primary or text-secondary).",
        },
        {
          selector: "ImportNamespaceSpecifier[parent.source.value='@tabler/icons-react']",
          message: "Import icons by name so unused ones are tree-shaken.",
        },
      ],
    },
  },
);
