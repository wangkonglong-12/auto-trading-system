import eslint from "@eslint/js";
import pluginVue from "eslint-plugin-vue";
import globals from "globals";
import tseslint from "typescript-eslint";

export default [
  {
    ignores: ["coverage/**", "node_modules/**"],
  },
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs["flat/recommended"],
  {
    files: ["**/*.{ts,vue}"],
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.node,
      },
      parserOptions: {
        parser: tseslint.parser,
      },
    },
    rules: {
      "@typescript-eslint/consistent-type-imports": "error",
      "@typescript-eslint/no-explicit-any": "error",
      "vue/block-order": ["error", { order: ["script", "template", "style"] }],
      "vue/component-name-in-template-casing": ["error", "PascalCase"],
      "vue/max-attributes-per-line": "off",
      "vue/multi-word-component-names": "error",
      "vue/no-v-html": "error",
      "vue/singleline-html-element-content-newline": "off",
    },
  },
  {
    files: ["src/node/*.vue"],
    rules: { "vue/html-self-closing": ["error", { html: { void: "always" } }] },
  },
];
