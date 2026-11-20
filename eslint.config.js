import js from "@eslint/js";
import globals from "globals";

export default [
  { ignores: ["node_modules/", "data/", "ComfyUI/"] },
  js.configs.recommended,
  {
    files: ["model-manager/web/**/*.js"],
    languageOptions: { globals: globals.browser },
  },
  {
    files: ["tests/**/*.js", "eslint.config.js"],
    languageOptions: { globals: globals.node },
  },
];
