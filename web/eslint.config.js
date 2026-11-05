import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "dist-embed", "node_modules", "eslint.config.js"] },
  ...tseslint.configs.strictTypeChecked,
  {
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
  },
);
