import nextConfig from "eslint-config-next";

/** @type {import("eslint").Linter.Config[]} */
const config = [
  {
    ignores: [
      ".next/**",
      "node_modules/**",
      "playwright-report/**",
      "test-results/**",
      "src/types/api.generated.ts",
      "coverage/**",
    ],
  },
  ...nextConfig,
  {
    rules: {
      // Server-only secrets must never be referenced from a `NEXT_PUBLIC_`
      // name; enforced instead by the config unit tests (task §5), since
      // there is no reliable lint rule for "this module runs server-side".
    },
  },
];

export default config;
