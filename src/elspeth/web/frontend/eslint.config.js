import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    ignores: [
      "coverage/**",
      "dist/**",
      "node_modules/**",
      "playwright-report/**",
      "test-results/**",
    ],
  },
  {
    files: [
      "src/**/*.{ts,tsx}",
      "vite.config.ts",
      "playwright.oidc.config.ts",
      "tests/e2e/aws-ecs-oidc.staging.spec.ts",
      "tests/e2e/harness/oidc-*.ts",
      "tests/e2e/composer-workspace-*.spec.ts",
      "tests/e2e/composer-workspace.visual.spec.ts",
      "tests/e2e/composer-reflow.spec.ts",
      "tests/e2e/helpers/api.ts",
      "tests/e2e/helpers/workspace-*.ts",
      "tests/e2e/page-objects/composer-page.ts",
    ],
    linterOptions: {
      reportUnusedDisableDirectives: "off",
    },
    languageOptions: {
      ecmaVersion: 2020,
      parser: tseslint.parser,
      globals: {
        ...globals.browser,
        ...globals.es2020,
        ...globals.node,
      },
    },
    plugins: {
      "react-hooks": reactHooks,
    },
    rules: {
      "react-hooks/exhaustive-deps": "warn",
      "react-hooks/rules-of-hooks": "error",
    },
  },
);
